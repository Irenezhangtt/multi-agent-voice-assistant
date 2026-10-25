"""Run a scripted call in a sandbox; grade separately from backend evidence."""
import argparse
from datetime import datetime
from importlib.metadata import version
import json
from pathlib import Path
import sqlite3
from contextlib import nullcontext
from unittest.mock import patch

from langsmith import tracing_context

from evals.graders import GRADER_VERSION, grade_turn
from voice_agent.config import Settings
from voice_agent.runtime import AgentRuntime


def run_case(case_path: Path, output: Path):
    case = json.loads(case_path.read_text())
    output.mkdir(parents=True, exist_ok=True)
    state_dir = output / "state"
    # Never reuse state from a prior trial, even if a runner mistakenly reuses a folder.
    if state_dir.exists():
        raise ValueError("Trial state directory already exists")
    state_dir.mkdir()
    now = datetime.fromisoformat(case["now"])
    responses = []
    profile = case.get("profile")
    with tracing_context(enabled=False), (patch.dict("os.environ", {"ANTHROPIC_API_KEY": "synthetic-offline-key"}) if profile else nullcontext()):
        settings = Settings(mode="claude" if profile else "mock", model="scripted-multi-agent-v1" if profile else "claude-sonnet-4-6", data_dir=state_dir)
        models = None
        if profile:
            from evals.multi_agent_cases import scripted_models
            models = scripted_models(profile)
        runtime = AgentRuntime(settings, faults=case.get("faults", {}), clock=lambda: now, model_factory=(lambda name: models[name]) if models else None)
        try:
            call_id = runtime.create_call()["state"]["call_id"]
            for index, turn in enumerate(case["turns"]):
                response = runtime.turn(call_id, turn["message"], request_id=f"scripted-{index}")
                responses.append({"response": response, "outcomes": runtime.store.outcomes(call_id)})
            result = {
                "case_id": case["id"], "call_id": call_id, "dataset_version": case["dataset_version"],
                "snapshot_id": case["snapshot_id"], "now": case["now"], "mode": "scripted-models" if profile else "mock",
                "versions": settings.versions(), "turns": responses,
                "packages": {name: version(name) for name in ("langgraph", "langchain", "langsmith")},
                "traces": runtime.store.traces(call_id),
            }
            (output / "trial.json").write_text(json.dumps(result, ensure_ascii=False, indent=2))
            return result
        finally:
            runtime.close()


def verify_case(expected_path: Path, output: Path, verifier_dir: Path):
    verifier_dir.mkdir(parents=True, exist_ok=True)
    checks, failure = [], None
    try:
        expected = json.loads(expected_path.read_text())
        result = json.loads((output / "trial.json").read_text())
        if any(result.get(key) != expected[key] for key in ("case_id", "snapshot_id", "dataset_version")):
            raise ValueError("Evidence belongs to a different case or snapshot")
        if len(result["turns"]) != len(expected["turns"]) or not result["turns"]:
            raise ValueError("Missing or extra turns")
        for index, (actual, target) in enumerate(zip(result["turns"], expected["turns"])):
            if actual["response"]["state"]["message"] != target["message"]:
                raise ValueError("Caller input does not match the test case")
            for check in grade_turn(actual["response"], actual["outcomes"], target["expected"]):
                checks.append({"turn": index, **check})
        # Independently read the actual final DB, rather than trusting the agent report.
        database = (output / "state" / "business.sqlite3").resolve()
        with sqlite3.connect(database.as_uri() + "?mode=ro", uri=True) as db:
            db.row_factory = sqlite3.Row
            outcomes = {table: [dict(r) for r in db.execute(f"SELECT * FROM {table} WHERE call_id=?", (result["call_id"],))]
                        for table in ("bookings", "transfers", "insurance_queries")}
        for check in grade_turn(result["turns"][-1]["response"], outcomes, expected["turns"][-1]["expected"]):
            checks.append({"turn": "final_database", **check})
        checks.append({"name": "final_database_matches_report", "passed": outcomes == result["turns"][-1]["outcomes"]})
    except Exception as exc:
        failure = type(exc).__name__
    passed = failure is None and bool(checks) and all(c["passed"] for c in checks)
    report = {"passed": passed, "reward": int(passed), "grader_version": GRADER_VERSION, "checks": checks, "error_type": failure}
    (verifier_dir / "report.json").write_text(json.dumps(report, ensure_ascii=False, indent=2))
    (verifier_dir / "reward.txt").write_text("1\n" if passed else "0\n")
    return report


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("command", choices=["run", "verify"])
    parser.add_argument("--case", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--verifier-dir", type=Path, default=Path("/logs/verifier"))
    args = parser.parse_args()
    if args.command == "run":
        run_case(args.case, args.output)
    else:
        # Reward=0 is an evaluation failure, not an infrastructure command failure.
        print(json.dumps(verify_case(args.case, args.output, args.verifier_dir), ensure_ascii=False))


if __name__ == "__main__":
    main()
