from __future__ import annotations

import ast
import asyncio
import os
import tempfile
import unittest
import wave
from pathlib import Path
from unittest.mock import patch

from src.common.media_paths import get_shared_media_temp_dir


CORE_ROOT = Path(__file__).resolve().parents[1]


def _module_function_body(relative_path: str, function_name: str) -> ast.AST:
    tree = ast.parse((CORE_ROOT / relative_path).read_text(encoding="utf-8"))
    return next(
        node
        for node in tree.body
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node.name == function_name
    )


def _function_body(relative_path: str, class_name: str, function_name: str) -> ast.AST:
    tree = ast.parse((CORE_ROOT / relative_path).read_text(encoding="utf-8"))
    class_node = next(node for node in tree.body if isinstance(node, ast.ClassDef) and node.name == class_name)
    return next(
        node
        for node in class_node.body
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node.name == function_name
    )


def _assignment_value(node: ast.AST, name: str) -> ast.AST:
    for child in ast.walk(node):
        if isinstance(child, ast.Assign) and any(
            isinstance(target, ast.Name) and target.id == name for target in child.targets
        ):
            return child.value
        if isinstance(child, ast.AnnAssign) and isinstance(child.target, ast.Name) and child.target.id == name:
            return child.value
    raise AssertionError(f"no assignment to {name!r}")


def _calls_shared_media_temp_dir(node: ast.AST) -> bool:
    return any(
        isinstance(child, ast.Call)
        and isinstance(child.func, ast.Name)
        and child.func.id == "get_shared_media_temp_dir"
        for child in ast.walk(node)
    )


def _references_name(node: ast.AST, name: str) -> bool:
    return any(isinstance(child, ast.Name) and child.id == name for child in ast.walk(node))


class SharedMediaTempPathTests(unittest.TestCase):
    def test_helper_creates_temp_directory_under_core_data(self):
        with tempfile.TemporaryDirectory() as root:
            expected = Path(root) / "data" / "media-tmp"
            with patch("src.common.media_paths.CORE_ROOT", Path(root)):
                actual = get_shared_media_temp_dir()

            self.assertEqual(actual, expected)
            self.assertTrue(expected.is_dir())

    def test_music_trim_uses_shared_temp_directory_and_preserves_source(self):
        relative_path = "plugins/mus_library/plugin.py"
        source_path = CORE_ROOT / relative_path
        trim_wav_node = _module_function_body(relative_path, "_trim_wav")

        with tempfile.TemporaryDirectory() as root:
            fixture_root = Path(root)
            source = fixture_root / "source.wav"
            core_root = fixture_root / "core"
            shared_dir = core_root / "data" / "media-tmp"
            source_frames = b"\x01\x00\x02\x00" * 8
            with wave.open(str(source), "wb") as output:
                output.setnchannels(2)
                output.setsampwidth(2)
                output.setframerate(4)
                output.writeframes(source_frames)
            original_bytes = source.read_bytes()

            helper_calls = []

            def fixture_shared_temp_dir():
                helper_calls.append(True)
                return get_shared_media_temp_dir()

            namespace = {
                "Path": Path,
                "os": os,
                "asyncio": asyncio,
                "wave": wave,
                "get_shared_media_temp_dir": fixture_shared_temp_dir,
            }
            exec(
                compile(
                    ast.Module(body=[trim_wav_node], type_ignores=[]),
                    str(source_path),
                    "exec",
                ),
                namespace,
            )
            trim_wav = namespace["_trim_wav"]

            with patch("src.common.media_paths.CORE_ROOT", core_root):
                unchanged = asyncio.run(trim_wav(source, 0))
                self.assertEqual(unchanged, source)
                self.assertEqual(helper_calls, [])

                trimmed = asyncio.run(trim_wav(source, 1))

            self.assertEqual(helper_calls, [True])
            self.assertEqual(trimmed.parent, shared_dir)
            self.assertEqual(trimmed.suffix, ".wav")
            self.assertTrue(trimmed.is_file())
            with wave.open(str(trimmed), "rb") as output:
                self.assertEqual(output.getnchannels(), 2)
                self.assertEqual(output.getsampwidth(), 2)
                self.assertEqual(output.getframerate(), 4)
                self.assertEqual(output.getnframes(), 4)
            self.assertEqual(source.read_bytes(), original_bytes)

    def test_bilibili_download_bases_video_files_on_shared_temp_directory(self):
        download = _function_body(
            "plugins/bilibili_video_sender_plugin/plugin.py",
            "BilibiliAutoSendHandler",
            "execute",
        )
        download_to_temp = next(
            node
            for node in ast.walk(download)
            if isinstance(node, ast.FunctionDef) and node.name == "_download_to_temp"
        )

        self.assertTrue(_calls_shared_media_temp_dir(_assignment_value(download_to_temp, "tmp_dir")))
        for name in ("temp_path", "video_temp", "audio_temp"):
            with self.subTest(output=name):
                self.assertTrue(_references_name(_assignment_value(download_to_temp, name), "tmp_dir"))


if __name__ == "__main__":
    unittest.main()
