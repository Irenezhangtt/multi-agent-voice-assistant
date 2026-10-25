import json
from pathlib import Path
import sqlite3
import tomllib

import pytest

from evals.harbor_export import export_tasks
from evals.harbor_trial import run_case, verify_case


def test_exported_tasks_are_secret_free_versioned_and_offline(tmp_path):
    root = tmp_path / "export"
    manifest = export_tasks(root)
    assert manifest["tasks"] == 24
    assert export_tasks(root) == manifest
    for task in (root / "tasks").iterdir():
        config = tomllib.loads((task / "task.toml").read_text())
        assert config["environment"]["network_mode"] == "no-network"
        inputs = json.loads((task / "environment/case.json").read_text())
        assert all(set(turn) == {"message"} for turn in inputs["turns"])
        assert not list((task / "environment").rglob(".env"))
        assert not list((task / "environment").rglob("*.sqlite3"))
        assert not (task / "environment/source/evals/dataset.json").exists()
    job = json.loads((root / "job.json").read_text())
    assert job["n_attempts"] == 3 and job["n_concurrent_trials"] == 4
    assert job["agents"][0]["import_path"] == "harbor_clinic_adapter:ClinicAgent"
    # Altered frozen bundles must not be silently accepted or overwritten.
    target = root / "tasks/booking-confirmed/environment/case.json"
    target.write_text("{}")
    with pytest.raises(ValueError, match="modified"):
        export_tasks(root)


def test_all_exported_cases_run_and_grade_using_independent_state(tmp_path):
    root = tmp_path / "export"
    export_tasks(root)
    calls = set()
    for task in sorted((root / "tasks").iterdir()):
        output = tmp_path / "runs" / task.name
        result = run_case(task / "environment/case.json", output)
        calls.add(result["call_id"])
        grade = verify_case(task / "tests/expected.json", output, tmp_path / "verifiers" / task.name)
        assert grade["passed"], (task.name, grade)
    assert len(calls) == 24


def test_verifier_checks_actual_database_not_only_agent_report(tmp_path):
    root = tmp_path / "export"
    export_tasks(root)
    task = root / "tasks/booking-confirmed"
    output, verifier = tmp_path / "run", tmp_path / "verifier"
    run_case(task / "environment/case.json", output)
    assert verify_case(task / "tests/expected.json", output, verifier)["reward"] == 1
    with sqlite3.connect(output / "state/business.sqlite3") as db:
        db.execute("UPDATE bookings SET date='2099-01-01'")
    report = verify_case(task / "tests/expected.json", output, verifier)
    assert not report["passed"]
    assert (verifier / "reward.txt").read_text().strip() == "0"
    assert any(not c["passed"] for c in report["checks"])


def test_trial_cannot_reuse_an_existing_database(tmp_path):
    root = tmp_path / "export"
    export_tasks(root)
    task = root / "tasks/booking-confirmed"
    run_case(task / "environment/case.json", tmp_path / "run")
    with pytest.raises(ValueError, match="already exists"):
        run_case(task / "environment/case.json", tmp_path / "run")


def test_missing_evidence_is_a_failed_reward(tmp_path):
    expected = tmp_path / "expected.json"
    expected.write_text("{}")
    result = verify_case(expected, tmp_path / "missing", tmp_path / "verifier")
    assert not result["passed"]
    assert (tmp_path / "verifier/reward.txt").read_text().strip() == "0"


def test_multi_agent_harbor_profiles_use_real_mcp_and_independent_databases(tmp_path):
    root = tmp_path / "multi-export"
    manifest = export_tasks(root, dataset_path=Path("evals/multi-agent-dataset.json"))
    assert manifest["tasks"] == 4
    for task in sorted((root / "tasks").iterdir()):
        output = tmp_path / "multi-runs" / task.name
        result = run_case(task / "environment/case.json", output)
        assert result["mode"] == "scripted-models"
        assert result["versions"]["architecture"] == "multi-agent"
        assert any(e["type"] == "mcp_tool" for turn in result["turns"] for e in turn["response"]["state"]["events"])
        grade = verify_case(task / "tests/expected.json", output, tmp_path / "multi-verifier" / task.name)
        assert grade["passed"], (task.name, grade)
