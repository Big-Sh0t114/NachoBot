"""
NachoBot WebUI — FastAPI Server
Main entry point: REST API + WebSocket endpoints + static file serving.
"""

import asyncio
import json
import logging
import uvicorn
from pathlib import Path
from contextlib import asynccontextmanager

from fastapi import FastAPI, WebSocket, WebSocketDisconnect, HTTPException
from fastapi.staticfiles import StaticFiles
from fastapi.responses import FileResponse
from pydantic import BaseModel

from config_manager import ConfigManager
from process_manager import ProcessManager
from plugin_manager import PluginManager
from db_manager import DatabaseManager
from knowledge_manager import KnowledgeManager
from setup_manager import EnvironmentChecker, ConfigInitializer, DependencyInstaller, PathVerifier, NapCatConfigurator

logger = logging.getLogger("webui")

STATIC_DIR = Path(__file__).parent / "static"

# Shared instances
config_mgr = ConfigManager()
process_mgr = ProcessManager()
plugin_mgr = PluginManager()
db_mgr = DatabaseManager()
knowledge_mgr = KnowledgeManager()


@asynccontextmanager
async def lifespan(app: FastAPI):
    """Application lifecycle."""
    yield
    # Shutdown: stop all running services
    for sid in list(process_mgr.states.keys()):
        try:
            await process_mgr.stop_service(sid)
        except Exception:
            pass


app = FastAPI(title="NachoBot WebUI", lifespan=lifespan)

# Mount static files
app.mount("/static", StaticFiles(directory=str(STATIC_DIR)), name="static")


# =========================================================================
# Pages
# =========================================================================


@app.get("/nachobot.ico", include_in_schema=False)
async def favicon():
    return FileResponse(Path(__file__).parent / "nachobot.ico")


@app.get("/")
async def index():
    return FileResponse(STATIC_DIR / "index.html")


# =========================================================================
# Config API
# =========================================================================


@app.get("/api/configs")
async def list_configs():
    return config_mgr.list_configs()


@app.get("/api/configs/{file_id}")
async def get_config(file_id: str):
    try:
        data = config_mgr.read_config(file_id, mask_sensitive=True)
        raw = config_mgr.read_config_raw(file_id)
        return {"data": data, "raw": raw}
    except FileNotFoundError as e:
        raise HTTPException(404, str(e))
    except ValueError as e:
        raise HTTPException(400, str(e))


class ConfigUpdate(BaseModel):
    raw: str  # We accept raw TOML text to preserve user formatting


@app.put("/api/configs/{file_id}")
async def update_config(file_id: str, body: ConfigUpdate):
    try:
        entry = config_mgr._find(file_id)
        full = config_mgr.root / entry["path"]
        # Backup first
        config_mgr._backup(full)
        # Write raw text directly
        full.write_text(body.raw, encoding="utf-8")
        # Hot-reload configurations & services
        if file_id == "webui_config":
            from webui_config import webui_config
            webui_config.reload()

        from process_manager import _register_services
        _register_services()
        return {"status": "ok"}
    except ValueError as e:
        raise HTTPException(400, str(e))
    except Exception as e:
        raise HTTPException(500, str(e))


@app.post("/api/configs/{file_id}/backup")
async def backup_config(file_id: str):
    try:
        bak = config_mgr.backup_config(file_id)
        return {"status": "ok", "backup": bak}
    except (ValueError, FileNotFoundError) as e:
        raise HTTPException(400, str(e))


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
        asyncio.create_task(process_mgr.start_group(group_id))
        return {"status": "starting", "group": group_id}
    except (ValueError, RuntimeError) as e:
        raise HTTPException(400, str(e))


@app.post("/api/groups/{group_id}/stop")
async def stop_group(group_id: str):
    try:
        asyncio.create_task(process_mgr.stop_group(group_id))
        return {"status": "stopping", "group": group_id}
    except ValueError as e:
        raise HTTPException(400, str(e))


@app.post("/api/services/{service_id}/start")
async def start_service(service_id: str):
    try:
        asyncio.create_task(process_mgr.start_service(service_id))
        return {"status": "starting", "service": service_id}
    except ValueError as e:
        raise HTTPException(400, str(e))


@app.post("/api/services/{service_id}/stop")
async def stop_service(service_id: str):
    try:
        asyncio.create_task(process_mgr.stop_service(service_id))
        return {"status": "stopping", "service": service_id}
    except ValueError as e:
        raise HTTPException(400, str(e))


# =========================================================================
# WebSocket — Log streaming
# =========================================================================


@app.websocket("/ws/logs/{service_id}")
async def ws_logs(ws: WebSocket, service_id: str):
    await ws.accept()

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
        data = plugin_mgr.read_plugin_config(plugin_id)
        raw = plugin_mgr.read_plugin_config_raw(plugin_id)
        return {"data": data, "raw": raw}
    except FileNotFoundError as e:
        raise HTTPException(404, str(e))


class PluginConfigUpdate(BaseModel):
    raw: str


@app.put("/api/plugins/{plugin_id}/config")
async def update_plugin_config(plugin_id: str, body: PluginConfigUpdate):
    try:
        config_path = plugin_mgr.plugins_dir / plugin_id / "config.toml"
        config_path.write_text(body.raw, encoding="utf-8")
        return {"status": "ok"}
    except Exception as e:
        raise HTTPException(500, str(e))


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
        return db_mgr.get_stats()
    except Exception as e:
        raise HTTPException(500, str(e))


@app.get("/api/db/tables")
async def db_list_tables():
    try:
        return db_mgr.list_tables()
    except Exception as e:
        raise HTTPException(500, str(e))


@app.get("/api/db/tables/{table_name}")
async def db_query_table(
    table_name: str,
    page: int = 1,
    size: int = 50,
    search: str = "",
    sort_by: str = "id",
    sort_order: str = "desc",
    filters: str = "",
):
    try:
        filter_dict = None
        if filters:
            filter_dict = json.loads(filters)
        return db_mgr.query_table(table_name, page, size, search, sort_by, sort_order, filter_dict)
    except ValueError as e:
        raise HTTPException(404, str(e))
    except Exception as e:
        raise HTTPException(500, str(e))


@app.get("/api/db/tables/{table_name}/columns/{column}/values")
async def db_column_values(table_name: str, column: str):
    try:
        return db_mgr.get_column_values(table_name, column)
    except ValueError as e:
        raise HTTPException(404, str(e))
    except Exception as e:
        raise HTTPException(500, str(e))


@app.get("/api/db/tables/{table_name}/{row_id}")
async def db_get_row(table_name: str, row_id: int):
    try:
        return db_mgr.get_row(table_name, row_id)
    except ValueError as e:
        raise HTTPException(404, str(e))


class RowUpdate(BaseModel):
    data: dict


@app.put("/api/db/tables/{table_name}/{row_id}")
async def db_update_row(table_name: str, row_id: int, body: RowUpdate):
    try:
        db_mgr.update_row(table_name, row_id, body.data)
        return {"status": "ok"}
    except PermissionError as e:
        raise HTTPException(403, str(e))
    except ValueError as e:
        raise HTTPException(404, str(e))


@app.delete("/api/db/tables/{table_name}/{row_id}")
async def db_delete_row(table_name: str, row_id: int):
    try:
        db_mgr.delete_row(table_name, row_id)
        return {"status": "ok"}
    except PermissionError as e:
        raise HTTPException(403, str(e))
    except ValueError as e:
        raise HTTPException(404, str(e))


# =========================================================================
# Knowledge Base API
# =========================================================================


def _is_core_running() -> bool:
    """Check if NachoBot Core is currently running."""
    from process_manager import ServiceStatus

    state = process_mgr.states.get("nachobot")
    return state is not None and state.status in (
        ServiceStatus.RUNNING,
        ServiceStatus.STARTING,
    )


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
    except Exception as e:
        logger.exception("[Setup] generate_configs failed")
        raise HTTPException(500, str(e))


class VerifyPathRequest(BaseModel):
    type: str
    path: str = ""


@app.post("/api/setup/verify-path")
async def setup_verify_path(body: VerifyPathRequest):
    """Verify an external dependency path."""
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
    except Exception as e:
        logger.exception("[Setup] napcat configure failed")
        raise HTTPException(500, str(e))


@app.websocket("/ws/setup/install")
async def ws_setup_install(ws: WebSocket):
    """
    WebSocket for real-time dependency installation.
    Client sends: {"action": "install", "tasks": [{"id":..., "type":..., "name":..., "dir":...}, ...]}
    Server streams: {"type": "log"|"task_start"|"task_done"|"all_done", ...}
    """
    await ws.accept()

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
    from webui_config import webui_config
    uvicorn.run(
        "server:app",
        host=webui_config.host,
        port=webui_config.port,
        log_level="info",
    )
