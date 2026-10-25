"""Durable step replay and cooperative cancellation, fenced at SQL commit time."""
from concurrent.futures import ThreadPoolExecutor, TimeoutError
from contextvars import ContextVar, copy_context
import json
from threading import Lock
from uuid import uuid4

from langchain_core.messages import messages_from_dict, messages_to_dict

current_execution = ContextVar("current_execution", default=None)
write_scope = ContextVar("write_scope", default=None)


class ExecutionInterrupted(Exception):
    """The task was suspended or superseded; it must not publish a response."""


def assert_live(db, scope=None):
    scope = scope if scope is not None else write_scope.get()
    if not scope or not scope.get("execution_id"):
        return  # Legacy offline workflow and standalone toolkit fixtures.
    row = db.execute("SELECT e.status,e.revision,r.revision AS latest FROM executions e JOIN call_revisions r ON r.call_id=e.call_id WHERE e.id=?", (scope["execution_id"],)).fetchone()
    if not row or row["status"] != "running" or row["revision"] != scope["revision"] or row["latest"] != scope["revision"]:
        raise ExecutionInterrupted("Task was suspended or superseded")


class ExecutionJournal:
    def __init__(self, store):
        self.store = store
        self.pool = ThreadPoolExecutor(max_workers=8, thread_name_prefix="agent-model")
        self.futures, self.lock = {}, Lock()
        with store.connect() as db:
            db.executescript("""
                CREATE TABLE IF NOT EXISTS call_revisions(call_id TEXT PRIMARY KEY, revision INTEGER NOT NULL);
                CREATE TABLE IF NOT EXISTS executions(
                    id TEXT PRIMARY KEY,call_id TEXT NOT NULL,request_id TEXT NOT NULL,message TEXT NOT NULL,
                    revision INTEGER NOT NULL,status TEXT NOT NULL,base TEXT NOT NULL,clock TEXT NOT NULL,
                    trace_id TEXT NOT NULL,UNIQUE(call_id,request_id));
                CREATE TABLE IF NOT EXISTS execution_steps(
                    execution_id TEXT NOT NULL,step_key TEXT NOT NULL,payload TEXT NOT NULL,
                    PRIMARY KEY(execution_id,step_key));
                CREATE TABLE IF NOT EXISTS tool_effects(effect_key TEXT PRIMARY KEY,payload TEXT NOT NULL);
                CREATE TABLE IF NOT EXISTS insurance_retrievals(query_key TEXT PRIMARY KEY,payload TEXT NOT NULL);
            """)

    def submit(self, call_id, message, request_id, clock):
        with self.store.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            prior = db.execute("SELECT * FROM executions WHERE call_id=? AND request_id=?", (call_id, request_id)).fetchone()
            if prior:
                if prior["message"] != message:
                    raise ValueError("同一个 request_id 不能用于不同的消息")
                return dict(prior)
            record = db.execute("SELECT state FROM calls WHERE id=?", (call_id,)).fetchone()
            if not record:
                raise KeyError(call_id)
            revision = self.bump(db, call_id)
            db.execute("UPDATE executions SET status='superseded' WHERE call_id=? AND status IN ('queued','running','suspended')", (call_id,))
            base = json.loads(record["state"])
            facts = base.setdefault("trusted_facts", {})
            # Reconcile effects committed before a crash/cancellation saved the parent response.
            from voice_agent.context import FACT_FIELDS
            for domain, table in (("booking", "bookings"), ("handoff", "transfers")):
                receipt = db.execute(f"SELECT * FROM {table} WHERE call_id=? ORDER BY rowid DESC LIMIT 1", (call_id,)).fetchone()
                if receipt:
                    value = {k: receipt[k] for k in FACT_FIELDS[domain] if k in receipt.keys()}
                    facts[domain] = {"source": "sql", "value": value}
                    if domain == "handoff":
                        base[domain] = {**base.get(domain, {}), **value}
            receipt = db.execute("SELECT payload FROM insurance_assessments WHERE call_id=? ORDER BY rowid DESC LIMIT 1", (call_id,)).fetchone()
            if receipt:
                value = json.loads(receipt[0])
                facts["insurance"] = {"source": "sql", "value": {k: value[k] for k in FACT_FIELDS["insurance"] if k in value}}
            values = (str(uuid4()), call_id, request_id, message, revision, "queued", json.dumps(base), clock, str(uuid4()))
            db.execute("INSERT INTO executions VALUES (?,?,?,?,?,?,?,?,?)", values)
            return dict(db.execute("SELECT * FROM executions WHERE id=?", (values[0],)).fetchone())

    @staticmethod
    def bump(db, call_id):
        db.execute("INSERT INTO call_revisions VALUES (?,1) ON CONFLICT(call_id) DO UPDATE SET revision=revision+1", (call_id,))
        return db.execute("SELECT revision FROM call_revisions WHERE call_id=?", (call_id,)).fetchone()[0]

    def interrupt(self, call_id):
        with self.store.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            self.bump(db, call_id)
            db.execute("UPDATE executions SET status='suspended' WHERE call_id=? AND status IN ('queued','running')", (call_id,))

    def pending(self, call_id):
        with self.store.connect() as db:
            row = db.execute("SELECT * FROM executions WHERE call_id=? AND status IN ('queued','running','suspended') ORDER BY rowid DESC LIMIT 1", (call_id,)).fetchone()
        return dict(row) if row else None

    def activate(self, execution_id, *, resume=False):
        with self.store.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            row = dict(db.execute("SELECT * FROM executions WHERE id=?", (execution_id,)).fetchone())
            latest = db.execute("SELECT revision FROM call_revisions WHERE call_id=?", (row["call_id"],)).fetchone()[0]
            if row["status"] in {"superseded", "completed"}:
                raise ExecutionInterrupted("Task is no longer resumable")
            if resume:
                row["revision"] = self.bump(db, row["call_id"])
            elif row["revision"] != latest or row["status"] != "queued":
                raise ExecutionInterrupted("Task was interrupted before starting")
            db.execute("UPDATE executions SET status='running',revision=? WHERE id=?", (row["revision"], execution_id))
            row["status"] = "running"
            return ExecutionControl(self, row)

    def close(self):
        self.pool.shutdown(wait=False, cancel_futures=True)
        with self.lock:
            self.futures.clear()


class ExecutionControl:
    def __init__(self, journal, row):
        self.journal, self.row = journal, row
        self.step_key = None
        self.reply_sink = None

    def fork(self):
        # Each concurrent child owns its step key; durable records/revision stay shared.
        child = ExecutionControl(self.journal, self.row)
        return child

    def publish(self, state, text):
        if not self.reply_sink or not text:
            return
        self.check()
        self.journal.store.save_progress(state, self)
        self.check()
        self.reply_sink(text)

    def scope(self):
        return {"execution_id": self.row["id"], "revision": self.row["revision"], "step_key": self.step_key}

    def check(self):
        with self.journal.store.connect() as db:
            assert_live(db, self.scope())

    def cached(self, key):
        self.check()
        with self.journal.store.connect() as db:
            row = db.execute("SELECT payload FROM execution_steps WHERE execution_id=? AND step_key=?", (self.row["id"], key)).fetchone()
        return json.loads(row[0]) if row else None

    def save(self, key, payload):
        with self.journal.store.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            assert_live(db, self.scope())
            db.execute("INSERT OR IGNORE INTO execution_steps VALUES (?,?,?)", (self.row["id"], key, json.dumps(payload, ensure_ascii=False)))

    def model(self, key, invoke):
        payload = self.cached(key)
        if payload is not None:
            return messages_from_dict(payload)[0]
        identity = (self.row["id"], key)
        with self.journal.lock:
            future = self.journal.futures.get(identity)
            if future is None:
                future = self.journal.pool.submit(copy_context().run, invoke)
                self.journal.futures[identity] = future
        while True:
            self.check()
            try:
                result = future.result(timeout=0.05)
                break
            except TimeoutError:
                if future.done():
                    # A provider-raised TimeoutError is a failure, not a polling timeout.
                    with self.journal.lock:
                        self.journal.futures.pop(identity, None)
                    raise
                continue
        self.save(key, messages_to_dict([result]))
        with self.journal.lock:
            self.journal.futures.pop(identity, None)
        return result

    def tool(self, key, invoke):
        payload = self.cached(key)
        if payload is not None:
            return payload
        result = invoke()
        self.save(key, result)
        return result
