"""Shared Claude client configuration for routing and insurance assessment."""
import os

from langchain.chat_models import init_chat_model


def claude_model(settings, *, max_tokens, max_retries=0):
    workspace = os.getenv("ANTHROPIC_WORKSPACE_ID", "").strip()
    headers = {"anthropic-workspace-id": workspace} if workspace else {}
    return init_chat_model(settings.model, model_provider="anthropic", timeout=30,
                           max_retries=max_retries, max_tokens=max_tokens,
                           default_headers=headers)
