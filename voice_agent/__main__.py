import argparse
import json
import os

from voice_agent.config import Settings


def main():
    parser = argparse.ArgumentParser(description="Clinic Voice Agent local foundation")
    parser.add_argument("command", choices=["serve", "eval", "doctor"], nargs="?", default="serve")
    parser.add_argument("--port", type=int, default=8765)
    parser.add_argument("--dataset", default="evals/dataset.json")
    parser.add_argument("--report", default="reports/eval-latest.json")
    args = parser.parse_args()
    settings = Settings.from_env()
    if args.command == "doctor":
        import platform
        print(json.dumps({
            "python": platform.python_version(), "mode": settings.mode, "architecture": settings.versions()["architecture"], "model": settings.model,
            "claude_key_configured": bool(os.getenv("ANTHROPIC_API_KEY", "").strip()),
            "claude_workspace_configured": bool(os.getenv("ANTHROPIC_WORKSPACE_ID", "").strip()),
            "langsmith_tracing": os.getenv("LANGSMITH_TRACING", "false").lower() == "true",
            "langsmith_key_configured": bool(os.getenv("LANGSMITH_API_KEY", "").strip()),
            "business_backend": "local-sql", "telephony": "not_connected", "data_dir": str(settings.data_dir.resolve()),
            "speech": settings.speech_status(),
        }, ensure_ascii=False, indent=2))
    elif args.command == "eval":
        # Foundation regressions are offline and do not consume Claude credits.
        from evals.runner import run_dataset
        summary = run_dataset(args.dataset, args.report)
        print(json.dumps(summary, ensure_ascii=False, indent=2))
        raise SystemExit(0 if summary["failed"] == 0 else 1)
    else:
        import uvicorn
        from voice_agent.api import create_app
        uvicorn.run(create_app(settings), host="127.0.0.1", port=args.port, workers=1)


if __name__ == "__main__":
    main()
