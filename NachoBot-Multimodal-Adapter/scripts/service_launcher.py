"""Ensure standalone model-service consoles, independent of the desktop pet."""

from __future__ import annotations

import argparse
import ctypes
import json
import os
import socket
import subprocess
import sys
import time
import tomllib
import urllib.request
import uuid
from pathlib import Path

try:
    from .service_process import WindowsJob, SessionOwner
except ImportError:
    from service_process import WindowsJob, SessionOwner

ROOT = Path(__file__).resolve().parents[1]


class ServiceLock:
    def __init__(self, path):
        self.path = path
        self.file = None

    def acquire(self):
        import msvcrt

        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.file = self.path.open("a+b")
        self.file.write(b"0")
        self.file.flush()
        self.file.seek(0)
        try:
            msvcrt.locking(self.file.fileno(), msvcrt.LK_NBLCK, 1)
        except OSError:
            self.close()
            return False
        return True

    def close(self):
        if self.file is not None:
            self.file.close()
            self.file = None


def settings(root=ROOT):
    with (root / "startup.toml").open("rb") as file:
        config = tomllib.load(file)
    components = config.get("components", {})
    for name in ("core", "tts", "perception"):
        if type(components.get(name, name == "core")) is not bool:
            raise ValueError(f"startup.toml: components.{name} must be boolean")
    if components.get("core", True) is False:
        print("[Standalone] Core disabled; TTS and perception startup skipped.", flush=True)
        return []
    profile = config.get("runtime", {}).get("profile", "gpu")
    if profile not in ("gpu", "cpu"):
        raise ValueError("startup.toml: runtime.profile must be gpu or cpu")
    python = root / (".venv" if profile == "gpu" else ".venv-cpu") / "Scripts" / "python.exe"
    services = []
    for name, file, section, default_port in (
        ("tts", "base.toml", "server", 9880),
        ("perception", "perception.toml", "perception", 9874),
    ):
        if not components.get(name, False):
            continue
        with (root / "configs" / file).open("rb") as stream:
            document = tomllib.load(stream)
        endpoint = document.get(section, {})
        host, port = endpoint.get("host", "127.0.0.1"), endpoint.get("port", default_port)
        if host not in ("127.0.0.1", "localhost", "0.0.0.0", "::1", "::"):
            raise ValueError(f"{name}: local service host required")
        if type(port) is not int or not 1 <= port <= 65535:
            raise ValueError(f"{name}: invalid port")
        command = (
            [str(python), "-u", "scripts/container_tts_entrypoint.py", "--host", host, "--port", str(port)]
            if name == "tts"
            else [str(python), "-u", "-m", "nachobot_multimodal.api_server"]
        )
        services.append(dict(name=name, host=host, port=port, command=command))
    if services and not python.is_file():
        raise ValueError(f"Multimodal environment missing: {python}; prepare it through WebUI first")
    return services


def probe(service):
    host = service["host"]
    host = "127.0.0.1" if host == "0.0.0.0" else "::1" if host == "::" else host
    try:
        with socket.create_connection((host, service["port"]), timeout=0.5):
            pass
    except OSError:
        return "absent"
    authority = f"[{host}]" if ":" in host else host
    try:
        with urllib.request.urlopen(f"http://{authority}:{service['port']}/api/health", timeout=2) as response:
            value = json.loads(response.read(1024 * 1024))
        required = (
            {"ready", "backend", "backend_alive"} if service["name"] == "tts" else {"ready", "operations", "models"}
        )
        if not isinstance(value, dict) or not required <= value.keys():
            raise ValueError("incompatible health response")
    except (OSError, ValueError) as exc:
        raise RuntimeError(
            f"{service['name']} port {service['port']} belongs to an incompatible/unresponsive service"
        ) from exc
    return "ready" if value.get("ready") is True else "loading"


def lock_for(service, root=ROOT):
    return ServiceLock(root / ".runtime" / "service-consoles" / f"{service['name']}-{service['port']}.lock")


def write_result(path, **data):
    temporary = path.with_suffix(".tmp")
    temporary.write_text(json.dumps(data), encoding="utf-8")
    temporary.replace(path)


def ensure_service(service, root=ROOT, owner=None):
    lock = lock_for(service, root)
    if not lock.acquire():
        print(f"[{service['name']}] Independent console already running/loading.", flush=True)
        return
    try:
        state = probe(service)
        if state != "absent":
            print(f"[{service['name']}] Reusing existing service ({state}).", flush=True)
            return
        result = root / ".runtime" / "service-consoles" / f"{uuid.uuid4().hex}.json"
        # Worker takes the durable lock; release it immediately before dispatch.
        lock.close()
        command = [
            sys.executable,
            "-u",
            str(root / "scripts" / "service_launcher.py"),
            "worker",
            "--service",
            service["name"],
            "--result",
            str(result),
        ]
        if owner is not None:
            command += [
                "--owner-pid",
                str(owner.pid),
                "--owner-created",
                str(owner.created),
                "--owner-lease",
                str(owner.lease),
            ]
        process = subprocess.Popen(
            command,
            cwd=root,
            creationflags=subprocess.CREATE_NEW_CONSOLE,
        )
        deadline = time.monotonic() + 15
        try:
            while not result.exists():
                if process.poll() is not None or time.monotonic() >= deadline:
                    raise RuntimeError(f"{service['name']} independent console did not acknowledge startup")
                time.sleep(0.05)
            data = json.loads(result.read_text(encoding="utf-8"))
            if data.get("status") == "error":
                raise RuntimeError(data.get("message", "service console startup failed"))
            print(f"[{service['name']}] {data['status']}; loading logs are in its independent console.", flush=True)
        finally:
            result.unlink(missing_ok=True)
    finally:
        lock.close()


def worker(service, result, root=ROOT, owner=None):
    # CREATE_NEW_CONSOLE can still inherit redirected file handles from callers.
    sys.stdout = open("CONOUT$", "w", encoding="utf-8", buffering=1)
    sys.stderr = open("CONOUT$", "w", encoding="utf-8", buffering=1)
    sys.stdin = open("CONIN$", "r", encoding="utf-8")
    ctypes.windll.kernel32.SetConsoleTitleW(f"NachoBot {service['name']} :{service['port']}")
    lock = lock_for(service, root)
    process = job = None
    acknowledged = False
    try:
        if owner is not None and not owner.alive():
            write_result(result, status="error", message="Desktop startup session already ended")
            return 1
        if not lock.acquire():
            write_result(result, status="already starting")
            return 0
        state = probe(service)
        if state != "absent":
            write_result(result, status=f"reused ({state})")
            return 0
        print(f"[{service['name']}] Starting independent service; close this console to stop it.", flush=True)
        job = WindowsJob()
        env = os.environ.copy()
        env.update(PYTHONUNBUFFERED="1", PYTHONIOENCODING="utf-8", HOST=service["host"], PORT=str(service["port"]))
        process = subprocess.Popen(
            service["command"],
            cwd=root,
            env=env,
            stdout=sys.stdout,
            stderr=sys.stderr,
            stdin=subprocess.DEVNULL,
            creationflags=0x00000004,
        )
        job.assign(process)
        ntdll = ctypes.WinDLL("ntdll")
        ntdll.NtResumeProcess.argtypes = [ctypes.c_void_p]
        ntdll.NtResumeProcess.restype = ctypes.c_long
        if ntdll.NtResumeProcess(ctypes.c_void_p(int(process._handle))) < 0:
            raise RuntimeError("could not resume model service")
        write_result(result, status="started (models loading)")
        acknowledged = True
        while process.poll() is None:
            if owner is not None and not owner.alive():
                print(f"[{service['name']}] Owning desktop session ended; stopping this service tree.", flush=True)
                return 0
            time.sleep(0.1)
        returncode = process.returncode
        if returncode:
            raise RuntimeError(f"{service['name']} exited with code {returncode}; see logs above")
        return 0
    except KeyboardInterrupt:
        return 0
    except Exception as exc:
        print(f"[ERROR] {exc}", file=sys.stderr, flush=True)
        if not acknowledged:
            write_result(result, status="error", message=str(exc))
        # Failed consoles remain readable, but must not own a startup lock
        # or leave model descendants alive while waiting for the user.
        if job is not None:
            try:
                job.terminate()
                if process is not None:
                    process.wait(timeout=10)
                job.wait_until_empty(10)
            finally:
                job.close()
                job = None
        elif process is not None and process.poll() is None:
            process.kill()
            process.wait(timeout=10)
        lock.close()
        if owner is None:
            try:
                input("Press Enter to close this failed service console...")
            except EOFError:
                pass
        return 1
    finally:
        if job is not None:
            try:
                job.terminate()
                if process is not None:
                    process.wait(timeout=10)
                job.wait_until_empty(10)
            finally:
                job.close()
        elif process is not None:
            process.kill()
            process.wait(timeout=10)
        lock.close()
        if owner is not None:
            owner.close()


def main(argv=None):
    parser = argparse.ArgumentParser()
    parser.add_argument("action", choices=("ensure", "worker"))
    parser.add_argument("--service", choices=("tts", "perception"))
    parser.add_argument("--result", type=Path)
    parser.add_argument("--owner-pid", type=int)
    parser.add_argument("--owner-created", type=int)
    parser.add_argument("--owner-lease")
    args = parser.parse_args(argv)
    owner = None
    try:
        if os.name != "nt":
            raise RuntimeError("Independent service consoles require Windows")
        services = settings()
        if args.owner_pid is not None:
            if not args.owner_lease:
                raise ValueError("owner lease is required")
            owner = SessionOwner(args.owner_pid, args.owner_created, args.owner_lease)
            if not owner.alive():
                raise RuntimeError("Desktop startup session already ended")
        if args.action == "ensure":
            for service in services:
                ensure_service(service, owner=owner)
            return 0
        service = next(value for value in services if value["name"] == args.service)
        return worker(service, args.result, owner=owner)
    except Exception as exc:
        if args.result:
            write_result(args.result, status="error", message=str(exc))
        print(f"[ERROR] {exc}", file=sys.stderr, flush=True)
        return 1
    finally:
        if owner is not None:
            owner.close()


if __name__ == "__main__":
    raise SystemExit(main())
