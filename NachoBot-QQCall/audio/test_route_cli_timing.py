"""Timing diagnostics must not alter the helper's proof or wire contract."""
import contextlib
import io
import json
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parent))
import windows_routes as routes


class RouteCliTimingTests(unittest.TestCase):
    def run_helper(self, directory, *, diagnostics=True, diagnostic_failure=False):
        request = directory / "request.json"
        output = directory / "report.json"
        request.write_text('{"private":"do-not-copy"}', encoding="utf8")
        proof = {"verified": True, "only_proof_field": "unchanged"}
        original_write = routes.write_report_file

        def write(path, report):
            if diagnostic_failure and Path(path).name == "audio-route-timing.private.json":
                raise routes._ObserverFailure("observer_unavailable")
            original_write(path, report)

        stdout = io.StringIO()
        args = ["--request-file", str(request), "--report", str(output)]
        if diagnostics:
            args.append("--include-route-diagnostics")
        with patch.object(routes, "observe_windows_routes", return_value=proof), \
                patch.object(routes, "write_report_file", side_effect=write), \
                contextlib.redirect_stdout(stdout):
            code = routes.main(args)
        self.assertEqual(code, 0)
        self.assertEqual(json.loads(stdout.getvalue()), {"status": "verified", "verified": True})
        self.assertEqual(json.loads(output.read_text()), proof)
        return directory / "audio-route-timing.private.json"

    def test_timings_are_bounded_latest_only_and_have_no_request_content(self):
        with tempfile.TemporaryDirectory() as value:
            directory = Path(value)
            timing = self.run_helper(directory)
            self.run_helper(directory)
            data = json.loads(timing.read_text())
            self.assertEqual(set(data), {"schema", "helperEnteredAtMs", "requestAgeAtEntryMs",
                "observationElapsedMs", "reportWriteElapsedMs", "helperElapsedMs"})
            self.assertTrue(all(type(v) is int for v in data.values()))
            self.assertNotIn("do-not-copy", timing.read_text())
            self.assertEqual(len(list(directory.glob("*timing*"))), 1)
            self.assertFalse(list(directory.glob("*.tmp")))

    def test_diagnostic_failure_does_not_modify_verification_result(self):
        with tempfile.TemporaryDirectory() as value:
            self.assertFalse(self.run_helper(Path(value), diagnostic_failure=True).exists())

    def test_without_flag_no_extra_diagnostic_file(self):
        with tempfile.TemporaryDirectory() as value:
            self.assertFalse(self.run_helper(Path(value), diagnostics=False).exists())


if __name__ == "__main__":
    unittest.main()
