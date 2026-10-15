from __future__ import annotations

import importlib.util
import pathlib
import sys
import tempfile

sys.dont_write_bytecode = True
ROOT = pathlib.Path(__file__).resolve().parents[3]
SPEC = importlib.util.spec_from_file_location("audio_route_guard_probe", ROOT / "scripts" / "audio_route_guard_probe.py")
assert SPEC and SPEC.loader
PROBE = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(PROBE)


def main() -> None:
    with tempfile.TemporaryDirectory(prefix="qar-probe-mock-") as temp:
        image = pathlib.Path(temp) / "QQ.exe"
        image.write_bytes(b"mock metadata only")
        config = {
            "version": 1,
            "qqBuild": "9.9.26-44343",
            "parentPid": 100,
            "childPid": 101,
            "hostGeneration": 9007199254740991,
            "parentCreationTime": 1000,
            "childCreationTime": 1001,
            "parentImage": str(image),
            "childImage": str(image),
            "captureEndpointId": "opaque-capture-id",
            "renderEndpointId": "opaque-render-id",
        }
        prepared = PROBE.validate_config(config)
        assert prepared["endpointIdsPresent"] and prepared["generationPresent"]
        assert "captureEndpointId" not in prepared and "hostGeneration" not in prepared

        for field, value, error in (
            ("hostGeneration", 9007199254740992, "hostGeneration_out_of_range"),
            ("parentPid", 4294967296, "parentPid_out_of_range"),
            ("captureEndpointId", "x" * 1024, "capture_endpoint_id_invalid"),
            ("childPid", True, "childPid_invalid"),
        ):
            invalid = dict(config)
            invalid[field] = value
            try:
                PROBE.validate_config(invalid)
            except ValueError as exception:
                assert str(exception) == error, (field, exception)
            else:
                raise AssertionError(f"{field} invalid value was accepted")

        report = PROBE.inspect_artifact(str(image))
        assert report["sizeBytes"] == len(b"mock metadata only")
        assert len(report["sha256"]) == 64

    print("audio_route_probe_metadata_test=passed evidence=temporary_mock_files_only")


if __name__ == "__main__":
    main()
