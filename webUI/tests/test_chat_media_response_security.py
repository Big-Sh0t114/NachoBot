from __future__ import annotations

import ast
import asyncio
import mimetypes
import sys
import tempfile
import unittest
from importlib import import_module
from pathlib import Path
from unittest.mock import patch

from fastapi import FastAPI, HTTPException
from fastapi.testclient import TestClient

WEBUI_DIR = Path(__file__).resolve().parents[1]
if str(WEBUI_DIR) not in sys.path:
    sys.path.insert(0, str(WEBUI_DIR))

from media_response import chat_media_file_response


def _import_chat_media_without_default_store_creation():
    """Import the real store while suppressing its import-time default mkdir."""
    default_root = (WEBUI_DIR / "data" / "chat_media").resolve()
    original_mkdir = Path.mkdir

    def mkdir_without_default_store(self, *args, **kwargs):
        if self.resolve() == default_root:
            return None
        return original_mkdir(self, *args, **kwargs)

    with patch.object(Path, "mkdir", mkdir_without_default_store):
        return import_module("chat_media")


chat_media = _import_chat_media_without_default_store_creation()


def _extract_get_chat_media_route(store):
    """Compile only the production route body with the test store bound in."""
    server_path = WEBUI_DIR / "server.py"
    server_tree = ast.parse(server_path.read_text(encoding="utf-8"), filename=str(server_path))
    route_node = next(
        node
        for node in server_tree.body
        if isinstance(node, ast.AsyncFunctionDef) and node.name == "get_chat_media"
    )
    route_node.decorator_list = []
    isolated_module = ast.Module(body=[route_node], type_ignores=[])
    ast.fix_missing_locations(isolated_module)
    namespace = {
        "chat_media_store": store,
        "ChatMediaError": chat_media.ChatMediaError,
        "HTTPException": HTTPException,
        "chat_media_file_response": chat_media_file_response,
        # Kept available so the old route body can be reproduced as a failing baseline.
        "mimetypes": mimetypes,
    }
    exec(  # noqa: S102 - only the inspected production route function AST is compiled
        compile(isolated_module, str(server_path), "exec"), namespace
    )
    return namespace["get_chat_media"]


class ChatMediaResponseSecurityTests(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp_dir.cleanup)
        self.store = chat_media.ChatMediaStore(Path(self.temp_dir.name) / "store")
        self.app = FastAPI()

        self.app.add_api_route(
            "/api/chat/media/{media_id}",
            _extract_get_chat_media_route(self.store),
            methods=["GET"],
        )

        self.client = TestClient(self.app)

    def _save(
        self,
        name: str,
        data: bytes = b"untrusted payload",
        *,
        media_type: str = "file",
        mime_type: str = "application/octet-stream",
    ) -> dict:
        return self.store.save_bytes(
            media_type,
            data,
            name=name,
            mime_type=mime_type,
        )

    def test_active_and_unknown_formats_are_downloads_with_security_headers(self):
        for filename in (
            "page.html",
            "vector.svg",
            "document.xml",
            "document.xhtml",
            "document.pdf",
            "script.js",
            "unknown.mystery",
            "extensionless",
        ):
            with self.subTest(filename=filename):
                payload = b"<script>window.pwned = true</script>"
                metadata = self._save(
                    filename,
                    payload,
                    media_type="image" if filename == "page.html" else "file",
                    mime_type="image/png" if filename == "page.html" else "text/html",
                )
                response = self.client.get(metadata["url"])

                self.assertEqual(response.status_code, 200)
                self.assertEqual(response.content, payload)
                self.assertEqual(
                    response.headers["content-type"], "application/octet-stream"
                )
                self.assertTrue(
                    response.headers["content-disposition"].startswith("attachment;")
                )
                self.assertEqual(
                    response.headers["x-content-type-options"], "nosniff"
                )
                self.assertEqual(
                    response.headers["content-security-policy"],
                    "sandbox; default-src 'none'",
                )

    def test_passive_images_and_video_keep_inline_mime_and_original_bytes(self):
        cases = (
            ("picture.png", b"\x89PNG\r\n\x1a\nimage", "image/png", "image"),
            ("emoji.gif", b"GIF89aemoji", "image/gif", "emoji"),
            ("clip.mp4", b"\x00\x00\x00\x18ftypisomvideo", "video/mp4", "video"),
        )
        for filename, payload, expected_type, media_kind in cases:
            with self.subTest(filename=filename):
                metadata = self._save(
                    filename,
                    payload,
                    media_type=media_kind,
                    mime_type=expected_type,
                )
                response = self.client.get(metadata["url"])

                self.assertEqual(response.status_code, 200)
                self.assertEqual(response.content, payload)
                self.assertEqual(response.headers["content-type"], expected_type)
                self.assertEqual(response.headers["content-disposition"], "inline")
                self.assertEqual(
                    response.headers["x-content-type-options"], "nosniff"
                )
                self.assertEqual(
                    response.headers["content-security-policy"],
                    "sandbox; default-src 'none'",
                )

        polyglot_metadata = self._save(
            "disguised.png",
            b"<script>window.pwned = true</script>",
            media_type="image",
            mime_type="text/html",
        )
        polyglot_response = self.client.get(polyglot_metadata["url"])
        self.assertEqual(polyglot_response.status_code, 200)
        self.assertEqual(polyglot_response.content, b"<script>window.pwned = true</script>")
        self.assertEqual(polyglot_response.headers["content-type"], "image/png")
        self.assertEqual(polyglot_response.headers["x-content-type-options"], "nosniff")
        self.assertEqual(
            polyglot_response.headers["content-security-policy"],
            "sandbox; default-src 'none'",
        )

        video_metadata = self._save(
            "seek.mp4", cases[2][1], media_type="video", mime_type="video/mp4"
        )
        range_response = self.client.get(
            video_metadata["url"], headers={"range": "bytes=4-7"}
        )
        self.assertEqual(range_response.status_code, 206)
        self.assertEqual(range_response.content, b"ftyp")
        self.assertEqual(
            range_response.headers["content-range"],
            f"bytes 4-7/{len(cases[2][1])}",
        )
        self.assertEqual(range_response.headers["content-type"], "video/mp4")

    def test_attachment_filename_uses_starlette_encoding(self):
        quoted_path = self.store.root / "报告 Final.html"
        quoted_path.write_bytes(b"<html>download</html>")
        response = chat_media_file_response(
            quoted_path, trusted_root=self.store.root
        )

        self.assertEqual(
            response.headers["content-disposition"],
            "attachment; filename*=utf-8''%E6%8A%A5%E5%91%8A%20Final.html",
        )

    def test_helper_rejects_outside_sibling_traversal_missing_and_nonregular_paths(self):
        root = self.store.root
        outside = root.parent / "outside.html"
        outside.write_bytes(b"outside")
        sibling_root = root.with_name(root.name + "-sibling")
        sibling_root.mkdir()
        sibling_file = sibling_root / "secret.html"
        sibling_file.write_bytes(b"sibling secret")
        traversal_path = root / ".." / sibling_root.name / sibling_file.name
        missing = root / "missing.mp4"
        directory = root / "directory"
        directory.mkdir()

        rejected_paths = {
            "outside root": outside,
            "sibling prefix": sibling_file,
            "traversal": traversal_path,
            "missing file": missing,
            "nonregular directory": directory,
        }
        for case, path in rejected_paths.items():
            with self.subTest(case=case):
                with self.assertRaises(HTTPException) as raised:
                    chat_media_file_response(path, trusted_root=root)
                self.assertEqual(raised.exception.status_code, 404)
                self.assertEqual(raised.exception.detail, "Media not found")
                self.assertNotIn(str(path), str(raised.exception.detail))

    def test_route_rechecks_resolved_path_against_store_root(self):
        root = self.store.root
        outside = root.parent / "outside-route.html"
        outside.write_bytes(b"outside")
        sibling_root = root.with_name(root.name + "-sibling")
        sibling_root.mkdir()
        sibling_file = sibling_root / "secret-route.html"
        sibling_file.write_bytes(b"sibling secret")
        traversal_path = root / ".." / sibling_root.name / sibling_file.name
        media_id = "a" * 32 + ".html"

        for case, resolved_path in (
            ("outside root", outside),
            ("sibling prefix", sibling_file),
            ("traversal", traversal_path),
        ):
            with self.subTest(case=case):
                with patch.object(self.store, "resolve", return_value=resolved_path):
                    response = self.client.get(f"/api/chat/media/{media_id}")
                self.assertEqual(response.status_code, 404)
                self.assertNotIn(str(resolved_path), response.text)

    def test_route_rejects_valid_looking_id_symlink_to_outside_root(self):
        media_id = "b" * 32 + ".png"
        outside = self.store.root.parent / "outside-image.png"
        outside.write_bytes(b"outside image")
        link = self.store.root / media_id
        try:
            link.symlink_to(outside)
        except (OSError, NotImplementedError) as exc:
            self.skipTest(f"symlinks are unavailable: {type(exc).__name__}")

        with self.assertRaises(HTTPException) as raised:
            chat_media_file_response(link, trusted_root=self.store.root)
        self.assertEqual(raised.exception.status_code, 404)
        self.assertNotIn(str(outside), str(raised.exception.detail))

        response = self.client.get(f"/api/chat/media/{media_id}")

        self.assertEqual(response.status_code, 404)
        self.assertNotIn(str(outside), response.text)

    def test_invalid_media_ids_remain_rejected(self):
        response = self.client.get("/api/chat/media/not-a-generated-id")
        self.assertEqual(response.status_code, 404)
        with self.assertRaises(chat_media.ChatMediaError):
            self.store.resolve("../secret.html")
        route = _extract_get_chat_media_route(self.store)
        with self.assertRaises(HTTPException) as raised:
            asyncio.run(route("../secret.html"))
        self.assertEqual(raised.exception.status_code, 404)
        self.assertEqual(raised.exception.detail, "无效的附件标识")


if __name__ == "__main__":
    unittest.main()
