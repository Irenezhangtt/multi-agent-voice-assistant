from datetime import datetime
from importlib.metadata import version
import json
from pathlib import Path
from tempfile import TemporaryDirectory

from langsmith import tracing_context

from evals.graders import GRADER_VERSION, grade_turn
from voice_agent.config import Settings
from voice_agent.runtime import AgentRuntime
from voice_agent.storage import utc_now


def run_dataset(dataset_path="evals/dataset.json", report_path="reports/eval-latest.json"):
    dataset = json.loads(Path(dataset_path).read_text())
    results = []
    with tracing_context(enabled=False):
        for case in dataset["cases"]:
            # A clean database + checkpointer for each case; turns within a case share state.
            with TemporaryDirectory(prefix="voice-agent-eval-") as folder:
                settings = Settings(mode="mock", data_dir=Path(folder))
                now = datetime.fromisoformat(case.get("now", dataset["now"]))
                runtime = AgentRuntime(settings, faults=case.get("faults", {}), clock=lambda: now)
                turns = []
                try:
                    call_id = runtime.create_call()["state"]["call_id"]
                    for item in case["turns"]:
                        response = runtime.turn(call_id, item["message"])
                        outcomes = runtime.store.outcomes(call_id)
                        checks = grade_turn(response, outcomes, item["expected"])
                        turns.append({
                            "message": item["message"], "reply": response["state"]["reply"],
                            "trace_id": response["trace_id"], "duration_ms": response["duration_ms"],
                            "checks": checks, "events": response["state"]["events"],
                        })
                    results.append({"id": case["id"], "passed": all(c["passed"] for t in turns for c in t["checks"]), "turns": turns})
                except Exception as exc:
                    results.append({"id": case["id"], "passed": False, "error_type": type(exc).__name__, "turns": turns})
                finally:
                    runtime.close()
    summary = {"total": len(results), "passed": sum(r["passed"] for r in results), "failed": sum(not r["passed"] for r in results)}
    report = {
        "created_at": utc_now(), "dataset_version": dataset["version"], "grader_version": GRADER_VERSION,
        "harness_version": "local-harness-v1", "mode": "mock", "business_backend": "local-sql",
        "scope": "Synthetic text workflow regressions; not Claude, audio, telephony, or production success rates.",
        "versions": Settings().versions(),
        "packages": {name: version(name) for name in ("langchain", "langgraph", "langsmith")},
        "summary": summary, "results": results,
    }
    output = Path(report_path)
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(json.dumps(report, ensure_ascii=False, indent=2))
    return {**summary, "report": str(output.resolve()), "mode": "mock"}
