"""Run installers in a terminal so their byte/package progress remains visible."""

from __future__ import annotations

import asyncio
import codecs
import errno
import json
import os
import shutil
import subprocess
import threading
import time
import uuid
from concurrent.futures import TimeoutError as FutureTimeout
from pathlib import Path
from typing import Any, Callable

import pyte
import psutil

PROGRESS_PREFIX = "[NachoBot Install Progress] "


class InstallSnapshot(str):
    """Plain-text terminal frame; consumers replace the previous frame in place."""

    def __new__(cls, value: str, key: str | None = None):
        instance = super().__new__(cls, value)
        instance.key = key
        return instance


def decode_install_progress(text: str) -> str:
    """Decode the line-based bridge used by managed TTS installer children."""
    if text.startswith(PROGRESS_PREFIX):
        try:
            frame = json.loads(text[len(PROGRESS_PREFIX):])
            if isinstance(frame.get("line"), str) and isinstance(frame.get("key"), str):
                return InstallSnapshot(frame["line"], frame["key"])
        except (ValueError, AttributeError):
            pass
    return text


def uv_service_install_plan(command: list[str]) -> tuple[list[str], list[str]] | None:
    """Separate uv run's implicit sync from the long-running service command.

    Preserve project, interpreter, lock and optional-dependency selections.
    Like uv run, the sync is inexact so unrelated installed extras are retained.
    """
    if len(command) < 3 or Path(command[0]).stem.lower() != "uv" or command[1] != "run":
        return None
    valued = {"--project", "--directory", "--python", "-p", "--extra", "--group", "--no-group", "--package"}
    flags = {"--locked", "--frozen", "--offline", "--no-dev", "--all-extras", "--all-groups",
             "--no-default-groups", "--active", "--no-editable", "--no-install-project"}
    options = []
    index = 2
    while index < len(command):
        item = command[index]
        if item == "--" or not item.startswith("-"):
            break
        name, separator, _ = item.partition("=")
        if name == "--no-sync":
            return None
        if name in valued:
            options.append(item)
            if not separator:
                index += 1
                if index >= len(command):
                    raise ValueError(f"uv run option {name} requires a value")
                options.append(command[index])
        elif item in flags:
            options.append(item)
        else:
            # Do not guess how run-only options such as --with map to sync.
            raise ValueError(f"Unsupported managed uv run dependency option: {item}")
        index += 1
    return [command[0], "sync", "--inexact", *options], [command[0], "run", "--no-sync", *command[2:]]


def _terminate_installer_tree(process: psutil.Process | None) -> None:
    """Stop only the retained installer identity and its build subprocesses."""
    if process is None:
        return
    try:
        descendants = process.children(recursive=True)
    except psutil.NoSuchProcess:
        return
    for child in [*reversed(descendants), process]:
        try:
            child.kill()
        except psutil.NoSuchProcess:
            pass
    psutil.wait_procs([*descendants, process], timeout=2)


async def run_install_command(
    command: list[str],
    *,
    cwd: Path,
    env: dict[str, str],
    callback: Callable[[str], Any] | None,
    new_session: bool = True,
) -> int:
    """Stream readable terminal snapshots and return the actual process exit code.

    Use a worker thread for blocking PTY IO, keeping the WebSocket/event loop free.
    Arguments are passed directly to the process, never through a shell.
    """
    loop = asyncio.get_running_loop()
    stopped = threading.Event()

    def emit(text: str) -> None:
        if callback and not stopped.is_set():
            pending = asyncio.run_coroutine_threadsafe(callback(text), loop)
            while not stopped.is_set():
                try:
                    pending.result(timeout=0.1)
                    return
                except FutureTimeout:
                    if pending.done():
                        raise
                    continue
            pending.cancel()

    worker = asyncio.create_task(asyncio.to_thread(_run_terminal, command, cwd, env, emit, stopped, new_session))
    try:
        return await asyncio.shield(worker)
    except asyncio.CancelledError:
        stopped.set()
        # Wait for handle cleanup before releasing an installer's working folder.
        try:
            await asyncio.shield(worker)
        except Exception:
            pass
        raise


def _run_terminal(command, cwd, env, emit, stopped, new_session) -> int:
    columns, rows = 140, 40
    screen = pyte.HistoryScreen(columns, rows, history=1000)
    stream = pyte.Stream(screen)
    previous = ""
    last_emit = 0.0
    frame_key = uuid.uuid4().hex

    def update(text: str = "", *, final: bool = False) -> None:
        nonlocal previous, last_emit
        stream.feed(text)
        now = time.monotonic()
        if not final and now - last_emit < 0.15:
            return
        history = [
            "".join(line[x].data if x in line else " " for x in range(columns)).rstrip()
            for line in screen.history.top
        ]
        frame = "\n".join(history + [line.rstrip() for line in screen.display]).strip("\n")
        if frame and frame != previous:
            emit(InstallSnapshot(frame, frame_key))
            previous = frame
            last_emit = now

    # Do not inherit a launcher setting that explicitly suppresses uv progress.
    env = {**env, "TERM": "xterm-256color", "UV_NO_PROGRESS": "0", "NO_COLOR": "1"}
    if os.name == "nt":
        from winpty import PTY, Backend

        executable = shutil.which(command[0], path=env.get("PATH"))
        if not executable:
            raise FileNotFoundError(command[0])
        terminal = PTY(columns, rows, backend=Backend.ConPTY)
        owned_process = None
        try:
            terminal.spawn(
                executable,
                cwd=str(cwd),
                env="\0".join(f"{key}={value}" for key, value in env.items()) + "\0",
                cmdline=" " + subprocess.list2cmdline(command[1:]),
            )
            try:
                owned_process = psutil.Process(terminal.pid)
                owned_process.create_time()
            except psutil.NoSuchProcess:
                pass
            screen.write_process_input = terminal.write
            while not stopped.is_set():
                try:
                    text = terminal.read(blocking=False)
                except EOFError:
                    break
                if text:
                    update(text)
                elif not terminal.isalive():
                    break
                else:
                    update()
                    time.sleep(0.05)
            update(final=True)
            status = terminal.get_exitstatus()
            return status if status is not None else 1
        finally:
            if terminal.isalive():
                _terminate_installer_tree(owned_process)
            del terminal

    # POSIX needs no additional PTY dependency.
    import pty
    import select

    master, slave = pty.openpty()
    proc = None
    owned_process = None
    try:
        import fcntl
        import struct
        import termios

        fcntl.ioctl(slave, termios.TIOCSWINSZ, struct.pack("HHHH", rows, columns, 0, 0))
        proc = subprocess.Popen(
            command, cwd=cwd, env=env, stdin=slave, stdout=slave, stderr=slave,
            start_new_session=new_session,
        )
        try:
            owned_process = psutil.Process(proc.pid)
            owned_process.create_time()
        except psutil.NoSuchProcess:
            pass
        os.close(slave)
        slave = -1
        decoder = codecs.getincrementaldecoder("utf-8")(errors="replace")
        screen.write_process_input = lambda text: os.write(master, text.encode("utf-8"))
        while not stopped.is_set():
            ready, _, _ = select.select([master], [], [], 0.1)
            if not ready:
                update()
                if proc.poll() is not None:
                    break
                continue
            try:
                data = os.read(master, 65536)
            except OSError as exc:
                if exc.errno == errno.EIO:
                    break
                raise
            if not data:
                break
            update(decoder.decode(data))
        update(decoder.decode(b"", final=True), final=True)
        if stopped.is_set():
            _terminate_installer_tree(owned_process)
        return proc.wait()
    finally:
        if proc is not None and proc.poll() is None:
            _terminate_installer_tree(owned_process)
            proc.wait()
        os.close(master)
        if slave != -1:
            os.close(slave)


def main() -> int:
    """Optional WebUI-owned progress bridge; installer environments stay isolated."""
    import sys

    if len(sys.argv) < 3 or sys.argv[1] != "--":
        raise ValueError("expected -- followed by an installer command")

    async def emit(frame):
        # Keep each wire record below asyncio's default 64 KiB line limit,
        # even for non-ASCII output escaped by JSON.
        print(PROGRESS_PREFIX + json.dumps({"key": frame.key, "line": frame[-8000:]}), flush=True)

    # A service-owned bridge must retain the supervisor's POSIX process group
    # so stopping TTS also stops an installer and its build subprocesses.
    return asyncio.run(run_install_command(
        sys.argv[2:], cwd=Path.cwd(), env=os.environ.copy(), callback=emit, new_session=False,
    ))


if __name__ == "__main__":
    raise SystemExit(main())
