"""
Autocrat — FastAPI Web Server
REST API + WebSocket for the control panel.
Token-based auth protects command execution endpoints.
Cross-channel: WebSocket clients can identify as "vscode" or "web" source.
Message bus integration routes results between all channels.
"""

import asyncio
import json
import os
import secrets
import tempfile
import yaml
from typing import Any, Dict, List, Optional, Set
from fastapi import FastAPI, WebSocket, WebSocketDisconnect, Header, HTTPException, Query, UploadFile, File, Form
from fastapi.staticfiles import StaticFiles
from fastapi.responses import HTMLResponse, FileResponse, StreamingResponse
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel
from nexus.core.engine import NexusEngine
from nexus.core.logger import NexusLogger

app = FastAPI(title="Autocrat", version="2.0.0")
engine: NexusEngine = None
ws_clients: Set[WebSocket] = set()
# Track client sources: ws → "vscode" | "web" | "dashboard"
ws_client_sources: Dict[WebSocket, str] = {}
AUTH_TOKEN: str = None
message_bus = None
heartbeat_ref = None
whisper_model = None

# CORS
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)

# Static files
static_dir = os.path.join(os.path.dirname(__file__), "static")
if os.path.exists(static_dir):
    app.mount("/static", StaticFiles(directory=static_dir), name="static")


class CommandRequest(BaseModel):
    command: str


class CommandResponse(BaseModel):
    success: bool
    result: Any = None
    error: str = None
    timestamp: str = None
    duration_ms: float = None


def _load_whisper_model():
    """Lazy-load faster-whisper model for voice transcription."""
    global whisper_model
    if whisper_model is not None:
        return whisper_model

    try:
        whisper_mod = __import__("faster_whisper", fromlist=["WhisperModel"])
        WhisperModel = getattr(whisper_mod, "WhisperModel")
    except Exception as e:
        raise RuntimeError("faster-whisper not installed. Run: pip install faster-whisper") from e

    cfg = {}
    config_path = "nexus_config.yaml"
    if os.path.exists(config_path):
        with open(config_path, "r", encoding="utf-8") as f:
            cfg = yaml.safe_load(f) or {}

    ai_cfg = cfg.get("ai", {})
    voice_model_name = ai_cfg.get("voice_model", "small")
    compute_type = ai_cfg.get("voice_compute_type", "int8_float16")

    whisper_model = WhisperModel(voice_model_name, device="cuda", compute_type=compute_type)
    return whisper_model


def set_engine(eng: NexusEngine):
    global engine, AUTH_TOKEN
    engine = eng

    # Load auth token from config
    config_path = "nexus_config.yaml"
    if os.path.exists(config_path):
        with open(config_path, "r") as f:
            cfg = yaml.safe_load(f)
        AUTH_TOKEN = cfg.get("auth", {}).get("token")
        if not AUTH_TOKEN:
            # Auto-generate on first run
            AUTH_TOKEN = secrets.token_urlsafe(24)
            cfg.setdefault("auth", {})["token"] = AUTH_TOKEN
            with open(config_path, "w") as f:
                yaml.dump(cfg, f, default_flow_style=False)


def set_message_bus(bus):
    """Connect the message bus for cross-channel routing."""
    global message_bus
    message_bus = bus
    # Register "web" and "vscode" channels on the bus
    bus.register_channel("web", _bus_to_ws_web)
    bus.register_channel("vscode", _bus_to_ws_vscode)


def set_heartbeat(hb):
    """Store heartbeat reference for status endpoint."""
    global heartbeat_ref
    heartbeat_ref = hb


async def _bus_to_ws_web(msg):
    """Forward bus messages to web dashboard WS clients."""
    await _forward_bus_to_ws(msg, "web")


async def _bus_to_ws_vscode(msg):
    """Forward bus messages to VS Code WS clients."""
    await _forward_bus_to_ws(msg, "vscode")


async def _forward_bus_to_ws(msg, target_source: str):
    """Send a bus message to all WS clients matching target_source."""
    result = msg.result or {}
    payload = {
        "type": "cross_channel_result",
        "data": {
            "source": msg.source,
            "command": msg.text,
            **result,
        }
    }
    dead = set()
    for client in ws_clients:
        client_src = ws_client_sources.get(client, "web")
        if client_src == target_source:
            try:
                await client.send_json(payload)
            except Exception:
                dead.add(client)
    ws_clients.difference_update(dead)
    for d in dead:
        ws_client_sources.pop(d, None)


def verify_token(authorization: Optional[str] = Header(None)):
    """Verify the Bearer token for protected endpoints."""
    if not AUTH_TOKEN:
        return  # No token configured = no auth

    if not authorization:
        raise HTTPException(status_code=401, detail="Authorization required. Send token to execute commands.")

    # Accept "Bearer <token>" or raw token
    token = authorization.replace("Bearer ", "").strip()
    if token != AUTH_TOKEN:
        raise HTTPException(status_code=401, detail="Invalid token")


# ─── Public Endpoints (read-only, no auth) ────────────────────────────────────

@app.get("/", response_class=HTMLResponse)
async def root():
    """Serve the control panel."""
    index_path = os.path.join(static_dir, "index.html")
    if os.path.exists(index_path):
        return FileResponse(index_path)
    return HTMLResponse("<h1>Autocrat</h1><p>Static files not found.</p>")


@app.get("/api/greeting")
async def get_greeting():
    """Return a JARVIS-style startup greeting for the web dashboard."""
    if engine and hasattr(engine, "personality"):
        plugins_count = len(engine.plugins) if engine.plugins else 0
        commands_count = sum(len(p.get_commands()) for p in engine.plugins.values()) if engine.plugins else 0
        brain_ready = engine.brain is not None
        greeting = engine.personality.greeting(plugins_count, commands_count, brain_ready)
        return {"greeting": greeting}
    return {"greeting": "Autocrat online. Ready for commands."}


@app.get("/api/plugins")
async def get_plugins():
    """List all loaded plugins and their commands."""
    if not engine:
        return {"success": False, "error": "Engine not initialized"}
    plugins = []
    for p in engine.plugins.values():
        plugins.append({
            "name": p.name,
            "icon": p.icon,
            "description": p.description,
            "version": p.version,
            "enabled": p.enabled,
            "commands": p.get_commands(),
        })
    return {"success": True, "result": plugins}


@app.get("/api/history")
async def get_history():
    """Get command history."""
    if not engine:
        return {"success": False, "error": "Engine not initialized"}
    return {"success": True, "result": engine.history[-100:]}


@app.get("/api/system")
async def get_system_stats():
    """Get live system stats."""
    if not engine:
        return {"success": False, "error": "Engine not initialized"}
    # Direct plugin call to avoid running the full NLP pipeline
    plugin = engine.plugins.get("system_info")
    if plugin and plugin.enabled:
        result = plugin.execute("full", {})
    else:
        result = {"success": False, "error": "system_info plugin not available"}
    return result


@app.get("/api/commands")
async def get_all_commands():
    """Get all available commands from all plugins."""
    if not engine:
        return {"success": False, "error": "Engine not initialized"}
    return {"success": True, "result": engine.get_all_commands()}


@app.get("/api/suggestions")
async def get_suggestions(q: str = ""):
    """Get AI-powered command suggestions."""
    if not engine:
        return {"success": False, "error": "Engine not initialized"}
    suggestions = []
    if q and engine.brain.is_ready:
        ai_suggestions = engine.brain.get_suggestions(q, top_k=5)
        suggestions.extend([{"type": "ai", **s} for s in ai_suggestions])
    time_suggestions = engine.learner.get_time_suggestions()
    suggestions.extend([{"type": "time", **s} for s in time_suggestions])
    if engine._last_command:
        chain_suggestions = engine.learner.get_chain_suggestions(engine._last_command)
        suggestions.extend([{"type": "chain", **s} for s in chain_suggestions])
    return {"success": True, "result": suggestions}


@app.get("/api/brain")
async def get_brain_stats():
    """Get AI brain and learning stats."""
    if not engine:
        return {"success": False, "error": "Engine not initialized"}
    return {
        "success": True,
        "result": {
            "brain_active": engine.brain._ready,
            "learning_stats": engine.learner.get_stats(),
            "frequent_commands": engine.learner.get_frequent_commands(10),
        },
    }


# ─── Auth Endpoints ───────────────────────────────────────────────────────────

@app.post("/api/auth/verify")
async def verify_auth(authorization: Optional[str] = Header(None)):
    """Check if a token is valid."""
    if not AUTH_TOKEN:
        return {"success": True, "message": "No auth configured"}
    if not authorization:
        return {"success": False, "message": "No token provided"}
    token = authorization.replace("Bearer ", "").strip()
    if token == AUTH_TOKEN:
        return {"success": True, "message": "Authenticated"}
    return {"success": False, "message": "Invalid token"}


# ─── Protected Endpoints (require auth) ───────────────────────────────────────

import asyncio

@app.post("/api/command")
async def execute_command(req: CommandRequest, authorization: Optional[str] = Header(None)):
    """Execute a text command (PROTECTED)."""
    verify_token(authorization)

    if not engine:
        return {"success": False, "error": "Engine not initialized"}
    
    # Run engine.execute in a threadpool to avoid blocking the async event loop
    # and to prevent Playwright sync_api from complaining about being in an asyncio loop
    loop = asyncio.get_running_loop()
    result = await loop.run_in_executor(None, engine.execute, req.command)

    await broadcast_ws({"type": "command_result", "data": {
        "command": req.command,
        **result,
    }})

    # Push instant alert for confirmations to mobile dashboard
    if result.get("requires_confirmation"):
        await broadcast_ws({
            "type": "alert",
            "data": {
                "kind": "confirmation_required",
                "id": result.get("confirmation_id"),
                "command": req.command,
                "reasons": result.get("reasons", []),
                "approve_command": result.get("approve_command"),
                "reject_command": result.get("reject_command"),
            },
        })

    return result


@app.get("/api/stream/chat")
async def stream_chat(q: str = "", authorization: Optional[str] = Header(None)):
    """Stream a conversational LLM response token-by-token (PROTECTED).

    Used by the web UI for real-time text rendering instead of waiting
    for the full response. Returns text/event-stream (SSE).
    """
    verify_token(authorization)
    if not engine:
        return {"success": False, "error": "Engine not initialized"}
    if not q.strip():
        return {"success": False, "error": "Empty query"}

    def generate():
        for token in engine.gemini.stream_chat(q):
            # SSE format
            yield f"data: {json.dumps({'token': token})}\n\n"
        yield "data: {\"done\": true}\n\n"

    return StreamingResponse(generate(), media_type="text/event-stream")


@app.get("/api/confirmations")
async def list_confirmations(authorization: Optional[str] = Header(None)):
    """Get pending destructive-action confirmations (PROTECTED)."""
    verify_token(authorization)
    if not engine:
        return {"success": False, "error": "Engine not initialized"}
    return {"success": True, "result": engine.get_pending_confirmations()}


@app.post("/api/voice")
async def voice_command(
    audio: UploadFile = File(...),
    confirmation_id: str = Form(""),
    authorization: Optional[str] = Header(None),
):
    """
    Upload voice audio, transcribe on PC, then execute as command.
    If a pending confirmation exists, yes/no phrases auto-map to approve/reject.
    """
    verify_token(authorization)

    if not engine:
        return {"success": False, "error": "Engine not initialized"}

    suffix = os.path.splitext(audio.filename or "voice.webm")[-1] or ".webm"
    with tempfile.NamedTemporaryFile(delete=False, suffix=suffix) as temp_file:
        temp_path = temp_file.name
        temp_file.write(await audio.read())

    try:
        model = _load_whisper_model()
        segments, info = model.transcribe(temp_path, beam_size=1, vad_filter=True)
        transcription = " ".join((s.text or "").strip() for s in segments).strip()
    finally:
        try:
            os.remove(temp_path)
        except Exception:
            pass

    if not transcription:
        return {"success": False, "error": "Could not transcribe audio"}

    # Voice yes/no resolver for pending confirmations
    resolved_cmd = engine.resolve_confirmation_phrase(transcription, confirmation_id=confirmation_id)
    final_command = resolved_cmd or transcription

    result = engine.execute(final_command)

    await broadcast_ws({
        "type": "voice_result",
        "data": {
            "transcription": transcription,
            "resolved_command": final_command,
            **result,
        },
    })

    if result.get("requires_confirmation"):
        await broadcast_ws({
            "type": "alert",
            "data": {
                "kind": "confirmation_required",
                "id": result.get("confirmation_id"),
                "command": final_command,
                "reasons": result.get("reasons", []),
                "approve_command": result.get("approve_command"),
                "reject_command": result.get("reject_command"),
            },
        })

    return {
        "success": True,
        "transcription": transcription,
        "resolved_command": final_command,
        "result": result,
    }


# ─── WebSocket ────────────────────────────────────────────────────────────────

@app.websocket("/ws")
async def websocket_endpoint(ws: WebSocket, token: Optional[str] = Query(None),
                             source: Optional[str] = Query("web")):
    """
    WebSocket with optional token auth for command execution.
    Query params:
      ?token=<auth_token>   — authenticates for command execution
      ?source=vscode|web    — identifies this client for cross-channel routing
    """
    await ws.accept()
    ws_clients.add(ws)
    client_source = source or "web"
    ws_client_sources[ws] = client_source

    # Check if this client is authenticated (via ?token= query param)
    is_authed = (not AUTH_TOKEN) or (token == AUTH_TOKEN)

    nexus_logger = NexusLogger()

    async def send_log(entry):
        try:
            await ws.send_json({"type": "log", "data": entry})
        except Exception:
            pass

    def log_callback(entry):
        try:
            loop = asyncio.get_running_loop()
            loop.create_task(send_log(entry))
        except Exception:
            pass

    nexus_logger.add_ws_listener(log_callback)

    try:
        while True:
            data = await ws.receive_text()
            try:
                msg = json.loads(data)
                msg_type = msg.get("type", "command")

                if msg_type == "identify":
                    # Client identifies itself: {"type": "identify", "source": "vscode"}
                    ws_client_sources[ws] = msg.get("source", client_source)
                    await ws.send_json({"type": "identified", "source": ws_client_sources[ws]})

                elif msg_type == "command":
                    if not is_authed:
                        await ws.send_json({"type": "command_result", "data": {
                            "command": msg.get("data", ""),
                            "success": False,
                            "error": "Authentication required to execute commands",
                        }})
                        continue

                    cmd_text = msg.get("data", "")
                    # Route through message bus if available (enables cross-channel)
                    if message_bus:
                        bus_msg = message_bus.send(
                            text=cmd_text,
                            source=ws_client_sources.get(ws, "web"),
                            user=msg.get("user", "WebSocket"),
                        )
                        result = bus_msg.result
                    else:
                        result = engine.execute(cmd_text)

                    await ws.send_json({"type": "command_result", "data": {
                        "command": cmd_text,
                        **result,
                    }})

                    if result.get("requires_confirmation"):
                        await ws.send_json({
                            "type": "alert",
                            "data": {
                                "kind": "confirmation_required",
                                "id": result.get("confirmation_id"),
                                "command": cmd_text,
                                "reasons": result.get("reasons", []),
                                "approve_command": result.get("approve_command"),
                                "reject_command": result.get("reject_command"),
                            },
                        })

                elif msg_type == "ping":
                    await ws.send_json({"type": "pong"})

            except json.JSONDecodeError:
                if engine and is_authed:
                    if message_bus:
                        bus_msg = message_bus.send(
                            text=data,
                            source=ws_client_sources.get(ws, "web"),
                            user="WebSocket",
                        )
                        result = bus_msg.result
                    else:
                        result = engine.execute(data)
                    await ws.send_json({"type": "command_result", "data": {
                        "command": data,
                        **result,
                    }})

                    if result.get("requires_confirmation"):
                        await ws.send_json({
                            "type": "alert",
                            "data": {
                                "kind": "confirmation_required",
                                "id": result.get("confirmation_id"),
                                "command": data,
                                "reasons": result.get("reasons", []),
                                "approve_command": result.get("approve_command"),
                                "reject_command": result.get("reject_command"),
                            },
                        })
                elif not is_authed:
                    await ws.send_json({"type": "command_result", "data": {
                        "success": False,
                        "error": "Authentication required",
                    }})
    except WebSocketDisconnect:
        pass
    finally:
        ws_clients.discard(ws)
        ws_client_sources.pop(ws, None)
        nexus_logger.remove_ws_listener(log_callback)


async def broadcast_ws(message: dict):
    """Broadcast a message to all WebSocket clients."""
    dead = set()
    for client in ws_clients:
        try:
            await client.send_json(message)
        except Exception:
            dead.add(client)
    ws_clients.difference_update(dead)
    for d in dead:
        ws_client_sources.pop(d, None)


# ─── Cross-Channel & Heartbeat Endpoints ──────────────────────────────────────

@app.get("/api/bus/history")
async def bus_history(limit: int = 50):
    """Get message bus history (cross-channel commands)."""
    if not message_bus:
        return {"success": False, "error": "Message bus not active"}
    return {"success": True, "result": message_bus.get_history(limit)}


@app.get("/api/bus/stats")
async def bus_stats():
    """Get message bus channel stats."""
    if not message_bus:
        return {"success": False, "error": "Message bus not active"}
    return {"success": True, "result": message_bus.get_stats()}


@app.get("/api/heartbeat")
async def heartbeat_status():
    """Get heartbeat task status."""
    if not heartbeat_ref:
        return {"success": False, "error": "Heartbeat not active"}
    return {"success": True, "result": heartbeat_ref.get_status()}


@app.post("/api/heartbeat/task")
async def add_heartbeat_task(
    name: str = "",
    command: str = "",
    interval_minutes: int = 60,
    authorization: Optional[str] = Header(None),
):
    """Add a new heartbeat task (PROTECTED)."""
    verify_token(authorization)
    if not heartbeat_ref:
        return {"success": False, "error": "Heartbeat not active"}
    if not name or not command:
        return {"success": False, "error": "name and command are required"}
    task = heartbeat_ref.add_task(name, command, interval_minutes=interval_minutes)
    return {"success": True, "result": f"Task '{name}' scheduled every {interval_minutes} min"}
