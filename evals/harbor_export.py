"""Export the current dataset and a secret-free source snapshot to Harbor tasks."""
import argparse
from hashlib import sha256
import json
from pathlib import Path
import re


ROOT = Path(__file__).resolve().parents[1]
DEFAULT_BASE_IMAGE = "python:3.12.14-slim"
HARNESS_VERSION = "harbor-clinic-v2-mcp"


def encoded(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, indent=2).encode()


def export_tasks(output: Path, *, dataset_path: Path | None = None, source_root: Path = ROOT, base_image=DEFAULT_BASE_IMAGE):
    output = output.resolve()
    if not re.fullmatch(r"[a-zA-Z0-9./:@_-]+", base_image):
        raise ValueError("Invalid container base image reference")
    dataset_path = dataset_path or source_root / "evals/dataset.json"
    dataset_bytes = dataset_path.read_bytes()
    dataset = json.loads(dataset_bytes)
    source_files = sorted((source_root / "voice_agent").rglob("*.py"))
    source_files += [source_root / "evals" / name for name in ("__init__.py", "graders.py", "harbor_trial.py", "harbor_agent.py", "harbor_export.py", "multi_agent_cases.py")]
    source_files += sorted((source_root / "voice_agent" / "knowledge").glob("*.json"))
    source_files += sorted((source_root / "voice_agent" / "prompts").glob("*.md"))
    source_files += [source_root / "requirements.lock"]
    # Explicit file allowlist: never copy .env, data, recordings, virtualenvs or reports.
    sources = {str(p.relative_to(source_root)): p.read_bytes() for p in source_files}
    content_hashes = {name: sha256(body).hexdigest() for name, body in sorted(sources.items())}
    snapshot = {"harness_version": HARNESS_VERSION, "dataset_sha256": sha256(dataset_bytes).hexdigest(), "source_hashes": content_hashes, "base_image": base_image}
    snapshot_id = sha256(encoded(snapshot)).hexdigest()
    if output.exists():
        manifest_path = output / "manifest.json"
        if manifest_path.is_file():
            existing = json.loads(manifest_path.read_text())
            actual_files = [p for p in output.rglob("*")
                            if p.relative_to(output).parts[0] != "jobs" and "__pycache__" not in p.parts]
            actual_hashes = {str(p.relative_to(output)): sha256(p.read_bytes()).hexdigest()
                             for p in actual_files if p.is_file() and p != manifest_path}
            if (existing.get("snapshot_id") == snapshot_id and actual_hashes == existing.get("export_hashes")
                    and actual_hashes and not any(p.is_symlink() for p in actual_files)):
                return existing
        raise ValueError("Export directory exists with different or modified content; choose a new output directory")
    output.mkdir(parents=True)
    locks = sources["requirements.lock"].decode().splitlines()
    runtime_names = {"langchain", "langchain-anthropic", "langgraph", "langgraph-checkpoint-sqlite", "langsmith", "python-dotenv", "mcp"}
    runtime_locks = [line for line in locks if line.split("==")[0].lower() in runtime_names]
    if len(runtime_locks) != len(runtime_names):
        raise ValueError("Missing pinned workflow dependency in requirements.lock")
    dockerfile = f"""FROM {base_image}
WORKDIR /opt/voice-agent
COPY source/ /opt/voice-agent/
COPY runtime-requirements.txt /opt/voice-agent/runtime-requirements.txt
RUN pip install --no-cache-dir -r runtime-requirements.txt -c requirements.lock
ENV PYTHONPATH=/opt/voice-agent LANGSMITH_TRACING=false AGENT_MODE=mock
COPY case.json /app/case.json
RUN mkdir -p /logs/agent /logs/verifier
WORKDIR /app
"""
    ids = set()
    for case in dataset["cases"]:
        name = case["id"]
        if not re.fullmatch(r"[a-z0-9][a-z0-9-]{0,100}", name) or name in ids:
            raise ValueError("Task IDs must be unique safe directory names")
        ids.add(name)
        directory = output / "tasks" / name
        env = directory / "environment"
        tests = directory / "tests"
        env.mkdir(parents=True)
        tests.mkdir()
        input_case = {"id": name, "dataset_version": dataset["version"], "snapshot_id": snapshot_id,
                      "now": case.get("now", dataset["now"]), "faults": case.get("faults", {}),
                      "turns": [{"message": turn["message"]} for turn in case["turns"]]}
        if case.get("profile"):
            input_case["profile"] = case["profile"]
        (env / "case.json").write_bytes(encoded(input_case))
        (env / "Dockerfile").write_text(dockerfile)
        (env / ".dockerignore").write_text("**/.env*\n**/*.sqlite*\n**/__pycache__\n**/.git\n")
        (env / "runtime-requirements.txt").write_text("\n".join(runtime_locks) + "\n")
        for relative, body in sources.items():
            target = env / "source" / relative
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(body)
        (directory / "instruction.md").write_text(
            f"Run the clinic workflow application on the scripted caller turns in /app/case.json.\n"
            f"Task: {name}. Snapshot: {snapshot_id}.\n"
            "Use the ClinicAgent adapter; do not modify the app or fabricate outcomes.\n"
        )
        (directory / "task.toml").write_text(f'''schema_version = "1.3"
[metadata]
category = "clinic-workflow-regression"
dataset_version = "{dataset['version']}"
snapshot_id = "{snapshot_id}"
[agent]
timeout_sec = 120
[verifier]
timeout_sec = 60
network_mode = "no-network"
[environment]
network_mode = "no-network"
cpus = 1
memory_mb = 1024
storage_mb = 2048
''')
        expected = {"case_id": name, "dataset_version": dataset["version"], "snapshot_id": snapshot_id, "turns": case["turns"]}
        (tests / "expected.json").write_bytes(encoded(expected))
        (tests / "test.sh").write_text("#!/bin/bash\nset -euo pipefail\npython -m evals.harbor_trial verify --case /tests/expected.json --output /logs/agent --verifier-dir /logs/verifier\n")
    (output / "harbor_clinic_adapter.py").write_bytes(sources["evals/harbor_agent.py"])
    job = {"n_attempts": 3, "n_concurrent_trials": 4,
           "jobs_dir": str(output / "jobs"), "environment": {"type": "docker", "delete": True},
           "agents": [{"import_path": "harbor_clinic_adapter:ClinicAgent"}],
           "datasets": [{"path": str(output / "tasks")}]}
    (output / "job.json").write_bytes(encoded(job))
    hashes = {str(p.relative_to(output)): sha256(p.read_bytes()).hexdigest() for p in sorted(output.rglob("*")) if p.is_file()}
    manifest = {**snapshot, "snapshot_id": snapshot_id, "dataset_version": dataset["version"], "tasks": len(ids),
                "mode": "scripted-models" if all(c.get("profile") for c in dataset["cases"]) else "mock", "scope": "offline text workflow regression; no live STT/TTS/LLM calls",
                "job_config": str(output / "job.json"), "export_hashes": hashes}
    (output / "manifest.json").write_bytes(encoded(manifest))
    return manifest


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, default=Path("reports/harbor/export"))
    parser.add_argument("--dataset", type=Path)
    parser.add_argument("--base-image", default=DEFAULT_BASE_IMAGE)
    args = parser.parse_args()
    try:
        manifest = export_tasks(args.output, dataset_path=args.dataset, base_image=args.base_image)
    except ValueError as exc:
        parser.error(str(exc))
    print(json.dumps({key: manifest[key] for key in ("snapshot_id", "tasks", "mode", "job_config")}, indent=2))


if __name__ == "__main__":
    main()
