"""
NachoBot WebUI — FastAPI Server
Main entry point: REST API + WebSocket endpoints + static file serving.
"""

import asyncio
import json
import logging
import socket
import uvicorn
from pathlib import Path
from contextlib import asynccontextmanager
from urllib.parse import urlparse

from fastapi import FastAPI, WebSocket, WebSocketDisconnect, HTTPException, Query, Request
from fastapi.staticfiles import StaticFiles
from fastapi.responses import FileResponse
from pydantic import BaseModel

from config_manager import ConfigManager
from process_manager import ProcessManager
from plugin_manager import PluginManager
from db_manager import DatabaseManager
from knowledge_manager import KnowledgeManager
from memory_manager import is_available as memory_is_available
import memory_manager
from music_library import build_music_playlist
from chat_backend import ChatBackendError, chat_backend
from tts_manager import TTSGenerationError, TTSManager, TTSUnavailableError
from setup_manager import EnvironmentChecker, ConfigInitializer, DependencyInstaller, PathVerifier, NapCatConfigurator
from security import WebUISecurity, validate_webui_config_raw
from webui_config import CONFIG_PATH, webui_config

logger = logging.getLogger("webui")


def _log_safe(value: object, max_len: int = 200) -> str:
    text = str(value)
    text = text.replace("\r", "\\r").replace("\n", "\\n")
    text = "".join(ch if ch >= " " and ch != "\x7f" else "?" for ch in text)
    if len(text) > max_len:
        return text[:max_len].rstrip() + "...[truncated]"
    return text


STATIC_DIR = Path(__file__).parent / "static"

# Shared instances
config_mgr = ConfigManager()
process_mgr = ProcessManager()
plugin_mgr = PluginManager()
db_mgr = DatabaseManager()
knowledge_mgr = KnowledgeManager()
tts_mgr = TTSManager()
webui_security = WebUISecurity(webui_config)


def _validate_webui_config_raw(raw: str) -> None:
    """Validate the effective WebUI bind/auth pair before it reaches disk."""
    try:
        validate_webui_config_raw(
            raw,
            runtime_bind_host=webui_security.runtime_bind_host,
        )
    except RuntimeError as exc:
        raise ValueError(str(exc)) from exc


@asynccontextmanager
async def lifespan(app: FastAPI):
    """Application lifecycle."""
    # Validate the file itself before trusting the parsed/merged view.  This
    # catches legacy persisted auth_token values even when the current bind is
    # loopback, and protects startup from a raw config that hot-reload never
    # touched.
    if CONFIG_PATH.exists():
        validate_webui_config_raw(
            CONFIG_PATH.read_text(encoding="utf-8"),
            runtime_bind_host=webui_security.runtime_bind_host,
        )
    webui_security.ensure_safe_bind()
    await tts_mgr.start()
    try:
        yield
    finally:
        await tts_mgr.close()
        await chat_backend.close()
        await process_mgr.shutdown()


app = FastAPI(title="NachoBot WebUI", lifespan=lifespan)


@app.middleware("http")
async def protect_control_api(request: Request, call_next):
    rejection = webui_security.authorize_http(request)
    if rejection is not None:
        return rejection
    return await call_next(request)

# Mount static and resources files
app.mount("/static", StaticFiles(directory=str(STATIC_DIR)), name="static")

RESOURCES_DIR = Path(__file__).parent / "resources"
if RESOURCES_DIR.exists():
    app.mount("/resources", StaticFiles(directory=str(RESOURCES_DIR)), name="resources")

# =========================================================================
# Pages
# =========================================================================


@app.get("/nachobot.ico", include_in_schema=False)
async def favicon():
    return FileResponse(Path(__file__).parent / "nachobot.ico")


@app.get("/")
async def index():
    return FileResponse(STATIC_DIR / "index.html")

@app.get("/api/music/list")
async def list_music():
    return build_music_playlist(RESOURCES_DIR)


@app.get("/api/webui/info")
async def webui_info():
    from webui_config import webui_config

    return {"version": webui_config.version}


# =========================================================================
# Config API
# =========================================================================


@app.get("/api/configs")
async def list_configs():
    return config_mgr.list_configs()


@app.get("/api/configs/{file_id}")
async def get_config(file_id: str):
    try:
        raw = config_mgr.read_config_raw(file_id)
    except FileNotFoundError as e:
        raise HTTPException(404, str(e))
    except ValueError as e:
        raise HTTPException(400, str(e))

    try:
        data = config_mgr.read_config(file_id, mask_sensitive=True)
    except Exception as e:
        logger.warning("Failed to parse config %s: %s", _log_safe(file_id), e)
        data = None

    return {
        "data": data,
        "raw": raw,
    }


class ConfigUpdate(BaseModel):
    raw: str  # We accept raw TOML text to preserve user formatting


@app.put("/api/configs/{file_id}")
async def update_config(file_id: str, body: ConfigUpdate):
    try:
        if file_id != "env":
            import tomlkit
            try:
                tomlkit.parse(body.raw)
            except Exception as e:
                raise HTTPException(400, f"配置存在错误，保存被拒绝 {e}")
        validator = _validate_webui_config_raw if file_id == "webui_config" else None
        config_mgr.write_config_raw(file_id, body.raw, validator=validator)
        # Hot-reload configurations & services
        if file_id == "webui_config":
            webui_config.reload()

        from process_manager import _register_services
        _register_services()
        return {"status": "ok"}
    except HTTPException:
        raise
    except ValueError as e:
        raise HTTPException(400, str(e))
    except Exception:
        logger.exception("Config update failed: file_id=%s", _log_safe(file_id))
        raise HTTPException(500, "配置保存失败")


@app.post("/api/configs/{file_id}/backup")
async def backup_config(file_id: str):
    try:
        bak = config_mgr.backup_config(file_id)
        if not bak:
            raise ValueError("当前配置文件包含语法错误，为了防止污染记录，已拒绝将其备份")
        return {"status": "ok", "backup": bak}
    except (ValueError, FileNotFoundError) as e:
        raise HTTPException(400, str(e))

@app.get("/api/configs/{file_id}/backups")
async def list_config_backups(file_id: str):
    try:
        backups = config_mgr.list_backups(file_id)
        return {"status": "ok", "backups": backups}
    except ValueError as e:
        raise HTTPException(400, str(e))
    except Exception as e:
        raise HTTPException(500, f"获取备份列表失败: {e}")

class RestoreBackupRequest(BaseModel):
    backup_file: str

@app.post("/api/configs/{file_id}/restore")
async def restore_config_backup(file_id: str, body: RestoreBackupRequest):
    try:
        validator = _validate_webui_config_raw if file_id == "webui_config" else None
        bak_name = config_mgr.restore_backup(
            file_id,
            body.backup_file,
            validator=validator,
        )
        if file_id == "webui_config":
            webui_config.reload()
        from process_manager import _register_services
        _register_services()
        return {"status": "ok", "backup": bak_name}
    except FileNotFoundError as e:
        raise HTTPException(404, str(e))
    except ValueError as e:
        raise HTTPException(400, str(e))
    except Exception as e:
        logger.exception("Restore failed")
        raise HTTPException(500, f"恢复备份失败: {e}")


# =========================================================================
# Service / Group API
# =========================================================================


@app.get("/api/groups")
async def get_groups():
    return process_mgr.get_group_statuses()


@app.get("/api/services")
async def get_services():
    return process_mgr.get_all_statuses()


@app.post("/api/groups/{group_id}/start")
async def start_group(group_id: str):
    try:
        process_mgr.request_start_group(group_id)
        return {"status": "starting", "group": group_id}
    except (ValueError, RuntimeError) as e:
        raise HTTPException(400, str(e))


@app.post("/api/groups/{group_id}/stop")
async def stop_group(group_id: str):
    try:
        process_mgr.request_stop_group(group_id)
        return {"status": "stopping", "group": group_id}
    except ValueError as e:
        raise HTTPException(400, str(e))


@app.post("/api/services/{service_id}/start")
async def start_service(service_id: str):
    try:
        process_mgr.request_start_service(service_id)
        return {"status": "starting", "service": service_id}
    except (ValueError, RuntimeError) as e:
        raise HTTPException(400, str(e))


@app.post("/api/services/{service_id}/stop")
async def stop_service(service_id: str):
    try:
        process_mgr.request_stop_service(service_id)
        return {"status": "stopping", "service": service_id}
    except (ValueError, RuntimeError) as e:
        raise HTTPException(400, str(e))


class ServiceInput(BaseModel):
    text: str


@app.post("/api/services/{service_id}/input")
async def send_service_input(service_id: str, body: ServiceInput):
    try:
        await process_mgr.send_input(service_id, body.text)
        return {"status": "ok"}
    except ValueError as e:
        raise HTTPException(400, str(e))


# =========================================================================
# Chat API
# =========================================================================


class ChatMessageRequest(BaseModel):
    conversation_id: str = ""
    message: str
    request_message_id: str = ""
    user_id: str = "webui-user"
    user_name: str = "WebUI"


class ChatTTSRequest(BaseModel):
    text: str


@app.get("/api/chat/status")
async def chat_status():
    core_status = _get_core_status()
    result = await chat_backend.status(core_running=core_status == "running")
    result["core_status"] = core_status
    return result


@app.get("/api/chat/tts/status")
async def chat_tts_status():
    return await tts_mgr.status()


@app.post("/api/chat/tts")
async def chat_tts(body: ChatTTSRequest):
    try:
        audio_path, cache_hit = await tts_mgr.generate(body.text)
        return FileResponse(
            audio_path,
            media_type="audio/wav",
            headers={
                "Cache-Control": "private, max-age=86400",
                "X-TTS-Cache": "HIT" if cache_hit else "MISS",
            },
        )
    except ValueError as exc:
        raise HTTPException(400, str(exc))
    except TTSUnavailableError as exc:
        raise HTTPException(503, str(exc))
    except TTSGenerationError as exc:
        raise HTTPException(502, str(exc))


@app.post("/api/chat/message")
async def chat_message(body: ChatMessageRequest):
    if not _is_core_running():
        raise HTTPException(503, "NachoBot Core 未运行，请先启动核心服务")
    try:
        return await chat_backend.send_message(
            conversation_id=body.conversation_id,
            text=body.message,
            user_id=body.user_id,
            user_name=body.user_name,
            request_message_id=body.request_message_id,
        )
    except ChatBackendError as e:
        raise HTTPException(e.status_code, str(e))


@app.delete("/api/chat/conversations/{conversation_id}")
async def delete_chat_conversation(conversation_id: str):
    """Delete one WebUI conversation and its Core-side local identity data."""
    conversation_id = str(conversation_id or "").strip()
    if not conversation_id:
        raise HTTPException(400, "conversation_id 不能为空")

    try:
        backend_user_id = chat_backend.resolve_webui_user_id(conversation_id)
        result = await asyncio.to_thread(
            db_mgr.delete_webui_conversation,
            conversation_id,
            backend_user_id,
        )
        chat_backend.forget_conversation(conversation_id)
        logger.info(
            "Deleted WebUI conversation %s: backend_user_id=%s, deleted_rows=%s",
            _log_safe(conversation_id),
            _log_safe(backend_user_id),
            result.get("deleted_rows", 0),
        )
        return result
    except ValueError as e:
        raise HTTPException(409, str(e))
    except Exception:
        logger.exception("Failed to delete WebUI conversation %s", _log_safe(conversation_id))
        raise HTTPException(500, "删除会话数据库记录失败")


@app.websocket("/ws/chat/{conversation_id}")
async def ws_chat(ws: WebSocket, conversation_id: str):
    """Push each streamed Core reply into the matching WebUI conversation."""
    if not await webui_security.authorize_websocket(ws):
        return
    queue = chat_backend.subscribe(conversation_id)

    try:
        while True:
            event_task = asyncio.create_task(queue.get())
            receive_task = asyncio.create_task(ws.receive())
            done, pending = await asyncio.wait(
                {event_task, receive_task}, return_when=asyncio.FIRST_COMPLETED
            )
            for task in pending:
                task.cancel()
            await asyncio.gather(*pending, return_exceptions=True)

            if receive_task in done:
                received = receive_task.result()
                if received["type"] == "websocket.disconnect":
                    break
            if event_task in done:
                event = event_task.result()
                await ws.send_json(event)
                chat_backend.acknowledge_live_delivery(
                    conversation_id,
                    str(event.get("message_id") or ""),
                )
    except WebSocketDisconnect:
        pass
    finally:
        chat_backend.unsubscribe(conversation_id, queue)


# =========================================================================
# WebSocket — Log streaming
# =========================================================================


@app.websocket("/ws/logs/{service_id}")
async def ws_logs(ws: WebSocket, service_id: str):
    if not await webui_security.authorize_websocket(ws):
        return

    # Send historical logs
    history = process_mgr.get_log_history(service_id)
    if history:
        await ws.send_text(json.dumps({"type": "history", "lines": history}))

    # Define callback
    queue: asyncio.Queue = asyncio.Queue()

    async def on_line(line: str):
        await queue.put(line)

    process_mgr.subscribe(service_id, on_line)

    try:
        while True:
            try:
                line = await asyncio.wait_for(queue.get(), timeout=30)
                await ws.send_text(json.dumps({"type": "log", "line": line}))
            except asyncio.TimeoutError:
                # Send keepalive
                await ws.send_text(json.dumps({"type": "ping"}))
    except WebSocketDisconnect:
        pass
    except Exception:
        pass
    finally:
        process_mgr.unsubscribe(service_id, on_line)


# =========================================================================
# Plugin API
# =========================================================================


@app.get("/api/plugins")
async def list_plugins():
    return plugin_mgr.list_plugins()


@app.get("/api/plugins/{plugin_id}/config")
async def get_plugin_config(plugin_id: str):
    try:
        raw = plugin_mgr.read_plugin_config_raw(plugin_id)
    except FileNotFoundError as e:
        raise HTTPException(404, str(e))
    except ValueError as e:
        raise HTTPException(400, str(e))

    try:
        data = plugin_mgr.read_plugin_config(plugin_id)
    except Exception as e:
        logger.warning("Failed to parse plugin config %s: %s", _log_safe(plugin_id), e)
        data = None

    return {
        "data": data,
        "raw": raw,
    }


class PluginConfigUpdate(BaseModel):
    raw: str


@app.put("/api/plugins/{plugin_id}/config")
async def update_plugin_config(plugin_id: str, body: PluginConfigUpdate):
    try:
        import tomlkit
        try:
            tomlkit.parse(body.raw)
        except Exception as e:
            raise HTTPException(400, f"插件配置存在错误，保存被拒绝 {e}")

        plugin_mgr.write_plugin_config_raw(plugin_id, body.raw)
        return {"status": "ok"}
    except HTTPException:
        raise
    except (FileNotFoundError, ValueError) as e:
        raise HTTPException(400, str(e))
    except Exception:
        logger.exception("Plugin config update failed: plugin_id=%s", _log_safe(plugin_id))
        raise HTTPException(500, "插件配置保存失败")


# =========================================================================
# Status polling endpoint (lightweight)
# =========================================================================


@app.get("/api/status")
async def get_status():
    """Quick status snapshot for the status bar."""
    groups = process_mgr.get_group_statuses()
    summary = {}
    for g in groups:
        running = sum(1 for s in g["services"] if s["status"] == "running")
        total = len(g["services"])
        summary[g["id"]] = {
            "name": g["name"],
            "icon": g["icon"],
            "running": running,
            "total": total,
        }
    return summary


# =========================================================================
# Database API
# =========================================================================


@app.get("/api/db/stats")
async def db_stats():
    try:
        return await asyncio.to_thread(db_mgr.get_stats)
    except Exception:
        logger.exception("Database stats failed")
        raise HTTPException(500, "数据库统计读取失败")


@app.get("/api/db/tables")
async def db_list_tables():
    try:
        return await asyncio.to_thread(db_mgr.list_tables)
    except Exception:
        logger.exception("Database table list failed")
        raise HTTPException(500, "数据库表列表读取失败")


@app.get("/api/db/tables/{table_name}")
async def db_query_table(
    table_name: str,
    page: int = Query(1, ge=1, le=1_000_000),
    size: int = Query(50, ge=1, le=200),
    search: str = "",
    sort_by: str = "id",
    sort_order: str = "desc",
    filters: str = "",
):
    try:
        filter_dict = None
        if filters:
            filter_dict = json.loads(filters)
            if not isinstance(filter_dict, dict):
                raise ValueError("filters 必须是 JSON 对象")
        return await asyncio.to_thread(
            db_mgr.query_table,
            table_name,
            page,
            size,
            search,
            sort_by,
            sort_order,
            filter_dict,
        )
    except json.JSONDecodeError as e:
        raise HTTPException(400, f"filters 不是有效 JSON: {e.msg}")
    except ValueError as e:
        raise HTTPException(400, str(e))
    except Exception:
        logger.exception("Database table query failed: table=%s", _log_safe(table_name))
        raise HTTPException(500, "数据库表查询失败")


@app.get("/api/db/tables/{table_name}/columns/{column}/values")
async def db_column_values(table_name: str, column: str):
    try:
        return await asyncio.to_thread(db_mgr.get_column_values, table_name, column)
    except ValueError as e:
        raise HTTPException(404, str(e))
    except Exception:
        logger.exception(
            "Database column values query failed: table=%s column=%s",
            _log_safe(table_name),
            _log_safe(column),
        )
        raise HTTPException(500, "数据库列值读取失败")


@app.get("/api/db/tables/{table_name}/{row_id}")
async def db_get_row(table_name: str, row_id: int):
    try:
        return await asyncio.to_thread(db_mgr.get_row, table_name, row_id)
    except ValueError as e:
        raise HTTPException(404, str(e))


class RowUpdate(BaseModel):
    data: dict


@app.put("/api/db/tables/{table_name}/{row_id}")
async def db_update_row(table_name: str, row_id: int, body: RowUpdate):
    try:
        await asyncio.to_thread(db_mgr.update_row, table_name, row_id, body.data)
        return {"status": "ok"}
    except PermissionError as e:
        raise HTTPException(403, str(e))
    except ValueError as e:
        raise HTTPException(404, str(e))


@app.delete("/api/db/tables/{table_name}/{row_id}")
async def db_delete_row(table_name: str, row_id: int):
    try:
        await asyncio.to_thread(db_mgr.delete_row, table_name, row_id)
        return {"status": "ok"}
    except PermissionError as e:
        raise HTTPException(403, str(e))
    except ValueError as e:
        raise HTTPException(404, str(e))


# =========================================================================
# Knowledge Base API
# =========================================================================


def _get_core_status() -> str:
    """Return stopped/starting/running/stopping/error for NachoBot Core."""
    from process_manager import ServiceStatus

    state = process_mgr.states.get("nachobot")
    if state is not None:
        if state.status == ServiceStatus.STARTING:
            return "starting"
        if state.status == ServiceStatus.STOPPING:
            return "stopping"
        if state.status == ServiceStatus.ERROR:
            return "error"
        if state.status == ServiceStatus.STOPPED:
            return "stopped"

    try:
        parsed = urlparse(memory_manager._get_core_base_url())
        host = parsed.hostname or "127.0.0.1"
        if parsed.port is None:
            return "starting" if state is not None else "stopped"
        with socket.create_connection((host, parsed.port), timeout=0.3):
            return "running"
    except (OSError, RuntimeError):
        if state is not None and state.status == ServiceStatus.RUNNING:
            return "starting"
        return "stopped"


def _is_core_running() -> bool:
    """Check whether NachoBot Core has finished starting and accepts connections."""
    return _get_core_status() == "running"


@app.get("/api/knowledge/files")
async def knowledge_list_files():
    return knowledge_mgr.list_files()


@app.get("/api/knowledge/files/{filename}")
async def knowledge_read_file(filename: str):
    try:
        content = knowledge_mgr.read_file(filename)
        return {
            "filename": filename,
            "content": content,
            "core_running": _is_core_running(),
        }
    except FileNotFoundError as e:
        raise HTTPException(404, str(e))


class KnowledgeFileUpdate(BaseModel):
    content: str


@app.put("/api/knowledge/files/{filename}")
async def knowledge_update_file(filename: str, body: KnowledgeFileUpdate):
    if _is_core_running():
        raise HTTPException(
            409, "NachoBot Core 正在运行，请先停止核心后再编辑知识库文件"
        )
    try:
        knowledge_mgr.update_file(filename, body.content)
        return {"status": "ok"}
    except FileNotFoundError as e:
        raise HTTPException(404, str(e))


class KnowledgeFileCreate(BaseModel):
    filename: str
    content: str = ""


@app.post("/api/knowledge/files")
async def knowledge_create_file(body: KnowledgeFileCreate):
    if _is_core_running():
        raise HTTPException(
            409, "NachoBot Core 正在运行，请先停止核心后再新建知识库文件"
        )
    try:
        knowledge_mgr.create_file(body.filename, body.content)
        return {"status": "ok"}
    except ValueError as e:
        raise HTTPException(400, str(e))


@app.get("/api/knowledge/stats")
async def knowledge_stats():
    return knowledge_mgr.get_stats()


# =========================================================================
# Memory API (A_Memorix)
# =========================================================================


@app.get("/api/memory/status")
async def memory_status():
    """Check if A_Memorix is enabled."""
    return {"enabled": memory_is_available(), "core_running": _is_core_running()}


@app.get("/api/memory/stats")
async def memory_stats():
    """Get memory store statistics."""
    try:
        return await memory_manager.get_stats(core_running=_is_core_running())
    except Exception:
        logger.exception("Memory stats endpoint failed")
        raise HTTPException(500, "长期记忆统计失败")


class MemorySearchRequest(BaseModel):
    query: str
    chat_id: str = ""
    limit: int = 10


@app.post("/api/memory/search")
async def memory_search(body: MemorySearchRequest):
    """Search long-term memories."""
    try:
        return await memory_manager.search_memory(
            query=body.query,
            chat_id=body.chat_id,
            limit=body.limit,
            core_running=_is_core_running(),
        )
    except Exception:
        logger.exception("Memory search endpoint failed")
        raise HTTPException(500, "长期记忆检索失败")


class MemoryMaintainRequest(BaseModel):
    action: str  # reinforce / protect / restore / freeze
    target: str = ""
    reason: str = ""


@app.post("/api/memory/maintain")
async def memory_maintain(body: MemoryMaintainRequest):
    """Execute a memory maintenance action."""
    try:
        return await memory_manager.maintain(
            action=body.action,
            target=body.target,
            reason=body.reason,
            core_running=_is_core_running(),
        )
    except Exception:
        logger.exception("Memory maintain endpoint failed")
        raise HTTPException(500, "长期记忆维护失败")


# =========================================================================
# Setup Wizard API
# =========================================================================


@app.get("/api/setup/check")
async def setup_check_env():
    """Run all environment checks."""
    return EnvironmentChecker.check_all()


@app.get("/api/setup/configs/status")
async def setup_config_status():
    """Check which config files exist / are missing."""
    return ConfigInitializer.get_status()


@app.get("/api/setup/configs/defaults")
async def setup_config_defaults():
    """Return default config values from template files for pre-filling the wizard form."""
    return ConfigInitializer.get_defaults()


class SetupWizardData(BaseModel):
    components: list[str] = []
    core: dict = {}
    providers: list[dict] = []
    models: list[dict] = []
    tts: dict = {}
    env: dict = {}


@app.post("/api/setup/configs/generate")
async def setup_generate_configs(body: SetupWizardData):
    """Generate config files from templates using wizard form data."""
    try:
        data = body.model_dump()
        logger.info(
            "[Setup] generate_configs called: components=%s, providers=%d, models=%d",
            data.get("components"), len(data.get("providers", [])), len(data.get("models", []))
        )
        result = ConfigInitializer.generate_configs(data)
        logger.info(
            "[Setup] generate_configs result: generated=%d, errors=%s",
            len(result.get("generated", [])), result.get("errors", [])
        )
        return result
    except Exception:
        logger.exception("[Setup] generate_configs failed")
        raise HTTPException(500, "配置生成失败")


class VerifyPathRequest(BaseModel):
    type: str
    path: str = ""


@app.post("/api/setup/verify-path")
async def setup_verify_path(body: VerifyPathRequest):
    """Verify a setup dependency or project-managed runtime."""
    result = PathVerifier.verify_path(body.type, body.path)
    return result


@app.get("/api/setup/deps/tasks")
async def setup_dep_tasks(components: str = ""):
    """Return install tasks for selected components."""
    comp_list = [c.strip() for c in components.split(",") if c.strip()]
    return DependencyInstaller.get_install_tasks(comp_list)


class NapCatConfigRequest(BaseModel):
    napcat_dir: str
    qq_account: str = ""


@app.post("/api/setup/napcat/configure")
async def setup_configure_napcat(body: NapCatConfigRequest):
    """Auto-configure NapCat onebot11 WebSocket client + HTTP servers."""
    try:
        result = NapCatConfigurator.configure(body.napcat_dir, body.qq_account)
        logger.info(
            "[Setup] napcat configure: configured=%s, skipped=%s, errors=%s",
            result["configured"], result["skipped"], result["errors"]
        )
        return result
    except Exception:
        logger.exception("[Setup] napcat configure failed")
        raise HTTPException(500, "NapCat 配置失败")


@app.websocket("/ws/setup/install")
async def ws_setup_install(ws: WebSocket):
    """
    WebSocket for real-time dependency installation.
    Client sends: {"action": "install", "tasks": [{"id":..., "type":..., "name":..., "dir":...}, ...]}
    Server streams: {"type": "log"|"task_start"|"task_done"|"all_done", ...}
    """
    if not await webui_security.authorize_websocket(ws):
        return

    try:
        raw = await ws.receive_text()
        msg = json.loads(raw)
        tasks = msg.get("tasks", [])

        for task in tasks:
            await ws.send_text(json.dumps({
                "type": "task_start",
                "task_id": task["id"],
                "name": task["name"],
            }))

            async def on_line(line: str):
                try:
                    await ws.send_text(json.dumps({
                        "type": "log",
                        "task_id": task["id"],
                        "line": line,
                    }))
                except Exception:
                    pass

            result = await DependencyInstaller.install(task, callback=on_line)

            await ws.send_text(json.dumps({
                "type": "task_done",
                "task_id": task["id"],
                "status": result["status"],
                "message": result["message"],
            }))

        await ws.send_text(json.dumps({"type": "all_done"}))

    except WebSocketDisconnect:
        pass
    except Exception as e:
        try:
            await ws.send_text(json.dumps({"type": "error", "message": str(e)}))
        except Exception:
            pass


# =========================================================================
# Run
# =========================================================================

if __name__ == "__main__":
    if CONFIG_PATH.exists():
        validate_webui_config_raw(
            CONFIG_PATH.read_text(encoding="utf-8"),
            runtime_bind_host=webui_security.runtime_bind_host,
        )
    webui_security.ensure_safe_bind()
    uvicorn.run(
        "server:app",
        host=webui_config.host,
        port=webui_config.port,
        log_level="info",
    )
