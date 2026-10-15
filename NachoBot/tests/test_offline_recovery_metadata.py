import math
import unittest

from ncnk_message.offline_recovery import (
    build_recovery_metadata,
    is_offline_replay,
    normalize_original_timestamp,
)


class OfflineRecoveryMetadataTests(unittest.TestCase):
    def test_fractional_platform_seconds_are_preserved(self):
        timestamp, source = normalize_original_timestamp("1780000000.125", 1780000100.0)
        self.assertEqual(timestamp, 1780000000.125)
        self.assertEqual(source, "platform_seconds")

    def test_epoch_milliseconds_are_converted_to_seconds(self):
        timestamp, source = normalize_original_timestamp("1780000000125", 1780000100.0)
        self.assertEqual(timestamp, 1780000000.125)
        self.assertEqual(source, "platform_milliseconds")

    def test_invalid_timestamp_values_fall_back_to_receive_time(self):
        for value in (None, True, False, "", "not-a-time", 0, -1, math.nan, math.inf, -math.inf):
            with self.subTest(value=value):
                timestamp, source = normalize_original_timestamp(value, 1780000100.25)
                self.assertEqual(timestamp, 1780000100.25)
                self.assertEqual(source, "receive_time_fallback")

    def test_metadata_uses_original_time_and_boundaries(self):
        metadata = build_recovery_metadata(
            {"time": "1780000000.125"},
            connection_epoch_started_at=1780000090.0,
            received_at=1780000100.0,
            progress_id="progress-1",
        )
        self.assertEqual(metadata["progress_id"], "progress-1")
        self.assertTrue(is_offline_replay(metadata, core_startup_at=1780000080.0))
        live_metadata = build_recovery_metadata(
            {"time": "1780000000.125"},
            connection_epoch_started_at=1779999900.0,
            received_at=1780000100.0,
            progress_id="live-progress",
        )
        self.assertFalse(is_offline_replay(live_metadata, core_startup_at=1779999900.0))

    def test_either_boundary_can_identify_replay(self):
        metadata = build_recovery_metadata(
            {"time": 1780000000.125},
            connection_epoch_started_at=1779999900.0,
            received_at=1780000100.0,
            progress_id="progress-2",
        )
        self.assertTrue(is_offline_replay(metadata, core_startup_at=1780000080.0))

    def test_invalid_boundary_or_wrong_version_fails_closed(self):
        self.assertFalse(is_offline_replay({"version": 2, "original_timestamp": 1, "connection_epoch_started_at": 2}))
        self.assertFalse(is_offline_replay({"version": 1, "original_timestamp": math.inf, "connection_epoch_started_at": 2}))
        self.assertFalse(is_offline_replay({"version": 1, "original_timestamp": 1, "connection_epoch_started_at": math.nan}))


if __name__ == "__main__":
    unittest.main()
