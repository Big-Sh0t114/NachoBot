"""Pipe protocol fails closed and never stacks timed-out native readers."""
import io
import json
import unittest
from unittest.mock import patch

import windows_routes as routes


class StdioTests(unittest.TestCase):
    def serve(self, raw, reports=None):
        output = io.StringIO()
        with patch.object(routes, "observe_windows_routes", side_effect=reports) as observe:
            result = routes.serve_stdio(io.BytesIO(raw), output)
        return result, output.getvalue(), observe

    def test_serial_requests_and_eof(self):
        values = [{"id": str(i) * 32, "request": {"schema": 1}} for i in (1, 2)]
        code, text, observe = self.serve(
            b"".join((json.dumps(v) + "\n").encode() for v in values),
            [{"verified": False, "errors": []}] * 2,
        )
        self.assertEqual(code, 0)
        self.assertEqual([json.loads(line)["id"] for line in text.splitlines()], [v["id"] for v in values])
        self.assertEqual(observe.call_count, 2)
        self.assertTrue(all(call.kwargs["include_route_diagnostics"] for call in observe.call_args_list))

    def test_bad_framing_and_duplicate_keys_never_observe(self):
        for raw in (b"x" * 65537, b'{}', b'{}\n', b'\xff\n',
                    b'{"id":"x","id":"y","request":{}}\n'):
            with self.subTest(raw=raw[:50]):
                code, text, observe = self.serve(raw)
                self.assertEqual((code, text, observe.call_count), (2, "", 0))

    def test_native_timeout_ends_process_without_second_observation(self):
        request = (json.dumps({"id": "a" * 32, "request": {}}) + "\n").encode()
        code, text, observe = self.serve(request * 2, [{"verified": False, "errors": ["observer_timeout"]}])
        self.assertEqual(code, 2)
        self.assertEqual(observe.call_count, 1)
        self.assertEqual(len(text.splitlines()), 1)

    def test_oversized_report_never_written(self):
        request = (json.dumps({"id": "a" * 32, "request": {}}) + "\n").encode()
        code, text, _ = self.serve(request, [{"value": "x" * 65536}])
        self.assertEqual((code, text), (2, ""))


if __name__ == "__main__":
    unittest.main()
