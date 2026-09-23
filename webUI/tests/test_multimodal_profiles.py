from __future__ import annotations

import asyncio
from pathlib import Path
import sys
import unittest


WEBUI_DIR = Path(__file__).resolve().parents[1]
if str(WEBUI_DIR) not in sys.path:
    sys.path.insert(0, str(WEBUI_DIR))

import process_manager  # noqa: E402


class MultimodalProfileTopologyTests(unittest.TestCase):
    def setUp(self) -> None:
        process_manager._register_services(WEBUI_DIR.parent)

    def test_profiles_use_one_public_tts_runtime_and_full_perception(self) -> None:
        full = process_manager.GROUP_DEFS["tts_full"].services
        lite = process_manager.GROUP_DEFS["tts_lite"].services
        potato = process_manager.GROUP_DEFS["potato"].services

        self.assertEqual(full, ["tts_runtime_full", "perception"])
        self.assertEqual(lite, ["tts_runtime_lite"])
        self.assertEqual(potato, [])
        self.assertEqual(process_manager.SERVICE_DEFS["tts_runtime_full"].port, 9880)
        self.assertEqual(process_manager.SERVICE_DEFS["tts_runtime_lite"].port, 9880)
        self.assertEqual(process_manager.SERVICE_DEFS["perception"].port, 9874)
        self.assertNotIn("perception_lite", process_manager.SERVICE_DEFS)
        self.assertNotIn("potato_relay", process_manager.SERVICE_DEFS)

        service = process_manager.SERVICE_DEFS["perception"]
        self.assertEqual(service.cmd[-2:], ["-m", "nachobot_multimodal.api_server"])

    def test_potato_launch_is_core_only_and_has_nonempty_status(self) -> None:
        async def scenario() -> None:
            manager = process_manager.ProcessManager(WEBUI_DIR.parent)
            started: list[str] = []

            async def no_external_core_probe(*, force: bool = True) -> None:
                return None

            # This unit test verifies the profile topology and launch wiring;
            # live Core listeners must not change its result.
            manager.refresh_core_observation = no_external_core_probe  # type: ignore[method-assign]

            async def start_group(group_id: str) -> None:
                started.append(group_id)
                if group_id == "core":
                    manager.states["nachobot"] = process_manager.ServiceState(
                        status=process_manager.ServiceStatus.RUNNING
                    )

            async def stop_group(group_id: str) -> None:
                self.fail(f"unexpected rollback for {group_id}")

            manager.start_group = start_group  # type: ignore[method-assign]
            manager.stop_group = stop_group  # type: ignore[method-assign]

            await manager.start_launch("potato")

            self.assertEqual(started, ["core", "potato"])
            self.assertEqual(manager._launch_runtime, "gpu")
            self.assertEqual(
                manager._active_group_env["core"],
                {"NACHOBOT_RUNTIME_PROFILE": "potato"},
            )
            self.assertEqual(manager._core_runtime_profile, "potato")

            status = manager.get_launch_status()
            self.assertEqual(status["status"], "running")
            self.assertEqual(status["active_profile"], "potato")
            potato = next(profile for profile in status["profiles"] if profile["id"] == "potato")
            self.assertEqual(potato["status"], "running")
            self.assertEqual(potato["services"], [])

        asyncio.run(scenario())


if __name__ == "__main__":
    unittest.main()
