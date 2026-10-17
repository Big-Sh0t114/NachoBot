import time
import unittest

from signature import (
    build_callback_signature,
    build_webhook_signature,
    verify_callback_signature,
    verify_webhook_signature,
)


class CallbackSignatureTests(unittest.TestCase):
    def test_webhook_sha1_signature(self):
        body = b'{"event":"im_receive_msg"}'
        signature = build_webhook_signature(body, "secret")
        self.assertTrue(verify_webhook_signature(body, "secret", signature))
        self.assertFalse(verify_webhook_signature(body + b" ", "secret", signature))

    def test_matches_official_example(self):
        headers = {
            "x-nonce-str": "123456",
            "x-timestamp": "456789",
            "x-roomid": "268",
            "x-msg-type": "live_gift",
        }
        signature = build_callback_signature(headers, "abc123你好".encode(), "123abc")
        self.assertEqual(signature, "PDcKhdlsrKEJif6uMKD2dw==")

    def test_verification_rejects_replay(self):
        now = time.time()
        headers = {
            "x-nonce-str": "nonce",
            "x-timestamp": str(int(now - 1000)),
            "x-roomid": "room",
            "x-msg-type": "live_comment",
        }
        body = b"[]"
        headers["x-signature"] = build_callback_signature(headers, body, "secret")
        valid, reason = verify_callback_signature(
            headers, body, "secret", tolerance_seconds=300, now=now
        )
        self.assertFalse(valid)
        self.assertEqual(reason, "timestamp outside tolerance")


if __name__ == "__main__":
    unittest.main()
