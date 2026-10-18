"""Import, train, inspect, activate, and test the MaiSelf backend from any cwd."""

from pathlib import Path
import argparse
import json
import math
import sys
import time

CORE_ROOT = Path(__file__).resolve().parents[2]
if str(CORE_ROOT) not in sys.path:
    sys.path.insert(0, str(CORE_ROOT))

from plugins.mai_recog_self.backend.control import (  # noqa: E402
    DataLock,
    activate,
    import_folders,
    import_default_root,
    import_named_folders,
    request_cancel,
    request_pause,
    rollback,
    status,
    train_foreground,
)
from plugins.mai_recog_self.backend.paths import DEFAULT_DATA_ROOT, DEFAULT_RAW_ROOT  # noqa: E402
from plugins.mai_recog_self.backend.runtime import InferenceService, MAX_IMAGE_BYTES  # noqa: E402
from plugins.mai_recog_self.backend.store import Store  # noqa: E402


class NoModelError(RuntimeError):
    """The CLI inference test has no active version to load."""


class JsonArgumentParser(argparse.ArgumentParser):
    def error(self, message: str) -> None:
        self._print_message(json.dumps({"ok": False, "error": message}, ensure_ascii=False) + "\n", sys.stderr)
        raise SystemExit(2)


def _add_global_options(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--data-dir", type=Path, default=argparse.SUPPRESS, help="数据目录，默认 Core/data/plugins/mai_recog_self")
    parser.add_argument("--worker-python", default=argparse.SUPPRESS, help="显式worker Python；省略时优先使用可导入Torch/torchvision的同级NachoBot-Multimodal-Adapter/.venv，否则使用当前Core Python；不读取目录库中的旧路径")


def build_parser() -> argparse.ArgumentParser:
    parser = JsonArgumentParser(description="MaiSelf folder import, CPU/CUDA training, activation, and inference test")
    parser.add_argument("--data-dir", type=Path, default=DEFAULT_DATA_ROOT, help="数据目录")
    parser.add_argument("--worker-python", default="", help="显式worker Python；省略时优先使用可导入Torch/torchvision的同级NachoBot-Multimodal-Adapter/.venv，否则使用当前Core Python；不读取目录库中的旧路径")
    sub = parser.add_subparsers(dest="command", required=True, parser_class=JsonArgumentParser)

    command = sub.add_parser("import", help="从legacy self/other或角色名称文件夹导入静态图片")
    _add_global_options(command)
    command.add_argument("--source", type=Path, default=argparse.SUPPRESS, help="多类别根目录；每个直接子目录是一个类别，包含other背景类")
    command.add_argument("--self", dest="self_dir", type=Path, default=argparse.SUPPRESS, help="legacy二分类自己的图片目录")
    command.add_argument("--other", dest="other_dir", type=Path, default=argparse.SUPPRESS, help="legacy二分类其他图片目录")

    command = sub.add_parser("label", help="显式更正图片标签；导入重复图不会自动改标签")
    _add_global_options(command)
    command.add_argument("image_id", help="status/import输出的32位图片ID")
    command.add_argument("--label", required=True, help="已注册类别名称，或skip/unlabelled")

    command = sub.add_parser("train", help="以前台可观察worker方式训练或恢复")
    _add_global_options(command)
    command.add_argument("--mode", choices=["fresh", "incremental"], default="incremental")
    command.add_argument("--device", choices=["cpu", "cuda"], default="cpu")
    command.add_argument("--input-size", type=int, choices=[224, 320, 448], default=320)
    command.add_argument("--character", default="Bot自己")
    command.add_argument("--resume", metavar="JOB", default="", help="显式恢复已暂停任务")

    command = sub.add_parser("status", help="只读状态、模型和数据就绪度")
    _add_global_options(command)

    command = sub.add_parser("report", help="读取任务报告或进度")
    _add_global_options(command)
    command.add_argument("job_id")

    command = sub.add_parser("activate", help="校验模型哈希、审计记录和CPU加载后启用版本")
    _add_global_options(command)
    command.add_argument("version")

    command = sub.add_parser("rollback", help="校验并恢复上一个启用版本")
    _add_global_options(command)

    command = sub.add_parser("pause", help="请求worker保存checkpoint并等待它退出")
    _add_global_options(command)
    command.add_argument("job_id")

    command = sub.add_parser("cancel", help="协作取消worker或取消已暂停任务")
    _add_global_options(command)
    command.add_argument("job_id")

    command = sub.add_parser("test", help="对单张静态图片调用当前启用模型")
    _add_global_options(command)
    command.add_argument("file", type=Path)
    command.add_argument("--timeout", type=float, default=60.0, help="CLI预热与推理总预算秒数")

    return parser


def _emit(value: dict, stream=sys.stdout) -> None:
    print(json.dumps(value, ensure_ascii=False, allow_nan=False, default=str), file=stream, flush=True)


def _readonly_store(root: Path) -> Store:
    store = Store(root, initialize=False, readonly=True)
    return store


def _test(args: argparse.Namespace, root: Path) -> dict:
    if not math.isfinite(args.timeout) or args.timeout <= 0:
        raise ValueError("timeout必须为有限且大于0的秒数")
    path = args.file.expanduser().resolve()
    with path.open("rb") as stream:
        raw = stream.read(MAX_IMAGE_BYTES + 1)
    if not raw or len(raw) > MAX_IMAGE_BYTES:
        raise ValueError("图片为空或超过20MiB")
    service = InferenceService(root, getattr(args, "worker_python", ""))
    try:
        deadline = time.monotonic() + args.timeout
        while True:
            value = service.status()
            if not value["desired_version"]:
                raise NoModelError("没有启用的模型版本")
            if value["ready"] or value["last_error"] and not value["warming"]:
                break
            if time.monotonic() >= deadline:
                raise TimeoutError(f"推理worker预热超过{args.timeout:g}秒")
            time.sleep(0.1)
        remaining = max(0.1, deadline - time.monotonic())
        result = service.infer(raw, timeout=remaining)
        if result.get("status") != "ok":
            raise RuntimeError(json.dumps(result, ensure_ascii=False))
        return result
    finally:
        service.close()


def run(args: argparse.Namespace) -> dict:
    root = Path(args.data_dir).expanduser().resolve()
    command = args.command
    if command == "import":
        has_source = hasattr(args, "source")
        has_legacy = hasattr(args, "self_dir") or hasattr(args, "other_dir")
        if has_source and has_legacy:
            raise ValueError("--source不能与--self/--other同时使用")
        if has_source:
            return import_named_folders(root, args.source)
        if has_legacy:
            return import_folders(
                root,
                getattr(args, "self_dir", DEFAULT_RAW_ROOT / "self"),
                getattr(args, "other_dir", DEFAULT_RAW_ROOT / "other"),
            )
        return import_default_root(root, DEFAULT_RAW_ROOT)
    if command == "label":
        with DataLock(root):
            store = Store(root)
            from plugins.mai_recog_self.backend.control import _recover_or_reject_live_workers

            _recover_or_reject_live_workers(store)
            return store.label(args.image_id, args.label)
    if command == "train":
        return train_foreground(
            root,
            mode=args.mode,
            device=args.device,
            input_size=args.input_size,
            character=args.character,
            resume_job=args.resume,
            worker_python=getattr(args, "worker_python", ""),
        )
    if command == "status":
        return status(root)
    if command == "report":
        store = _readonly_store(root)
        job = store.job(args.job_id)
        return {"job": job, "progress": job.get("progress") or {}, "report": store.report(args.job_id)}
    if command == "activate":
        return activate(root, args.version, getattr(args, "worker_python", ""))
    if command == "rollback":
        return rollback(root, getattr(args, "worker_python", ""))
    if command == "pause":
        return request_pause(root, args.job_id)
    if command == "cancel":
        return request_cancel(root, args.job_id)
    if command == "test":
        return _test(args, root)
    raise ValueError(f"unsupported command: {command}")


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    try:
        _emit({"ok": True, **run(args)})
        return 0
    except NoModelError as exc:
        _emit({"ok": False, "status": "no_model", "error": str(exc)}, sys.stderr)
        return 2
    except Exception as exc:
        _emit({"ok": False, "error": str(exc)}, sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
