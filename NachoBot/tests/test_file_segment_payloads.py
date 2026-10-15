import base64
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from ncnk_message import Seg
from src.chat.message_receive.message import MAX_UPLOAD_BASE64_CHARS, MAX_UPLOAD_BYTES, MessageRecv


def _receiver(file_data, *, user_id="user-1"):
    receiver = MessageRecv(
        {
            "message_info": {
                "platform": "discord",
                "message_id": "message-1",
                "group_info": {"group_id": "group-1"},
                "user_info": {"user_id": user_id},
            },
            "message_segment": {"type": "file", "data": file_data},
        }
    )
    receiver.chat_stream = SimpleNamespace(
        stream_id="stream-1",
        platform="discord",
        group_info=SimpleNamespace(group_id="group-1"),
        user_info=SimpleNamespace(user_id=user_id),
    )
    return receiver


class FileSegmentPayloadTests(unittest.IsolatedAsyncioTestCase):
    async def test_bounded_base64_payload_saves_with_existing_actor_and_group_binding(self):
        content = b"bounded attachment"
        receiver = _receiver(
            {
                "name": "notes.txt",
                "base64": base64.b64encode(content).decode("ascii"),
                "size": len(content),
            }
        )

        with (
            patch("src.chat.message_receive.message.sandbox_user_allowed", return_value=True),
            patch(
                "src.chat.message_receive.message.sandbox_manager.save_upload",
                return_value="sandbox/notes.txt",
            ) as save_upload,
        ):
            result = await receiver._process_single_segment(Seg("file", receiver.message_segment.data))

        self.assertIn("已保存到沙盒", result)
        save_upload.assert_called_once_with(
            content,
            "notes.txt",
            stream_id="stream-1",
            platform="discord",
            group_id="group-1",
            actor_id="user-1",
        )

    async def test_invalid_base64_and_size_mismatch_are_rejected(self):
        cases = (
            {"name": "bad.bin", "base64": "%%%", "size": 1},
            {"name": "bad.bin", "base64": "data:application/octet-stream;base64,YWJj", "size": 3},
            {"name": "bad.bin", "base64": base64.b64encode(b"abc").decode("ascii"), "size": 2},
            {"name": "bad.bin", "base64": base64.b64encode(b"abc").decode("ascii"), "size": True},
        )
        with (
            patch("src.chat.message_receive.message.sandbox_user_allowed", return_value=True),
            patch("src.chat.message_receive.message.sandbox_manager.save_upload") as save_upload,
        ):
            for payload in cases:
                receiver = _receiver(payload)
                result = await receiver._process_single_segment(Seg("file", payload))
                self.assertIn("文件数据无效", result)
        save_upload.assert_not_called()

    async def test_encoded_length_is_bounded_before_decode(self):
        encoded = "A" * (MAX_UPLOAD_BASE64_CHARS + 1)
        receiver = _receiver({"name": "large.bin", "base64": encoded, "size": MAX_UPLOAD_BYTES + 1})

        with (
            patch("src.chat.message_receive.message.sandbox_user_allowed", return_value=True),
            patch("src.chat.message_receive.message.base64.b64decode") as decode,
            patch("src.chat.message_receive.message.sandbox_manager.save_upload") as save_upload,
        ):
            result = await receiver._process_single_segment(Seg("file", receiver.message_segment.data))

        self.assertIn("文件大小超过1MB限制", result)
        decode.assert_not_called()
        save_upload.assert_not_called()

    async def test_decoded_length_is_bounded(self):
        content = b"x" * (MAX_UPLOAD_BYTES + 1)
        payload = {
            "name": "large.bin",
            "base64": base64.b64encode(content).decode("ascii"),
            "size": len(content),
        }
        receiver = _receiver(payload)

        with (
            patch("src.chat.message_receive.message.sandbox_user_allowed", return_value=True),
            patch("src.chat.message_receive.message.sandbox_manager.save_upload") as save_upload,
        ):
            result = await receiver._process_single_segment(Seg("file", payload))

        self.assertIn("文件大小超过1MB限制", result)
        save_upload.assert_not_called()

    async def test_base64_payload_still_obeys_sandbox_user_allowlist(self):
        content = b"not authorized"
        payload = {
            "name": "private.txt",
            "base64": base64.b64encode(content).decode("ascii"),
            "size": len(content),
        }
        receiver = _receiver(payload, user_id="untrusted-user")

        with (
            patch("src.chat.message_receive.message.sandbox_user_allowed", return_value=False) as allowed,
            patch("src.chat.message_receive.message.sandbox_manager.save_upload") as save_upload,
        ):
            result = await receiver._process_single_segment(Seg("file", payload))

        self.assertIn("未通过沙盒名单策略", result)
        allowed.assert_called_once_with("untrusted-user")
        save_upload.assert_not_called()

    async def test_existing_url_and_local_path_inputs_still_save(self):
        class FakeResponse:
            status_code = 200
            headers = {"Content-Length": "3"}
            content = b"url"

        class FakeHttpClient:
            async def __aenter__(self):
                return self

            async def __aexit__(self, *_args):
                return None

            async def head(self, _url):
                return FakeResponse()

            async def get(self, _url):
                return FakeResponse()

        with tempfile.TemporaryDirectory() as directory:
            local_path = Path(directory) / "local.txt"
            local_path.write_bytes(b"path")
            url_receiver = _receiver({"name": "remote.txt", "url": "https://example.invalid/remote.txt"})
            path_receiver = _receiver({"name": "local.txt", "path": str(local_path)})

            with (
                patch("src.chat.message_receive.message.sandbox_user_allowed", return_value=True),
                patch("httpx.AsyncClient", return_value=FakeHttpClient()),
                patch(
                    "src.chat.message_receive.message.sandbox_manager.save_upload",
                    side_effect=["sandbox/remote.txt", "sandbox/local.txt"],
                ) as save_upload,
            ):
                url_result = await url_receiver._process_single_segment(
                    Seg("file", url_receiver.message_segment.data)
                )
                path_result = await path_receiver._process_single_segment(
                    Seg("file", path_receiver.message_segment.data)
                )

        self.assertIn("已保存到沙盒", url_result)
        self.assertIn("已保存到沙盒", path_result)
        self.assertEqual([call.args[0] for call in save_upload.call_args_list], [b"url", b"path"])


if __name__ == "__main__":
    unittest.main()
