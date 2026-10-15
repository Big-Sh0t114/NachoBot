from __future__ import annotations

import asyncio
from pathlib import Path
import sys
import unittest
from urllib.error import HTTPError
from urllib.request import Request
from unittest.mock import AsyncMock, patch


WEBUI_DIR = Path(__file__).resolve().parents[1]
if str(WEBUI_DIR) not in sys.path:
    sys.path.insert(0, str(WEBUI_DIR))

import process_manager  # noqa: E402
import tts_manager  # noqa: E402


class ExternalCoreObservationTests(unittest.TestCase):
    def setUp(self) -> None:
        process_manager._register_services(WEBUI_DIR.parent)
        self.manager = process_manager.ProcessManager(WEBUI_DIR.parent)

    def _patch_health(self, result):
        return (
            patch.object(tts_manager, "_get_core_base_url", return_value="http://127.0.0.1:8000"),
            patch.object(tts_manager, "_get_core_auth_token", return_value="test-token"),
            patch.object(tts_manager.TTSManager, "_request_json", return_value=result),
        )

    @staticmethod
    def _health_payload(
        profile: str,
        *,
        perception_ready: bool | None = None,
        tts_ready: bool | None = None,
        status: str | None = None,
    ) -> dict:
        required = {
            "full": (True, True),
            "lite": (False, True),
            "potato": (False, False),
        }[profile]
        if perception_ready is None:
            perception_ready = True
        if tts_ready is None:
            tts_ready = profile != "potato"
        observed_ready = (
            (not required[0] or perception_ready)
            and (not required[1] or tts_ready)
        )
        return {
            "status": status or ("ok" if observed_ready else "degraded"),
            "desired_profile": profile,
            "capabilities": {
                "perception": ["audio.transcribe.v1"],
                "tts": profile != "potato",
            },
            "observed_local": {
                "profile": profile,
                "ready": observed_ready,
                "perception": {
                    "required": required[0],
                    "ready": perception_ready,
                },
                "tts": {
                    "required": required[1],
                    "ready": tts_ready,
                },
            },
        }

    def test_authenticated_strict_schema_populates_external_status_without_identity(self) -> None:
        payload = self._health_payload("lite", tts_ready=False)

        async def scenario() -> None:
            with self._patch_health(payload)[0], self._patch_health(payload)[1], self._patch_health(payload)[2] as request:
                observation = await self.manager.refresh_core_observation()
            self.assertIsNotNone(observation)
            self.assertEqual(observation.observed_profile, "lite")
            self.assertFalse(observation.observed_local_ready)
            self.assertTrue(observation.tts_required)
            self.assertFalse(observation.tts_ready)
            request.assert_called_once_with(
                "http://127.0.0.1:8000/api/multimodal/health",
                tts_manager.CORE_HEALTH_TIMEOUT_SECONDS,
                "test-token",
            )
            self.assertEqual(tts_manager.CORE_HEALTH_TIMEOUT_SECONDS, 4.0)

            status = self.manager.get_service_status("nachobot")
            self.assertEqual(status["status"], "running")
            self.assertFalse(status["managed"])
            self.assertEqual(status["origin"], "external")
            self.assertEqual(status["observed_profile"], "lite")
            self.assertIsNone(status["pid"])
            self.assertNotIn("test-token", repr(status))

            launch = self.manager.get_launch_status()
            self.assertEqual(launch["status"], "partial")
            self.assertEqual(launch["active_profile"], "lite")
            lite = next(profile for profile in launch["profiles"] if profile["id"] == "lite")
            self.assertEqual(lite["status"], "partial")

        asyncio.run(scenario())

    def test_invalid_unauthorized_timeout_and_malformed_probe_clear_observation(self) -> None:
        async def scenario() -> None:
            self.manager.core_observation = process_manager.CoreObservation("ok", "full", 0.0)
            for failure in (
                {"status": "ok", "desired_profile": "full", "capabilities": []},
                RuntimeError("unauthorized"),
                TimeoutError("timed out"),
                {"status": "ready", "desired_profile": "full", "capabilities": {}},
            ):
                with patch.object(tts_manager, "_get_core_base_url", return_value="http://127.0.0.1:8000"), \
                     patch.object(tts_manager, "_get_core_auth_token", return_value="secret"), \
                     patch.object(tts_manager.TTSManager, "_request_json", side_effect=failure):
                    self.assertIsNone(await self.manager.refresh_core_observation())
                self.assertIsNone(self.manager.core_observation)

        asyncio.run(scenario())

    def test_transport_only_failure_keeps_bounded_display_state(self) -> None:
        payload = self._health_payload("lite")

        async def scenario() -> None:
            with self._patch_health(payload)[0], self._patch_health(payload)[1], self._patch_health(payload)[2]:
                await self.manager.refresh_core_observation()
            with patch.object(tts_manager, "_get_core_base_url", return_value="http://127.0.0.1:8000"), \
                 patch.object(tts_manager, "_get_core_auth_token", return_value="test-token"), \
                 patch.object(tts_manager.TTSManager, "_request_json", side_effect=TimeoutError("listener reconnecting")):
                self.assertIsNone(await self.manager.refresh_core_observation())

            self.assertIsNone(self.manager.core_observation)
            self.assertEqual(self.manager.core_readiness_status(), "running")
            core = self.manager.get_service_status("nachobot")
            self.assertEqual(core["status"], "running")
            self.assertEqual(core["origin"], "external")
            self.assertEqual(core["observed_profile"], "lite")
            self.assertEqual(self.manager.get_service_status("tts_runtime_lite")["status"], "running")
            launch = self.manager.get_launch_status()
            self.assertEqual(launch["active_profile"], "lite")
            self.assertEqual(launch["status"], "running")

            self.manager._core_display_grace_until = __import__("time").monotonic() - 1
            self.assertEqual(self.manager.core_readiness_status(), "stopped")
            self.assertEqual(self.manager.get_service_status("nachobot")["status"], "stopped")
            self.assertEqual(self.manager.get_launch_status()["status"], "stopped")

        asyncio.run(scenario())

    def test_unauthorized_or_invalid_health_clears_display_grace(self) -> None:
        payload = self._health_payload("full")
        failures = (
            HTTPError(Request("http://127.0.0.1:8000/api/multimodal/health"), 401, "Unauthorized", {}, None),
            {"status": "ok", "desired_profile": "full", "capabilities": []},
        )

        async def scenario() -> None:
            for failure in failures:
                with self._patch_health(payload)[0], self._patch_health(payload)[1], self._patch_health(payload)[2]:
                    await self.manager.refresh_core_observation()
                with patch.object(tts_manager, "_get_core_base_url", return_value="http://127.0.0.1:8000"), \
                     patch.object(tts_manager, "_get_core_auth_token", return_value="test-token"), \
                     patch.object(tts_manager.TTSManager, "_request_json", side_effect=failure):
                    self.assertIsNone(await self.manager.refresh_core_observation())
                self.assertIsNone(self.manager._external_core_display_observation())
                self.assertEqual(self.manager.core_readiness_status(), "stopped")

        asyncio.run(scenario())

    def test_uncertain_transport_start_preflight_checks_process_and_port(self) -> None:
        payload = self._health_payload("lite")

        async def scenario() -> None:
            with self._patch_health(payload)[0], self._patch_health(payload)[1], self._patch_health(payload)[2]:
                await self.manager.refresh_core_observation()
            with patch.object(tts_manager, "_get_core_base_url", return_value="http://127.0.0.1:8000"), \
                 patch.object(tts_manager, "_get_core_auth_token", return_value="test-token"), \
                 patch.object(tts_manager.TTSManager, "_request_json", side_effect=TimeoutError("listener reconnecting")):
                self.assertIsNone(await self.manager.refresh_core_observation())

            listener_snapshot = process_manager._ProcessSnapshot((), {8000: frozenset({987})})
            with patch.object(
                self.manager,
                "_probe_core_observation",
                new_callable=AsyncMock,
                return_value=process_manager._CoreProbeResult(None, "transport"),
            ), patch.object(self.manager, "_build_external_process_snapshot", return_value=listener_snapshot) as snapshot:
                with self.assertRaisesRegex(RuntimeError, "拒绝启动替代服务"):
                    await self.manager._fresh_external_core_observation()
            snapshot.assert_called_once()
            self.assertIsNone(self.manager.core_observation)

            absent_snapshot = process_manager._ProcessSnapshot((), {})
            with patch.object(
                self.manager,
                "_probe_core_observation",
                new_callable=AsyncMock,
                return_value=process_manager._CoreProbeResult(None, "transport"),
            ), patch.object(self.manager, "_build_external_process_snapshot", return_value=absent_snapshot):
                self.assertIsNone(await self.manager._fresh_external_core_observation())
            self.assertIsNone(self.manager._last_verified_external_core)

            live_process = process_manager._ProcessRecord(
                pid=987,
                ppid=None,
                cwd=str(self.manager.root / "NachoBot"),
                argv=("python", str(self.manager.root / "NachoBot" / "main.py")),
                executable="python.exe",
                name="python.exe",
            )
            process_snapshot = process_manager._ProcessSnapshot((live_process,), {})
            self.assertEqual(self.manager._external_core_process_presence(process_snapshot), "present")

            uncertain_snapshot = process_manager._ProcessSnapshot((), {}, failed=True)
            self.assertEqual(self.manager._external_core_process_presence(uncertain_snapshot), "indeterminate")

        asyncio.run(scenario())

    def test_late_core_probe_waiter_cannot_overwrite_newer_success(self) -> None:
        async def scenario() -> None:
            loop = asyncio.get_running_loop()
            old_task = loop.create_future()
            self.manager._core_probe_task = old_task  # type: ignore[assignment]
            self.manager._core_probe_task_generation = 1
            self.manager._core_probe_generation = 1
            old_waiter = asyncio.create_task(self.manager.refresh_core_observation())
            await asyncio.sleep(0)

            old_task.set_result(process_manager._CoreProbeResult(None, "transport"))
            newer = process_manager.CoreObservation("ok", "lite", 1.0)
            self.manager._probe_core_observation = AsyncMock(  # type: ignore[method-assign]
                return_value=process_manager._CoreProbeResult(newer)
            )
            latest = await self.manager.refresh_core_observation()
            await old_waiter
            self.assertEqual(latest, newer)
            self.assertEqual(self.manager.core_observation, newer)
            self.assertEqual(self.manager._external_core_display_observation(), newer)

        asyncio.run(scenario())

    def test_chat_post_uses_recently_verified_core_for_real_websocket_delivery(self) -> None:
        import server

        payload = self._health_payload("lite")

        async def scenario() -> None:
            with patch.object(server, "process_mgr", self.manager), \
                 patch.object(tts_manager, "_get_core_base_url", return_value="http://127.0.0.1:8000"), \
                 patch.object(tts_manager, "_get_core_auth_token", return_value="test-token"), \
                 patch.object(tts_manager.TTSManager, "_request_json", side_effect=[payload, TimeoutError("listener reconnecting")]), \
                 patch.object(server.chat_backend, "send_message", new_callable=AsyncMock, return_value={"status": "accepted"}) as send:
                await self.manager.refresh_core_observation()
                request = server.ChatMessageRequest(conversation_id="chat-1", message="hello")
                result = await server.chat_message(request)
            self.assertEqual(result["status"], "accepted")
            send.assert_awaited_once()

        asyncio.run(scenario())

    def test_chat_post_never_sends_without_any_verified_core_evidence(self) -> None:
        import server

        async def scenario() -> None:
            with patch.object(server, "process_mgr", self.manager), \
                 patch.object(tts_manager, "_get_core_base_url", return_value="http://127.0.0.1:8000"), \
                 patch.object(tts_manager, "_get_core_auth_token", return_value="test-token"), \
                 patch.object(tts_manager.TTSManager, "_request_json", side_effect=TimeoutError("unavailable")), \
                 patch.object(server.chat_backend, "send_message", new_callable=AsyncMock) as send:
                request = server.ChatMessageRequest(conversation_id="chat-1", message="hello")
                with self.assertRaises(server.HTTPException) as raised:
                    await server.chat_message(request)
            self.assertEqual(raised.exception.status_code, 503)
            send.assert_not_awaited()

        asyncio.run(scenario())

    def test_port_open_alone_is_not_external_identity(self) -> None:
        async def scenario() -> None:
            with patch.object(self.manager, "_port_is_open", return_value=True), \
                 patch.object(tts_manager, "_get_core_base_url", return_value="http://127.0.0.1:8000"), \
                 patch.object(tts_manager, "_get_core_auth_token", return_value="secret"), \
                 patch.object(tts_manager.TTSManager, "_request_json", side_effect=RuntimeError("not Core")):
                await self.manager.refresh_core_observation()
            status = self.manager.get_service_status("nachobot")
            self.assertEqual(status["status"], "stopped")
            self.assertIsNone(status["origin"])

        asyncio.run(scenario())

    def test_manager_owned_core_precedes_external_observation(self) -> None:
        self.manager._core_runtime_profile = "full"
        self.manager.states["nachobot"] = process_manager.ServiceState(
            status=process_manager.ServiceStatus.RUNNING,
            pid=1234,
        )
        self.manager.core_observation = process_manager.CoreObservation("ok", "lite", 0.0)
        status = self.manager.get_service_status("nachobot")
        self.assertTrue(status["managed"])
        self.assertEqual(status["origin"], "webui")
        self.assertEqual(status["observed_profile"], "full")
        self.assertEqual(status["pid"], 1234)

    def test_manager_owned_health_is_not_retained_as_external_after_stop(self) -> None:
        self.manager.states["nachobot"] = process_manager.ServiceState(
            status=process_manager.ServiceStatus.RUNNING,
            pid=1234,
        )
        payload = self._health_payload("lite")

        async def scenario() -> None:
            with self._patch_health(payload)[0], self._patch_health(payload)[1], self._patch_health(payload)[2]:
                self.assertIsNotNone(await self.manager.refresh_core_observation())
            self.assertIsNone(self.manager.core_observation)
            self.assertEqual(self.manager.get_service_status("nachobot")["origin"], "webui")

            self.manager.states["nachobot"].status = process_manager.ServiceStatus.STOPPED
            self.manager.states["nachobot"].pid = None
            status = self.manager.get_service_status("nachobot")
            self.assertEqual(status["status"], "stopped")
            self.assertIsNone(status["origin"])

        asyncio.run(scenario())

    def test_external_health_maps_full_lite_and_potato_dependencies(self) -> None:
        async def scenario() -> None:
            for profile, expected in (
                (
                    "full",
                    {
                        "tts_runtime_full": "running",
                        "perception": "running",
                        "tts_runtime_lite": "stopped",
                    },
                ),
                (
                    "lite",
                    {
                        "tts_runtime_lite": "running",
                        "tts_runtime_full": "stopped",
                        "perception": "stopped",
                    },
                ),
                (
                    "potato",
                    {
                        "tts_runtime_full": "stopped",
                        "tts_runtime_lite": "stopped",
                        "perception": "stopped",
                    },
                ),
            ):
                payload = self._health_payload(profile)
                with self._patch_health(payload)[0], self._patch_health(payload)[1], self._patch_health(payload)[2]:
                    await self.manager.refresh_core_observation()
                for service_id, expected_status in expected.items():
                    self.assertEqual(
                        self.manager.get_service_status(service_id)["status"],
                        expected_status,
                    )
                launch = self.manager.get_launch_status()
                self.assertEqual(launch["status"], "running")
                self.assertEqual(launch["active_profile"], profile)

        asyncio.run(scenario())

    def test_malformed_readiness_and_status_consistency_fail_closed(self) -> None:
        valid = self._health_payload("full")
        malformed = [
            {**valid, "observed_local": []},
            {
                **valid,
                "observed_local": {
                    **valid["observed_local"],
                    "profile": "lite",
                },
            },
            {
                **valid,
                "observed_local": {
                    **valid["observed_local"],
                    "perception": {
                        **valid["observed_local"]["perception"],
                        "required": 1,
                    },
                },
            },
            {
                **valid,
                "observed_local": {
                    **valid["observed_local"],
                    "ready": False,
                },
            },
            {**valid, "status": "degraded"},
            {**valid, "capabilities": {"tts": "yes"}},
        ]

        async def scenario() -> None:
            for payload in malformed:
                with self._patch_health(payload)[0], self._patch_health(payload)[1], self._patch_health(payload)[2]:
                    self.assertIsNone(await self.manager.refresh_core_observation())
                self.assertIsNone(self.manager.core_observation)

        asyncio.run(scenario())

    def test_three_argument_observation_does_not_synthesize_dependents(self) -> None:
        self.manager.core_observation = process_manager.CoreObservation("ok", "full", 0.0)
        self.assertEqual(self.manager.get_service_status("tts_runtime_full")["status"], "stopped")
        self.assertEqual(self.manager.get_service_status("perception")["status"], "stopped")
        self.assertEqual(self.manager.get_launch_status()["status"], "partial")

    def test_matching_external_profile_starts_dependents_only(self) -> None:
        self.manager.core_observation = process_manager.CoreObservation("ok", "full", 0.0)
        started: list[str] = []

        async def fake_start_group(group_id: str) -> None:
            started.append(group_id)
            if group_id == "tts_full":
                for service_id in process_manager.GROUP_DEFS[group_id].services:
                    self.manager.states[service_id] = process_manager.ServiceState(
                        status=process_manager.ServiceStatus.RUNNING
                    )

        async def scenario() -> None:
            self.manager.start_group = fake_start_group  # type: ignore[method-assign]
            self.manager.refresh_core_observation = AsyncMock(  # type: ignore[method-assign]
                side_effect=[self.manager.core_observation, self.manager.core_observation]
            )
            await self.manager.start_launch("full")

        asyncio.run(scenario())
        self.assertEqual(started, ["tts_full"])
        self.assertNotIn("nachobot", self.manager.states)

    def test_mixed_external_start_skips_ready_dependency_and_starts_missing_one(self) -> None:
        observation = process_manager.CoreObservation(
            "degraded",
            "full",
            0.0,
            True,
            True,
            False,
            True,
            True,
        )
        started: list[str] = []

        async def fake_start_service(service_id: str, **_kwargs) -> None:
            started.append(service_id)
            self.manager.states[service_id] = process_manager.ServiceState(
                status=process_manager.ServiceStatus.RUNNING,
            )

        async def scenario() -> None:
            self.manager.core_observation = observation
            self.manager.refresh_core_observation = AsyncMock(return_value=observation)  # type: ignore[method-assign]
            self.manager.start_service = fake_start_service  # type: ignore[method-assign]
            await self.manager.start_group("tts_full")

        asyncio.run(scenario())
        self.assertEqual(started, ["perception"])
        tts = self.manager.get_service_status("tts_runtime_full")
        self.assertEqual(tts["status"], "running")
        self.assertFalse(tts["managed"])
        self.assertEqual(tts["origin"], "external")
        self.assertEqual(self.manager.get_service_status("perception")["origin"], "webui")

    def test_manager_owned_dependency_wins_over_external_ready_observation(self) -> None:
        observation = process_manager.CoreObservation(
            "ok",
            "full",
            0.0,
            True,
            True,
            True,
            True,
            True,
        )
        self.manager.core_observation = observation
        self.manager.states["tts_runtime_full"] = process_manager.ServiceState(
            status=process_manager.ServiceStatus.RUNNING,
            pid=4321,
        )
        status = self.manager.get_service_status("tts_runtime_full")
        self.assertEqual(status["status"], "running")
        self.assertTrue(status["managed"])
        self.assertEqual(status["origin"], "webui")
        self.assertEqual(status["pid"], 4321)

    def test_mismatching_external_profile_is_rejected_before_start(self) -> None:
        self.manager.core_observation = process_manager.CoreObservation("ok", "full", 0.0)
        started: list[str] = []

        async def fake_start_group(group_id: str) -> None:
            started.append(group_id)

        async def scenario() -> None:
            self.manager.start_group = fake_start_group  # type: ignore[method-assign]
            self.manager.refresh_core_observation = AsyncMock(  # type: ignore[method-assign]
                return_value=self.manager.core_observation
            )
            with self.assertRaises(RuntimeError):
                await self.manager.start_launch("lite")

        asyncio.run(scenario())
        self.assertEqual(started, [])

    def test_direct_group_and_service_requests_reject_external_profile_mismatch(self) -> None:
        self.manager.core_observation = process_manager.CoreObservation("ok", "full", 0.0)
        with self.assertRaises(RuntimeError):
            self.manager.request_start_group("tts_lite")
        with self.assertRaises(RuntimeError):
            self.manager.request_start_service("tts_runtime_lite")

    def test_external_disappearance_is_rejected_and_never_stops_external(self) -> None:
        self.manager.core_observation = process_manager.CoreObservation("ok", "full", 0.0)
        started: list[str] = []
        terminated = AsyncMock()

        async def fake_start_group(group_id: str) -> None:
            started.append(group_id)

        async def scenario() -> None:
            self.manager.start_group = fake_start_group  # type: ignore[method-assign]
            self.manager.refresh_core_observation = AsyncMock(return_value=None)  # type: ignore[method-assign]
            with self.assertRaises(RuntimeError):
                await self.manager.start_launch("full", _force_external_probe=True)
            self.manager._terminate_state_process = terminated  # type: ignore[method-assign]
            await self.manager.stop_launch()
            await self.manager.shutdown()

        asyncio.run(scenario())
        self.assertEqual(started, [])
        terminated.assert_not_awaited()

    def test_start_preflight_forces_fresh_probe(self) -> None:
        async def scenario() -> None:
            self.manager._validate_group_start = lambda _group_id: None  # type: ignore[method-assign]
            refresh = AsyncMock(return_value=None)
            self.manager.refresh_core_observation = refresh  # type: ignore[method-assign]
            await self.manager.prepare_start_launch("potato")
            refresh.assert_awaited_once_with(force=True)

        asyncio.run(scenario())

    def test_direct_core_entrypoints_fresh_probe_and_noop_for_external_core(self) -> None:
        observation = process_manager.CoreObservation("ok", "potato", 0.0)

        async def scenario() -> None:
            refresh = AsyncMock(return_value=observation)
            self.manager.refresh_core_observation = refresh  # type: ignore[method-assign]
            start_locked = AsyncMock()
            self.manager._start_service_locked = start_locked  # type: ignore[method-assign]

            await self.manager.prepare_start_service("nachobot")
            await self.manager.prepare_start_group("core")
            await self.manager.start_service("nachobot")
            await self.manager.start_group("core")

            self.assertEqual(refresh.await_count, 4)
            self.assertTrue(all(call.kwargs == {"force": True} for call in refresh.await_args_list))
            self.assertEqual(self.manager.states, {})
            start_locked.assert_not_awaited()

        asyncio.run(scenario())

    def test_direct_start_launch_fresh_probe_without_prior_poll(self) -> None:
        observation = process_manager.CoreObservation("ok", "full", 0.0)
        started: list[str] = []

        async def fake_start_group(group_id: str) -> None:
            started.append(group_id)
            if group_id == "tts_full":
                for service_id in process_manager.GROUP_DEFS[group_id].services:
                    self.manager.states[service_id] = process_manager.ServiceState(
                        status=process_manager.ServiceStatus.RUNNING
                    )

        async def scenario() -> None:
            refresh = AsyncMock(side_effect=[observation, observation])
            self.manager.refresh_core_observation = refresh  # type: ignore[method-assign]
            self.manager.start_group = fake_start_group  # type: ignore[method-assign]

            await self.manager.start_launch("full")

            self.assertEqual(started, ["tts_full"])
            self.assertEqual(refresh.await_count, 2)
            self.assertTrue(all(call.kwargs == {"force": True} for call in refresh.await_args_list))
            self.assertNotIn("nachobot", self.manager.states)

        asyncio.run(scenario())

    def test_queued_core_request_yields_to_external_core_during_fresh_probe(self) -> None:
        observation = process_manager.CoreObservation("ok", "potato", 0.0)

        async def scenario() -> None:
            probe_started = asyncio.Event()
            release_probe = asyncio.Event()
            start_locked = AsyncMock()

            async def fresh_probe(*, force: bool = True):
                self.assertTrue(force)
                probe_started.set()
                await release_probe.wait()
                self.manager.core_observation = observation
                return observation

            self.manager.refresh_core_observation = fresh_probe  # type: ignore[method-assign]
            self.manager._start_service_locked = start_locked  # type: ignore[method-assign]

            self.manager.request_start_service("nachobot")
            operation = self.manager._operation_tasks["service:nachobot"]
            await probe_started.wait()
            self.assertEqual(
                self.manager.get_service_status("nachobot")["status"],
                process_manager.ServiceStatus.STARTING.value,
            )
            self.assertFalse(self.manager.get_service_status("nachobot")["managed"])
            release_probe.set()
            await operation

            status = self.manager.get_service_status("nachobot")
            self.assertEqual(status["status"], process_manager.ServiceStatus.RUNNING.value)
            self.assertEqual(status["origin"], "external")
            self.assertFalse(status["managed"])
            self.assertIsNone(status["pid"])
            self.assertNotIn("nachobot", self.manager.states)
            start_locked.assert_not_awaited()

        asyncio.run(scenario())

    def test_queued_full_dependency_request_yields_to_external_readiness(self) -> None:
        unavailable = process_manager.CoreObservation(
            "degraded",
            "full",
            0.0,
            True,
            True,
            False,
            True,
            False,
        )
        ready = process_manager.CoreObservation(
            "ok",
            "full",
            0.0,
            True,
            True,
            True,
            True,
            True,
        )

        async def scenario() -> None:
            self.manager.core_observation = unavailable
            probe_started = asyncio.Event()
            release_probe = asyncio.Event()
            start_locked = AsyncMock()

            async def fresh_probe(*, force: bool = True):
                self.assertTrue(force)
                probe_started.set()
                await release_probe.wait()
                self.manager.core_observation = ready
                return ready

            self.manager.refresh_core_observation = fresh_probe  # type: ignore[method-assign]
            self.manager._start_service_locked = start_locked  # type: ignore[method-assign]

            self.manager.request_start_service("tts_runtime_full")
            operation = self.manager._operation_tasks["service:tts_runtime_full"]
            await probe_started.wait()
            release_probe.set()
            await operation

            status = self.manager.get_service_status("tts_runtime_full")
            self.assertEqual(status["status"], process_manager.ServiceStatus.RUNNING.value)
            self.assertEqual(status["origin"], "external")
            self.assertFalse(status["managed"])
            self.assertIsNone(status["pid"])
            self.assertNotIn("tts_runtime_full", self.manager.states)
            start_locked.assert_not_awaited()

        asyncio.run(scenario())

    def test_queued_start_cancellation_clears_reservation_before_spawn(self) -> None:
        async def scenario() -> None:
            probe_started = asyncio.Event()
            release_probe = asyncio.Event()
            start_locked = AsyncMock()

            async def fresh_probe(*, force: bool = True):
                self.assertTrue(force)
                probe_started.set()
                await release_probe.wait()
                return None

            self.manager.refresh_core_observation = fresh_probe  # type: ignore[method-assign]
            self.manager._start_service_locked = start_locked  # type: ignore[method-assign]

            self.manager.request_start_service("nachobot")
            start_operation = self.manager._operation_tasks["service:nachobot"]
            await probe_started.wait()
            self.manager.request_stop_service("nachobot")
            stop_operation = self.manager._operation_tasks["service:nachobot"]
            await asyncio.gather(start_operation, stop_operation, return_exceptions=True)

            self.assertNotIn("nachobot", self.manager.states)
            start_locked.assert_not_awaited()

        asyncio.run(scenario())


if __name__ == "__main__":
    unittest.main()
