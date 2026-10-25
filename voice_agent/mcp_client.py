"""Persistent official MCP stdio session, owned by one background async task."""
import asyncio
from contextlib import suppress
import json
import os
from pathlib import Path
import sys
from threading import Event, Thread
from concurrent.futures import TimeoutError
from time import monotonic

from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client

from voice_agent.mcp_policy import AGENT_TOOLS, RESERVED


class MCPToolkit:
    def __init__(self, settings):
        self.settings = settings
        self.ready = Event()
        self.error = None
        self.schemas = {}
        self.thread = Thread(target=self._thread_main, daemon=True, name="clinic-mcp")
        self.thread.start()
        if not self.ready.wait(20):
            raise RuntimeError("MCP server startup timed out")
        if self.error:
            raise RuntimeError("MCP server could not start") from None

    def _thread_main(self):
        try:
            asyncio.run(self._serve())
        except Exception as error:
            self.error = type(error).__name__
            self.ready.set()

    async def _serve(self):
        self.loop = asyncio.get_running_loop()
        self.stop = asyncio.Event()
        # Explicit allowlist; never forward Claude/Deepgram/LangSmith keys or .env.
        env = {key: os.environ[key] for key in ("PATH", "SYSTEMROOT", "TMPDIR", "TEMP", "TMP") if key in os.environ}
        env.update({"LANGSMITH_TRACING": "false", "PYTHONPATH": str(Path(__file__).resolve().parents[1])})
        params = StdioServerParameters(command=sys.executable, args=["-m", "voice_agent.mcp_server", "--data-dir", str(self.settings.data_dir.resolve()),
                                       "--knowledge", str(self.settings.insurance_kb_path.resolve()), "--timezone", self.settings.timezone], env=env)
        with open(os.devnull, "w") as errlog:
            async with stdio_client(params, errlog=errlog) as (read, write):
                async with ClientSession(read, write) as session:
                    self.session = session
                    await asyncio.wait_for(session.initialize(), timeout=10)
                    discovered = await asyncio.wait_for(session.list_tools(), timeout=10)
                    self.schemas = {tool.name: tool.model_dump() for tool in discovered.tools}
                    required = set.union(*AGENT_TOOLS.values())
                    if not required <= self.schemas.keys():
                        raise RuntimeError("MCP toolkit is incomplete")
                    self.ready.set()
                    await self.stop.wait()

    def public_schema(self, name):
        import copy
        schema = copy.deepcopy(self.schemas[name]["inputSchema"])
        schema["properties"].pop("scope", None)
        schema["required"] = [key for key in schema.get("required", []) if key != "scope"]
        schema["additionalProperties"] = False
        return schema

    async def _call(self, name, arguments):
        result = await asyncio.wait_for(self.session.call_tool(name, arguments), timeout=35)
        if result.isError:
            return {"error": "mcp_tool_rejected", "message": "The MCP tool rejected this operation. Review its authorization and required inputs."}
        if result.structuredContent is not None:
            return result.structuredContent
        text = "\n".join(block.text for block in result.content if getattr(block, "type", None) == "text")
        return json.loads(text)

    def call(self, agent, name, arguments, scope):
        if name not in AGENT_TOOLS.get(agent, set()):
            raise PermissionError("Agent is not allowed to call this MCP tool")
        if RESERVED & arguments.keys():
            raise PermissionError("Model cannot supply trusted MCP scope")
        import jsonschema
        jsonschema.validate(arguments, self.public_schema(name))
        future = asyncio.run_coroutine_threadsafe(self._call(name, {**arguments, "scope": {**scope, "agent": agent}}), self.loop)
        from voice_agent.execution import current_execution, ExecutionInterrupted
        control = current_execution.get()
        deadline = monotonic() + 40
        try:
            while True:
                if control:
                    control.check()
                try:
                    return future.result(timeout=0.05)
                except TimeoutError:
                    if monotonic() > deadline:
                        future.cancel()
                        raise
        except ExecutionInterrupted:
            future.cancel()
            raise

    def close(self):
        if self.thread.is_alive():
            with suppress(RuntimeError):
                self.loop.call_soon_threadsafe(self.stop.set)
            self.thread.join(timeout=10)
