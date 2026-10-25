from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI, HTTPException, WebSocket
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, ConfigDict, Field
from starlette.middleware.trustedhost import TrustedHostMiddleware

from voice_agent.config import Settings
from voice_agent.runtime import AgentRuntime
from voice_agent.execution import ExecutionInterrupted
from voice_agent.speech import DeepgramSpeech
from voice_agent.voice import VoiceSession


class TurnInput(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)
    message: str = Field(min_length=1, max_length=2000)
    request_id: str | None = Field(default=None, min_length=1, max_length=80)


def create_app(settings: Settings | None = None, *, speech_factory=DeepgramSpeech):
    settings = settings or Settings.from_env()

    @asynccontextmanager
    async def lifespan(app):
        app.state.runtime = AgentRuntime(settings)
        app.state.voice_calls = set()
        yield
        app.state.runtime.close()

    app = FastAPI(title="Clinic Voice Agent — Local Foundation", version=settings.version, lifespan=lifespan)
    app.add_middleware(TrustedHostMiddleware, allowed_hosts=["127.0.0.1", "localhost", "testserver", "[::1]"])
    app.mount("/assets", StaticFiles(directory=Path(__file__).parent / "static"), name="assets")

    @app.get("/")
    def index():
        return FileResponse(Path(__file__).parent / "static" / "voice.html")

    @app.get("/debug")
    def debug():
        return FileResponse(Path(__file__).parent / "static" / "index.html")

    @app.get("/health")
    def health():
        return {
            "status": "ok", "mode": settings.mode, "architecture": settings.versions()["architecture"],
            "model": settings.model if settings.mode == "claude" else "deterministic-mock-v1",
            "business_backend": "local-sql", "telephony": "not_connected", "timezone": settings.timezone,
            "versions": settings.versions(),
            "speech": settings.speech_status(),
        }

    @app.post("/api/calls", status_code=201)
    def create_call():
        return app.state.runtime.create_call()

    @app.get("/api/calls/{call_id}")
    def get_call(call_id: str):
        try:
            return {**app.state.runtime.get_call(call_id), "voice_events": app.state.runtime.store.voice_events(call_id)}
        except KeyError:
            raise HTTPException(404, "测试电话不存在") from None

    @app.post("/api/calls/{call_id}/turns")
    def turn(call_id: str, body: TurnInput):
        if call_id in app.state.voice_calls:
            raise HTTPException(409, "请先结束此通语音连接，再使用文字调试。")
        try:
            return app.state.runtime.turn(call_id, body.message, body.request_id)
        except KeyError:
            raise HTTPException(404, "测试电话不存在") from None
        except ValueError as exc:
            raise HTTPException(409, str(exc)) from None
        except ExecutionInterrupted:
            raise HTTPException(409, "此任务已暂停或被新消息替代。") from None
        except Exception:
            raise HTTPException(500, "执行发生异常，请核验测试电话状态后再继续；不要假定操作已成功。") from None

    @app.get("/api/framework")
    def framework():
        return {"mermaid": app.state.runtime.graph.get_graph(xray=True).draw_mermaid(), "architecture": settings.versions()["architecture"]}

    @app.post("/api/calls/{call_id}/resume")
    def resume(call_id: str):
        if call_id in app.state.voice_calls:
            raise HTTPException(409, "语音连接会自动恢复待处理任务。")
        try:
            app.state.runtime.store.get_call(call_id)
            return app.state.runtime.resume(call_id)
        except KeyError:
            raise HTTPException(404, "测试电话不存在") from None
        except (ValueError, ExecutionInterrupted) as exc:
            raise HTTPException(409, str(exc)) from None

    @app.post("/api/calls/{call_id}/suspend")
    def suspend(call_id: str):
        try:
            app.state.runtime.store.get_call(call_id)
        except KeyError:
            raise HTTPException(404, "测试电话不存在") from None
        app.state.runtime.interrupt(call_id)
        return {"status": "suspended"}

    @app.get("/api/agents")
    def agents():
        from voice_agent.mcp_policy import AGENT_TOOLS
        system = app.state.runtime.multi_agent
        return {
            "architecture": settings.versions()["architecture"],
            "supervisor": {"strategy": "ReAct", "tools": ["delegate_" + name for name in AGENT_TOOLS]},
            "agents": {name: {"strategy": "ReAct", "prompt": f"voice_agent/prompts/{name}.md", "tools": sorted(names)} for name, names in AGENT_TOOLS.items()},
            "context": {"supervisor": "history + all trusted facts", "specialists": "isolated messages + selected trusted facts + own pending task"},
            "mcp": {"transport": "stdio", "server": "voice_agent.mcp_server", "connected": system is not None and system.toolkit.thread.is_alive()},
        }

    @app.get("/api/bookings")
    def bookings(customer_phone: str | None = None):
        """Local-only inspection; includes cancelled rows and modification receipts."""
        from voice_agent.identity import normalize_phone
        try:
            phone = normalize_phone(customer_phone) if customer_phone else None
        except ValueError as exc:
            raise HTTPException(422, str(exc)) from None
        with app.state.runtime.store.connect() as db:
            if phone:
                rows = db.execute("SELECT * FROM bookings WHERE customer_id=? ORDER BY updated_at DESC LIMIT 100", (phone,)).fetchall()
            else:
                rows = db.execute("SELECT * FROM bookings ORDER BY updated_at DESC LIMIT 100").fetchall()
        return {"bookings": [dict(row) for row in rows], "backend": "local-sql"}

    @app.get("/api/bookings/{booking_id}/history")
    def booking_history(booking_id: str):
        import json
        with app.state.runtime.store.connect() as db:
            if not db.execute("SELECT 1 FROM bookings WHERE id=?", (booking_id,)).fetchone():
                raise HTTPException(404, "Appointment not found")
            rows = db.execute("SELECT action,before_json,result,created_at FROM booking_mutations WHERE booking_id=? ORDER BY rowid", (booking_id,)).fetchall()
        return {"history": [{"action": row["action"], "before": json.loads(row["before_json"]), "after": json.loads(row["result"]), "created_at": row["created_at"]} for row in rows]}

    @app.websocket("/api/calls/{call_id}/voice")
    async def voice(ws: WebSocket, call_id: str):
        from urllib.parse import urlparse
        origin = ws.headers.get("origin")
        if origin and (urlparse(origin).netloc != ws.headers.get("host") or urlparse(origin).scheme not in {"http", "https"}):
            await ws.close(code=1008)
            return
        await ws.accept()
        try:
            record = app.state.runtime.store.get_call(call_id)
        except KeyError:
            await ws.send_json({"type": "error", "code": "call_not_found", "message": "测试电话不存在。"})
            await ws.close(code=1008)
            return
        if record["versions"] != settings.versions():
            await ws.send_json({"type": "error", "code": "version_mismatch", "message": "此通电话使用旧版架构，请点击新建通话。"})
            await ws.close(code=1008)
            return
        if call_id in app.state.voice_calls:
            await ws.send_json({"type": "error", "code": "already_connected", "message": "此通电话已有语音连接。"})
            await ws.close(code=1008)
            return
        if not settings.speech_status()["configured"]:
            await ws.send_json({"type": "error", "code": "speech_not_configured", "message": "请先在本机 .env 配置 DEEPGRAM_API_KEY，并重启服务。"})
            await ws.close(code=1008)
            return
        app.state.voice_calls.add(call_id)
        try:
            try:
                session = VoiceSession(ws, app.state.runtime, call_id, speech_factory)
            except Exception:
                await ws.send_json({"type": "error", "code": "speech_init_failed", "message": "无法初始化语音服务，请检查 Deepgram 配置。"})
                await ws.close(code=1011)
                return
            await session.run()
        finally:
            app.state.voice_calls.discard(call_id)

    return app
