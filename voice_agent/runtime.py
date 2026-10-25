from datetime import datetime, timezone
import json
import sqlite3
from threading import RLock
from time import perf_counter
from uuid import UUID, uuid4

from langgraph.checkpoint.sqlite import SqliteSaver

from voice_agent.config import Settings
from voice_agent.execution import ExecutionJournal, ExecutionInterrupted, current_execution, write_scope
from voice_agent.graph import build_graph
from voice_agent.storage import Store
from voice_agent.tools import LocalBackend, make_tools


class AgentRuntime:
    """One local worker process. SQL owns task revisions, receipts and step replay."""

    def __init__(self, settings: Settings, *, faults=None, clock=None, model_factory=None, mcp_toolkit=None):
        self.settings = settings
        self.clock = clock or (lambda: datetime.now(timezone.utc))
        self.lock, self.call_locks = RLock(), {}
        settings.data_dir.mkdir(parents=True, exist_ok=True)
        self.store = Store(settings.data_dir / "business.sqlite3")
        self.journal = ExecutionJournal(self.store)
        self.connection = sqlite3.connect(settings.data_dir / "checkpoints.sqlite3", check_same_thread=False)
        self.checkpointer = SqliteSaver(self.connection)
        self.backend = LocalBackend(self.store, settings, faults=faults)
        self.multi_agent = None
        if settings.mode == "claude" and settings.architecture == "multi-agent":
            from voice_agent.multi_agent import MultiAgentSystem
            self.multi_agent = MultiAgentSystem(settings, self.clock, toolkit=mcp_toolkit, model_factory=model_factory)
            self.graph = self.multi_agent.graph(self.checkpointer)
        else:
            self.graph = build_graph(settings, make_tools(self.backend), self.checkpointer, self.clock)

    def call_lock(self, call_id):
        with self.lock:
            return self.call_locks.setdefault(call_id, RLock())

    def close(self):
        with self.store.connect() as db:
            calls = [row[0] for row in db.execute("SELECT DISTINCT call_id FROM executions WHERE status IN ('running','queued')")]
        for call_id in calls:
            self.interrupt(call_id)
        for lock in list(self.call_locks.values()):
            with lock:
                pass
        if self.multi_agent:
            self.multi_agent.close()
        self.journal.close()
        self.connection.close()

    def create_call(self):
        state = self.store.create_call(self.settings.versions())
        return {"state": state, "versions": self.settings.versions(), "business_backend": "local-sql", "telephony": "not_connected"}

    def submit(self, call_id, message, request_id=None):
        record = self.store.get_call(call_id)
        if record["versions"] != self.settings.versions():
            raise ValueError("此通电话绑定了不同的系统版本，请新建测试电话")
        return self.journal.submit(call_id, message, request_id or str(uuid4()), self.clock().isoformat())

    def interrupt(self, call_id):
        self.journal.interrupt(call_id)

    def resume(self, call_id):
        with self.call_lock(call_id):
            pending = self.journal.pending(call_id)
            if not pending:
                raise ValueError("没有待恢复的任务")
            if self.store.get_call(call_id)["versions"] != self.settings.versions():
                raise ValueError("系统版本已变化，请新建测试电话")
            return self.execute(pending, resume=True)

    def turn(self, call_id: str, message: str, request_id: str | None = None):
        request_id = request_id or str(uuid4())
        existing = self.store.get_turn(call_id, request_id)
        if existing:
            if existing["message"] != message:
                raise ValueError("同一个 request_id 不能用于不同的消息")
            return json.loads(existing["response"])
        row = self.submit(call_id, message, request_id)
        return self.execute(row)

    def execute(self, row, *, resume=False, on_reply=None):
        call_id, request_id, message = row["call_id"], row["request_id"], row["message"]
        with self.call_lock(call_id):
            existing = self.store.get_turn(call_id, request_id)
            if existing:
                return json.loads(existing["response"])
            control = self.journal.activate(row["id"], resume=resume)
            control.reply_sink = on_reply
            token = current_execution.set(control)
            scope_token = write_scope.set(control.scope())
            try:
                trace_id = row["trace_id"]
                prior = json.loads(row["base"])
                config = {
                    "configurable": {"thread_id": call_id}, "run_id": UUID(trace_id),
                    "run_name": "call_turn", "recursion_limit": 15,
                    "metadata": {"call_id": call_id, "execution_id": row["id"], "resumed": resume,
                                 "task_revision": control.row["revision"], "case_id": prior["case_id"], **self.settings.versions()},
                }
                start = perf_counter()
                state = self.graph.invoke({**prior, "message": message, "turn_id": request_id, "events": [], "reply": ""}, config)
                control.check()
                elapsed = round((perf_counter() - start) * 1000, 2)
                trace = {"trace_id": trace_id, "call_id": call_id, "request_id": request_id,
                         "execution_id": row["id"], "resumed": resume, "task_revision": control.row["revision"],
                         "versions": self.settings.versions(), "duration_ms": elapsed, "route": state.get("route"),
                         "status": state["status"], "events": state["events"], "message": message, "reply": state["reply"]}
                response = {"state": state, "trace_id": trace_id, "duration_ms": elapsed,
                            "versions": self.settings.versions(), "business_backend": "local-sql", "telephony": "not_connected"}
                self.store.save_turn(call_id, request_id, message, response, trace, execution=control)
                return response
            except ExecutionInterrupted:
                raise
            except Exception:
                # Preserve completed steps after any unexpected failure; explicit resume retries the unfinished step.
                self.interrupt(call_id)
                raise
            finally:
                current_execution.reset(token)
                write_scope.reset(scope_token)

    def get_call(self, call_id):
        pending = self.journal.pending(call_id)
        execution = {k: pending[k] for k in ("id", "request_id", "message", "status", "revision")} if pending else None
        return {**self.store.get_call(call_id), "execution": execution, "outcomes": self.store.outcomes(call_id), "traces": self.store.traces(call_id)}
