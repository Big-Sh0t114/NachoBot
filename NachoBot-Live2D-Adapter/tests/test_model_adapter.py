from __future__ import annotations

import json
import queue
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from live2d_adapter.config import ConfigError, load_config
from live2d_adapter.model_adapter import Live2DModelAdapter, inspect_model
from live2d_adapter.renderer import Live2DRenderer


class StubLogger:
    def __getattr__(self, _name: str):
        return lambda *_args, **_kwargs: None


class StubModel:
    def __init__(self, values: list[float]) -> None:
        self.values = values
        self.parameter_writes: list[tuple[str, float, float]] = []
        self.expressions: list[str] = []

    def GetParameterValue(self, index: int) -> float:
        return self.values[index]

    def SetParameterValue(self, parameter_id: str, value: float, weight: float) -> None:
        self.parameter_writes.append((parameter_id, value, weight))

    def SetExpression(self, expression_id: str) -> None:
        self.expressions.append(expression_id)


class ModelFixture:
    def __init__(
        self,
        root: Path,
        *,
        parameters: dict[str, str],
        lip_sync_ids: list[str] | None = None,
        eye_blink_ids: list[str] | None = None,
        expressions: list[str] | None = None,
        motions: list[str] | None = None,
        moc_reference: str = "binary/avatar-core.moc3",
    ) -> None:
        self.root = root
        self.model_path = root / "avatar.model3.json"
        self.display_path = root / "avatar.cdi3.json"
        self.moc_path = root / moc_reference
        self.moc_path.parent.mkdir(parents=True, exist_ok=True)
        self.moc_path.write_bytes(b"test-moc")

        self.display_path.write_text(
            json.dumps(
                {
                    "Parameters": [
                        {"Id": parameter_id, "Name": display_name}
                        for parameter_id, display_name in parameters.items()
                    ]
                },
                ensure_ascii=False,
            ),
            encoding="utf-8",
        )

        groups = []
        if lip_sync_ids is not None:
            groups.append({"Target": "Parameter", "Name": "LipSync", "Ids": lip_sync_ids})
        if eye_blink_ids is not None:
            groups.append({"Target": "Parameter", "Name": "EyeBlink", "Ids": eye_blink_ids})

        self.model_path.write_text(
            json.dumps(
                {
                    "Version": 3,
                    "FileReferences": {
                        "Moc": moc_reference.replace("\\", "/"),
                        "DisplayInfo": self.display_path.name,
                        "Expressions": [
                            {"Name": name, "File": f"{name}.exp3.json"}
                            for name in (expressions or [])
                        ],
                        "Motions": {
                            name: [{"File": f"{name}.motion3.json"}] for name in (motions or [])
                        },
                    },
                    "Groups": groups,
                },
                ensure_ascii=False,
            ),
            encoding="utf-8",
        )


class ModelAdapterTests(unittest.TestCase):
    def test_uses_declared_moc_reference_and_opaque_lipsync_group(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            fixture = ModelFixture(
                Path(temporary_directory),
                parameters={"Param42": "Viseme Amount"},
                lip_sync_ids=["Param42"],
            )

            metadata = inspect_model(fixture.model_path)
            adapter = Live2DModelAdapter(metadata)

            self.assertEqual(metadata.moc_path, fixture.moc_path.resolve())
            self.assertEqual(adapter.resolve_parameter("MOUTH_OPEN"), ("Param42",))

    def test_rejects_conflicting_lipsync_group_and_finds_mouth_parameter(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            fixture = ModelFixture(
                Path(temporary_directory),
                parameters={
                    "ParamEyeLOpen": "左眼开闭",
                    "ParamEyeROpen": "右眼开闭",
                    "ParamMouthOpenY": "口开合",
                },
                lip_sync_ids=["ParamEyeLOpen", "ParamEyeROpen"],
                eye_blink_ids=["ParamEyeLOpen", "ParamEyeROpen"],
            )

            adapter = Live2DModelAdapter.from_model_path(fixture.model_path)

            self.assertEqual(
                adapter.resolve_parameter("MOUTH_OPEN"),
                ("ParamMouthOpenY",),
            )

    def test_uses_opaque_eyeblink_group_for_combined_canonical_control(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            fixture = ModelFixture(
                Path(temporary_directory),
                parameters={"BlinkA": "A", "BlinkB": "B"},
                eye_blink_ids=["BlinkA", "BlinkB"],
            )

            adapter = Live2DModelAdapter.from_model_path(fixture.model_path)

            self.assertEqual(
                adapter.resolve_parameter("EYE_OPEN"),
                ("BlinkA", "BlinkB"),
            )

    def test_manual_parameter_override_wins_after_runtime_binding(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            fixture = ModelFixture(
                Path(temporary_directory),
                parameters={"OpaqueA": "A", "OpaqueB": "B"},
            )
            adapter = Live2DModelAdapter.from_model_path(
                fixture.model_path,
                parameter_mappings={"MOUTH_OPEN": ("OpaqueA", "OpaqueB")},
            )
            adapter.bind_runtime(parameter_ids=("OpaqueA", "OpaqueB"))

            self.assertEqual(
                adapter.resolve_parameter("ParamMouthOpenY"),
                ("OpaqueA", "OpaqueB"),
            )

    def test_manual_expression_override_wins_over_same_named_expression(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            fixture = ModelFixture(
                Path(temporary_directory),
                parameters={"ParamMouthOpenY": "口开合"},
                expressions=["normal", "NeutralFace"],
            )
            adapter = Live2DModelAdapter.from_model_path(
                fixture.model_path,
                expression_mappings={"normal": "NeutralFace"},
            )

            self.assertEqual(adapter.resolve_expression("normal"), "NeutralFace")

    def test_resolves_localized_expressions_and_motion_groups(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            fixture = ModelFixture(
                Path(temporary_directory),
                parameters={"ParamMouthOpenY": "口开合"},
                expressions=["默认", "生气"],
                motions=["点头", "摇头"],
            )
            adapter = Live2DModelAdapter.from_model_path(
                fixture.model_path,
                action_mappings={"NOD": "Nod", "SHAKE_HEAD": "Shake"},
            )

            self.assertEqual(adapter.resolve_expression("normal"), "默认")
            self.assertEqual(adapter.resolve_expression("anger"), "生气")
            self.assertEqual(adapter.resolve_action("NOD"), "点头")
            self.assertEqual(adapter.resolve_action("SHAKE_HEAD"), "摇头")

    def test_disabling_auto_detection_preserves_legacy_identifiers(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            fixture = ModelFixture(
                Path(temporary_directory),
                parameters={"ParamMouthOpenY": "custom"},
                expressions=["f01"],
            )
            adapter = Live2DModelAdapter.from_model_path(
                fixture.model_path,
                enabled=False,
                action_mappings={"NOD": "Nod"},
            )

            self.assertEqual(
                adapter.resolve_parameter("MOUTH_OPEN"),
                ("ParamMouthOpenY",),
            )
            self.assertEqual(adapter.resolve_expression("joy"), "f01")
            self.assertEqual(adapter.resolve_action("NOD"), "Nod")

    def test_auto_detection_does_not_invent_missing_motion_groups(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            fixture = ModelFixture(
                Path(temporary_directory),
                parameters={"ParamMouthOpenY": "口开合"},
            )
            adapter = Live2DModelAdapter.from_model_path(
                fixture.model_path,
                action_mappings={"NOD": "Nod"},
            )

            self.assertIsNone(adapter.resolve_action("NOD"))

    def test_bundled_model_declares_its_mouth_lipsync_parameter(self) -> None:
        adapter_root = Path(__file__).resolve().parents[1]
        model_path = adapter_root / "resources" / "NachoBot" / "Nachobot.model3.json"
        if not model_path.is_file():
            self.skipTest("optional bundled Live2D model is not part of a clean checkout")
        adapter = Live2DModelAdapter.from_model_path(model_path)

        self.assertEqual(
            adapter.resolve_parameter("MOUTH_OPEN"),
            ("ParamMouthOpenY",),
        )

    def test_renderer_expands_canonical_tween_and_lipsync_to_overrides(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            fixture = ModelFixture(
                Path(temporary_directory),
                parameters={"OpaqueA": "A", "OpaqueB": "B"},
            )
            adapter = Live2DModelAdapter.from_model_path(
                fixture.model_path,
                parameter_mappings={"MOUTH_OPEN": ("OpaqueA", "OpaqueB")},
            )
            adapter.bind_runtime(parameter_ids=("OpaqueA", "OpaqueB"))
            renderer = Live2DRenderer(
                str(fixture.model_path),
                StubLogger(),
                queue.Queue(),
                model_adapter=adapter,
            )
            renderer.model = StubModel([0.25, 0.5])
            renderer._parameter_indexes = {"OpaqueA": 0, "OpaqueB": 1}

            renderer._handle_command(
                "param_tween",
                {"param": "MOUTH_OPEN", "value": 1.0, "duration": 0.5},
            )
            renderer._set_lip_sync_value(0.75)

            self.assertEqual(
                [tween["param"] for tween in renderer.active_tweens],
                ["OpaqueA", "OpaqueB"],
            )
            self.assertEqual(
                [tween["start_val"] for tween in renderer.active_tweens],
                [0.25, 0.5],
            )
            self.assertEqual(
                renderer.model.parameter_writes,
                [("OpaqueA", 0.75, 1.0), ("OpaqueB", 0.75, 1.0)],
            )


class AdaptationConfigTests(unittest.TestCase):
    def test_loads_optional_parameter_and_expression_overrides(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            config_path = root / "config.toml"
            config_path.write_text(
                """
[renderer]
model_path = "avatar.model3.json"

[adaptation]
enabled = true

[adaptation.parameters]
MOUTH_OPEN = ["OpaqueA", "OpaqueB"]

[adaptation.expressions]
angry = "MadFace"
""".strip(),
                encoding="utf-8",
            )

            config = load_config(config_path)

            self.assertTrue(config.adaptation.enabled)
            self.assertEqual(
                config.adaptation.parameter_mappings["MOUTH_OPEN"],
                ("OpaqueA", "OpaqueB"),
            )
            self.assertEqual(
                config.adaptation.expression_mappings["angry"],
                "MadFace",
            )

    def test_rejects_non_string_parameter_override(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            config_path = Path(temporary_directory) / "config.toml"
            config_path.write_text(
                """
[renderer]
model_path = "avatar.model3.json"

[adaptation.parameters]
MOUTH_OPEN = [1]
""".strip(),
                encoding="utf-8",
            )

            with self.assertRaises(ConfigError):
                load_config(config_path)

    def test_rejects_non_string_expression_override(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            config_path = Path(temporary_directory) / "config.toml"
            config_path.write_text(
                """
[renderer]
model_path = "avatar.model3.json"

[adaptation.expressions]
angry = 1
""".strip(),
                encoding="utf-8",
            )

            with self.assertRaises(ConfigError):
                load_config(config_path)

    def test_server_bind_can_be_overridden_for_container_runtime(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            config_path = root / "config.toml"
            config_path.write_text(
                """
[server]
host = "127.0.0.1"
port = 8766

[renderer]
model_path = "avatar.model3.json"
""".strip(),
                encoding="utf-8",
            )
            with mock.patch.dict(
                "os.environ",
                {
                    "NACHOBOT_LIVE2D_HOST": "0.0.0.0",
                    "NACHOBOT_LIVE2D_PORT": "9876",
                },
            ):
                config = load_config(config_path)

            self.assertEqual(config.server.host, "0.0.0.0")
            self.assertEqual(config.server.port, 9876)


if __name__ == "__main__":
    unittest.main()
