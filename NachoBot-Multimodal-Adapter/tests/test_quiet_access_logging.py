from __future__ import annotations

import logging
from pathlib import Path
import sys
import unittest


PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from nachobot_multimodal.utils.uvicorn_logging import install_quiet_access_logging


ACCESS_MESSAGE = '%s - "%s %s HTTP/%s" %s'


class _Capture(logging.Handler):
    def __init__(self) -> None:
        super().__init__(logging.DEBUG)
        self.records: list[logging.LogRecord] = []

    def emit(self, record: logging.LogRecord) -> None:
        self.records.append(record)


def _access_record(path: str, status: int) -> logging.LogRecord:
    return logging.LogRecord(
        name="uvicorn.access",
        level=logging.INFO,
        pathname=__file__,
        lineno=1,
        msg=ACCESS_MESSAGE,
        args=("127.0.0.1:1234", "GET", path, "1.1", status),
        exc_info=None,
    )


class QuietAccessLoggingTests(unittest.TestCase):
    def setUp(self) -> None:
        self.access_logger = logging.getLogger("uvicorn.access")
        self.error_logger = logging.getLogger("uvicorn.error")
        self._logger_state = {
            self.access_logger: (
                list(self.access_logger.handlers),
                list(self.access_logger.filters),
                self.access_logger.level,
                self.access_logger.propagate,
            ),
            self.error_logger: (
                list(self.error_logger.handlers),
                list(self.error_logger.filters),
                self.error_logger.level,
                self.error_logger.propagate,
            ),
        }
        self.access_capture = _Capture()
        self.error_capture = _Capture()
        for logger, capture in (
            (self.access_logger, self.access_capture),
            (self.error_logger, self.error_capture),
        ):
            logger.handlers.clear()
            logger.filters.clear()
            logger.addHandler(capture)
            logger.setLevel(logging.INFO)
            logger.propagate = False

    def tearDown(self) -> None:
        for logger, (handlers, filters, level, propagate) in self._logger_state.items():
            logger.handlers.clear()
            logger.filters[:] = filters
            logger.setLevel(level)
            logger.propagate = propagate
            for handler in handlers:
                logger.addHandler(handler)

    def test_successful_probe_is_suppressed_at_info(self) -> None:
        install_quiet_access_logging()

        self.access_logger.handle(_access_record("/health", 200))

        self.assertEqual(self.access_capture.records, [])
        self.assertEqual(self.error_capture.records, [])

    def test_debug_enabled_error_logger_receives_equivalent_probe_message(self) -> None:
        install_quiet_access_logging()
        self.error_logger.setLevel(logging.DEBUG)

        self.access_logger.handle(_access_record("/v1/capabilities?probe=1", 204))

        self.assertEqual(self.access_capture.records, [])
        self.assertEqual(len(self.error_capture.records), 1)
        rerouted = self.error_capture.records[0]
        self.assertEqual(rerouted.levelno, logging.DEBUG)
        self.assertEqual(rerouted.getMessage(), _access_record("/v1/capabilities?probe=1", 204).getMessage())

    def test_query_string_is_ignored_when_matching_probe_path(self) -> None:
        install_quiet_access_logging()

        self.access_logger.handle(_access_record("/api/health?ready=1", 200))

        self.assertEqual(self.access_capture.records, [])

    def test_failed_probe_keeps_normal_access_visibility(self) -> None:
        install_quiet_access_logging()

        self.access_logger.handle(_access_record("/api/health", 503))

        self.assertEqual(len(self.access_capture.records), 1)
        self.assertEqual(self.access_capture.records[0].getMessage(), _access_record("/api/health", 503).getMessage())

    def test_ordinary_request_keeps_normal_access_visibility(self) -> None:
        install_quiet_access_logging()

        self.access_logger.handle(_access_record("/v1/perception", 200))

        self.assertEqual(len(self.access_capture.records), 1)

    def test_installation_is_idempotent(self) -> None:
        first = install_quiet_access_logging()
        second = install_quiet_access_logging()

        self.assertIs(first, second)
        self.assertEqual(len(self.access_logger.filters), 1)


if __name__ == "__main__":
    unittest.main()
