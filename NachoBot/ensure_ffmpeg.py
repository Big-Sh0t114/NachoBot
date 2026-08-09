from __future__ import annotations

import os
from pathlib import Path
import shutil
import time

from static_ffmpeg import run


PROJECT_ROOT = Path(__file__).resolve().parents[1]
LOCK_TIMEOUT_SECONDS = 600
STALE_LOCK_SECONDS = 1800


def get_shared_platform_dir() -> Path:
    """返回当前平台共享的 FFmpeg 二进制目录。"""
    configured_dir = os.environ.get("NACHOBOT_FFMPEG_DIR", "").strip()
    shared_root = (
        Path(configured_dir).expanduser()
        if configured_dir
        else PROJECT_ROOT / ".runtime" / "ffmpeg"
    )
    return shared_root.resolve() / run.get_platform_key()


def acquire_download_lock(lock_dir: Path) -> None:
    """通过原子目录创建实现跨进程下载锁。"""
    deadline = time.monotonic() + LOCK_TIMEOUT_SECONDS
    while True:
        try:
            lock_dir.mkdir(parents=False, exist_ok=False)
            (lock_dir / "owner.txt").write_text(
                f"pid={os.getpid()}\ntime={time.time()}\n",
                encoding="utf-8",
            )
            return
        except FileExistsError:
            try:
                age = time.time() - lock_dir.stat().st_mtime
                if age > STALE_LOCK_SECONDS:
                    shutil.rmtree(lock_dir, ignore_errors=True)
                    continue
            except FileNotFoundError:
                continue

            if time.monotonic() >= deadline:
                raise TimeoutError(f"等待 FFmpeg 下载锁超时: {lock_dir}")
            print("[INFO] 其他进程正在准备共享 FFmpeg，等待完成...")
            time.sleep(1)


def main() -> int:
    platform_dir = get_shared_platform_dir()
    platform_dir.mkdir(parents=True, exist_ok=True)
    lock_dir = platform_dir.parent / ".download.lock"

    acquire_download_lock(lock_dir)
    try:
        ffmpeg_path, ffprobe_path = run.get_or_fetch_platform_executables_else_raise(
            download_dir=str(platform_dir)
        )

        missing = [
            path
            for path in (Path(ffmpeg_path), Path(ffprobe_path))
            if not path.is_file()
        ]
        if missing:
            missing_text = ", ".join(str(path) for path in missing)
            raise FileNotFoundError(f"FFmpeg 二进制下载后仍不存在: {missing_text}")

        print(f"FFMPEG={ffmpeg_path}")
        print(f"FFPROBE={ffprobe_path}")
        return 0
    finally:
        shutil.rmtree(lock_dir, ignore_errors=True)


if __name__ == "__main__":
    raise SystemExit(main())
