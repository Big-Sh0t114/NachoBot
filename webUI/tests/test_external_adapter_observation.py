from __future__ import annotations

import asyncio
from pathlib import Path
from types import SimpleNamespace
import sys
import unittest
from unittest.mock import AsyncMock, MagicMock, patch


WEBUI_DIR = Path(__file__).resolve().parents[1]
if str(WEBUI_DIR) not in sys.path:
    sys.path.insert(0, str(WEBUI_DIR))

import process_manager  # noqa: E402


class _Process:
    def __init__(self, info=None, error: BaseException | None = None):
        self._info = info
        self._error = error

    @property
    def info(self):
        if self._error is not None:
            raise self._error
        return self._info


class ExternalAdapterObservationTests(unittest.TestCase):
    def setUp(self) -> None:
        process_manager._register_services(WEBUI_DIR.parent)
        self.manager = process_manager.ProcessManager(WEBUI_DIR.parent)

    def _record(
        self,
        service_id: str,
        pid: int,
        *,
        ppid: int | None = None,
        cwd: str | None = None,
        argv: tuple[str, ...] | None = None,
        name: str | None = None,
        executable: str | None = None,
    ) -> process_manager._ProcessRecord:
        expected_cwd = cwd or self.manager._external_service_cwd(service_id)
        if service_id == "snowluma_runtime":
            argv = argv or ("node", "index.mjs")
            name = name or "node.exe"
            executable = executable or "node.exe"
        elif service_id == "live2d":
            argv = argv or ("python", "-m", "live2d_adapter", "--config", "config.toml")
            name = name or "python.exe"
            executable = executable or "python.exe"
        elif service_id == "koishi":
            argv = argv or ("node", "node_modules/@koishijs/cli/lib/index.js", "start")
            name = name or "node.exe"
            executable = executable or "node.exe"
        elif service_id == "napcat_shell":
            argv = argv or ("launcher-user.bat",)
            name = name or "NapCat.Shell.exe"
            executable = executable or "NapCat.Shell.exe"
        else:
            argv = argv or ("python", "main.py")
            name = name or "python.exe"
            executable = executable or "python.exe"
        return process_manager._ProcessRecord(
            pid=pid,
            ppid=ppid,
            cwd=expected_cwd,
            argv=tuple(argv),
            executable=executable,
            name=name,
        )

    @staticmethod
    def _connection(pid: int, port: int):
        return SimpleNamespace(status="LISTEN", pid=pid, laddr=("127.0.0.1", port))

    @staticmethod
    def _observations(outcomes: dict[str, str] | None = None) -> dict[str, process_manager.AdapterObservation]:
        outcomes = outcomes or {}
        return {
            service_id: process_manager.AdapterObservation(
                service_id,
                outcomes.get(service_id, "absent"),
                0.0,
                "external evidence",
            )
            for service_id in process_manager.EXTERNAL_ADAPTER_SERVICE_IDS
        }

    def test_snapshot_classifies_all_ten_services_with_direct_and_descendant_ports(self) -> None:
        records = [
            self._record("napcat_adapter", 101),
            self._record("snowluma_adapter", 102),
            self._record("snowluma_runtime", 103),
            self._record("bilibili", 104),
            self._record("live2d", 105),
            self._record("koishi", 106),
            self._record("koishi_adapter", 107),
            self._record("discordvc", 108),
            self._record("universalvc", 109),
            self._record("napcat_shell", 110),
            self._record(
                "napcat_shell",
                111,
                ppid=110,
                name="QQ.exe",
                executable="QQ.exe",
            ),
        ]
        listeners = {
            process_manager.SERVICE_DEFS["napcat_adapter"].port: frozenset({101}),
            process_manager.SERVICE_DEFS["snowluma_runtime"].port: frozenset({103}),
            process_manager.SERVICE_DEFS["live2d"].port: frozenset({105}),
            process_manager.SERVICE_DEFS["koishi"].port: frozenset({106}),
        }
        snapshot = process_manager._ProcessSnapshot(tuple(records), listeners)

        result = self.manager._classify_external_adapters(snapshot)

        self.assertEqual(set(result), set(process_manager.EXTERNAL_ADAPTER_SERVICE_IDS))
        self.assertTrue(all(item.outcome == "external_ready" for item in result.values()))

    def test_wrong_cwd_is_negative_and_port_owner_accepts_direct_or_descendant(self) -> None:
        wrong = self._record(
            "napcat_adapter",
            120,
            cwd=f"{self.manager._external_service_cwd('napcat_adapter')}-wrong",
        )
        wrong_result = self.manager._classify_external_adapters(
            process_manager._ProcessSnapshot((wrong,), {})
        )
        self.assertEqual(wrong_result["napcat_adapter"].outcome, "absent")

        direct = self._record("napcat_adapter", 121)
        direct_result = self.manager._classify_external_adapters(
            process_manager._ProcessSnapshot(
                (direct,),
                {process_manager.SERVICE_DEFS["napcat_adapter"].port: frozenset({121})},
            )
        )
        self.assertEqual(direct_result["napcat_adapter"].outcome, "external_ready")

        root = self._record("napcat_adapter", 122)
        child = self._record("napcat_adapter", 123, ppid=122, argv=("python", "worker.py"))
        descendant_result = self.manager._classify_external_adapters(
            process_manager._ProcessSnapshot(
                (root, child),
                {process_manager.SERVICE_DEFS["napcat_adapter"].port: frozenset({123})},
            )
        )
        self.assertEqual(descendant_result["napcat_adapter"].outcome, "external_ready")

    def test_unrelated_occupied_port_is_indeterminate(self) -> None:
        port = process_manager.SERVICE_DEFS["napcat_adapter"].port
        result = self.manager._classify_external_adapters(
            process_manager._ProcessSnapshot((), {port: frozenset({9999})})
        )
        self.assertEqual(result["napcat_adapter"].outcome, "indeterminate")
        self.assertEqual(result["bilibili"].outcome, "absent")

    def test_present_unready_adapter_is_nonowning_error_without_pid(self) -> None:
        record = self._record("napcat_adapter", 125)
        result = self.manager._classify_external_adapters(
            process_manager._ProcessSnapshot((record,), {})
        )
        self.assertEqual(result["napcat_adapter"].outcome, "external_present_unready")
        self.manager.adapter_observation_cache["napcat_adapter"] = result["napcat_adapter"]
        status = self.manager.get_service_status("napcat_adapter")
        self.assertEqual(status["status"], "error")
        self.assertEqual(status["external_state"], "present_unready")
        self.assertFalse(status["managed"])
        self.assertIsNone(status["pid"])

    def test_global_process_or_listener_scan_failure_is_indeterminate(self) -> None:
        with patch.object(process_manager.psutil, "process_iter", side_effect=PermissionError("table")), \
             patch.object(process_manager.psutil, "net_connections", return_value=[]):
            snapshot = self.manager._build_external_process_snapshot()
        self.assertTrue(snapshot.failed)
        result = self.manager._classify_external_adapters(snapshot)
        self.assertTrue(all(item.outcome == "indeterminate" for item in result.values()))

        with patch.object(process_manager.psutil, "process_iter", return_value=[]), \
             patch.object(process_manager.psutil, "net_connections", side_effect=PermissionError("listeners")):
            listener_failure = self.manager._build_external_process_snapshot()
        self.assertTrue(listener_failure.failed)

    def test_unrelated_per_process_access_does_not_fail_global_snapshot(self) -> None:
        valid = self._record("bilibili", 130)
        inaccessible = _Process(error=PermissionError("unrelated process"))
        visible = _Process(
            {
                "pid": valid.pid,
                "ppid": valid.ppid,
                "cwd": valid.cwd,
                "cmdline": list(valid.argv),
                "exe": valid.executable,
                "name": valid.name,
            }
        )
        with patch.object(process_manager.psutil, "process_iter", return_value=[inaccessible, visible]), \
             patch.object(process_manager.psutil, "net_connections", return_value=[]):
            snapshot = self.manager._build_external_process_snapshot()
        self.assertFalse(snapshot.failed)
        result = self.manager._classify_external_adapters(snapshot)
        self.assertEqual(result["bilibili"].outcome, "external_ready")

    def test_duplicate_candidates_are_indeterminate(self) -> None:
        first = self._record("bilibili", 140)
        second = self._record("bilibili", 141)
        result = self.manager._classify_external_adapters(
            process_manager._ProcessSnapshot((first, second), {})
        )
        self.assertEqual(result["bilibili"].outcome, "indeterminate")

    def test_napcat_shell_requires_launcher_or_runtime_identity(self) -> None:
        cwd = self.manager._external_service_cwd("napcat_shell")
        plain_terminal = self._record(
            "napcat_shell",
            150,
            cwd=cwd,
            argv=("cmd.exe",),
            name="cmd.exe",
            executable="cmd.exe",
        )
        plain_result = self.manager._classify_external_adapters(
            process_manager._ProcessSnapshot((plain_terminal,), {})
        )
        self.assertEqual(plain_result["napcat_shell"].outcome, "absent")

        paused_launcher = self._record(
            "napcat_shell",
            151,
            cwd=cwd,
            argv=("cmd.exe", "/c", "launcher-user.bat"),
            name="cmd.exe",
            executable="cmd.exe",
        )
        paused_result = self.manager._classify_external_adapters(
            process_manager._ProcessSnapshot((paused_launcher,), {})
        )
        self.assertEqual(
            paused_result["napcat_shell"].outcome,
            "external_present_unready",
        )

        runtime = self._record(
            "napcat_shell",
            152,
            cwd=cwd,
            argv=("NapCatWinBootMain.exe",),
            name="NapCatWinBootMain.exe",
            executable="NapCatWinBootMain.exe",
        )
        runtime_result = self.manager._classify_external_adapters(
            process_manager._ProcessSnapshot((runtime,), {})
        )
        self.assertEqual(runtime_result["napcat_shell"].outcome, "external_ready")

    def test_manager_owned_service_precedes_external_and_external_pid_is_null(self) -> None:
        self.manager.adapter_observation_cache["bilibili"] = process_manager.AdapterObservation(
            "bilibili", "external_ready", 0.0, "external evidence"
        )
        external_status = self.manager.get_service_status("bilibili")
        self.assertEqual(external_status["status"], "running")
        self.assertFalse(external_status["managed"])
        self.assertEqual(external_status["origin"], "external")
        self.assertIsNone(external_status["pid"])

        self.manager.states["bilibili"] = process_manager.ServiceState(
            status=process_manager.ServiceStatus.RUNNING,
            pid=4321,
        )
        owned_status = self.manager.get_service_status("bilibili")
        self.assertTrue(owned_status["managed"])
        self.assertEqual(owned_status["origin"], "webui")
        self.assertEqual(owned_status["pid"], 4321)

    def test_fresh_start_skips_external_and_refuses_disappearance_race(self) -> None:
        selected = process_manager.GROUP_DEFS["qq_adapter"].services[0]
        ready = self._observations({selected: "external_ready"})
        absent = self._observations()

        async def scenario() -> None:
            self.manager._ensure_required_components = lambda _ids: None  # type: ignore[method-assign]
            self.manager._fresh_external_adapter_observations = AsyncMock(  # type: ignore[method-assign]
                return_value=ready
            )
            locked = AsyncMock()
            self.manager._start_service_locked = locked  # type: ignore[method-assign]
            await self.manager.start_service(selected)
            locked.assert_not_awaited()
            self.assertNotIn(selected, self.manager.states)

            self.manager._adapter_start_preflight[selected] = ready[selected]
            self.manager._fresh_external_adapter_observations = AsyncMock(  # type: ignore[method-assign]
                return_value=absent
            )
            self.manager.core_observation = process_manager.CoreObservation("ok", "potato", 0.0)
            self.manager.refresh_core_observation = AsyncMock(  # type: ignore[method-assign]
                return_value=self.manager.core_observation
            )
            with self.assertRaisesRegex(RuntimeError, "状态发生变化"):
                await self.manager.start_service(selected)
            locked.assert_not_awaited()

        asyncio.run(scenario())

    def test_mutation_fresh_scan_does_not_reuse_inflight_status_probe(self) -> None:
        stale = self._observations({"bilibili": "external_ready"})
        fresh = self._observations()

        async def scenario() -> None:
            release = asyncio.Event()

            async def old_probe():
                await release.wait()
                return stale

            old_task = asyncio.create_task(old_probe())
            self.manager._adapter_probe_task = old_task
            with patch.object(self.manager, "_scan_external_adapters", return_value=fresh) as scan:
                observed = await self.manager.refresh_adapter_observation_for_mutation()
            self.assertEqual(observed["bilibili"].outcome, "absent")
            scan.assert_called_once_with(exclude_pids=frozenset())
            self.assertIs(self.manager._adapter_probe_task, old_task)
            old_task.cancel()
            await asyncio.gather(old_task, return_exceptions=True)

        asyncio.run(scenario())

    def test_late_status_poll_cannot_replace_newer_mutation_snapshot(self) -> None:
        stale = self._observations({"bilibili": "external_ready"})
        fresh = self._observations()

        async def scenario() -> None:
            release = asyncio.Event()

            async def old_probe():
                await release.wait()
                return stale

            self.manager._adapter_observation_generation = 1
            self.manager._adapter_probe_task_generation = 1
            self.manager._adapter_probe_task = asyncio.create_task(old_probe())
            old_waiter = asyncio.create_task(self.manager.refresh_adapter_observation())
            await asyncio.sleep(0)
            with patch.object(self.manager, "_scan_external_adapters", return_value=fresh):
                mutation = await self.manager.refresh_adapter_observation_for_mutation()
            self.assertEqual(mutation["bilibili"].outcome, "absent")

            release.set()
            observed = await old_waiter
            self.assertEqual(observed["bilibili"].outcome, "absent")
            self.assertEqual(self.manager.adapter_observation_cache["bilibili"].outcome, "absent")

        asyncio.run(scenario())

    def test_server_mutation_endpoints_use_boundary_fresh_adapter_scan(self) -> None:
        source = (WEBUI_DIR / "server.py").read_text(encoding="utf-8")
        self.assertNotIn("refresh_adapter_observation(force=True)", source)
        self.assertEqual(
            source.count("refresh_adapter_observation_for_mutation()"),
            6,  # five mutation boundaries plus the explicit fresh group read
        )

    def test_group_snapshot_can_request_independent_fresh_scan(self) -> None:
        import server

        async def scenario() -> None:
            with patch.object(server, "process_mgr", self.manager), \
                 patch.object(self.manager, "refresh_core_observation", new_callable=AsyncMock) as core_probe, \
                 patch.object(self.manager, "refresh_adapter_observation_for_mutation", new_callable=AsyncMock) as adapter_probe, \
                 patch.object(self.manager, "refresh_external_observation", new_callable=AsyncMock) as shared_probe, \
                 patch.object(self.manager, "get_group_statuses", return_value=[{"id": "bilibili"}]):
                result = await server.get_groups(fresh=True)
                self.assertEqual(result, [{"id": "bilibili"}])
                core_probe.assert_awaited_once_with(force=True)
                adapter_probe.assert_awaited_once_with()
                shared_probe.assert_not_awaited()

                await server.get_groups()
                shared_probe.assert_awaited_once_with(force=True)

        asyncio.run(scenario())

    def test_launch_status_refreshes_core_without_scanning_adapters(self) -> None:
        import server

        async def scenario() -> None:
            expected = {"status": "running", "active_profile": "full"}
            with patch.object(server, "process_mgr", self.manager), \
                 patch.object(self.manager, "refresh_core_observation", new_callable=AsyncMock) as core_probe, \
                 patch.object(self.manager, "refresh_adapter_observation_for_mutation", new_callable=AsyncMock) as adapter_probe, \
                 patch.object(self.manager, "refresh_external_observation", new_callable=AsyncMock) as shared_probe, \
                 patch.object(self.manager, "get_launch_status", return_value=expected):
                result = await server.get_launch_status()
                self.assertEqual(result, expected)
                core_probe.assert_awaited_once_with(force=True)
                adapter_probe.assert_not_awaited()
                shared_probe.assert_not_awaited()

        asyncio.run(scenario())

    def test_global_status_reuses_adapter_cache_between_launcher_polls(self) -> None:
        import server

        async def scenario() -> None:
            groups = [{"id": "bilibili", "name": "Bilibili", "icon": "tv",
                       "services": [{"status": "running"}, {"status": "stopped"}]}]
            with patch.object(server, "process_mgr", self.manager), \
                 patch.object(self.manager, "refresh_core_observation", new_callable=AsyncMock) as core_probe, \
                 patch.object(self.manager, "refresh_external_observation", new_callable=AsyncMock) as shared_probe, \
                 patch.object(self.manager, "refresh_adapter_observation", new_callable=AsyncMock) as adapter_probe, \
                 patch.object(self.manager, "get_group_statuses", return_value=groups):
                result = await server.get_status()
                self.assertEqual(result["bilibili"]["running"], 1)
                self.assertEqual(result["bilibili"]["total"], 2)
                core_probe.assert_awaited_once_with(force=True)
                shared_probe.assert_not_awaited()
                adapter_probe.assert_not_awaited()

        asyncio.run(scenario())

    def test_post_spawn_port_owner_excludes_owned_tree_and_rejects_duplicate(self) -> None:
        service_id = "napcat_adapter"
        port = process_manager.SERVICE_DEFS[service_id].port
        owned = self._record(service_id, 200)
        state = process_manager.ServiceState(
            status=process_manager.ServiceStatus.STARTING,
            pid=200,
        )

        async def scenario() -> None:
            owned_snapshot = process_manager._ProcessSnapshot(
                (owned,),
                {port: frozenset({200})},
            )
            with patch.object(self.manager, "_build_external_process_snapshot", return_value=owned_snapshot):
                observations = await self.manager._external_duplicate_after_spawn(service_id, state)
            self.assertEqual(observations[service_id].outcome, "absent")

            duplicate = self._record(service_id, 201)
            duplicate_snapshot = process_manager._ProcessSnapshot(
                (owned, duplicate),
                {port: frozenset({200, 201})},
            )
            with patch.object(self.manager, "_build_external_process_snapshot", return_value=duplicate_snapshot):
                with self.assertRaisesRegex(RuntimeError, "并发外部"):
                    await self.manager._external_duplicate_after_spawn(service_id, state)

        asyncio.run(scenario())

    def test_post_spawn_qq_check_rejects_new_opposite_backend(self) -> None:
        service_id = "napcat_adapter"
        port = process_manager.SERVICE_DEFS[service_id].port
        owned = self._record(service_id, 210)
        opposite = self._record("snowluma_adapter", 211)
        snapshot = process_manager._ProcessSnapshot(
            (owned, opposite),
            {port: frozenset({210})},
        )
        state = process_manager.ServiceState(
            status=process_manager.ServiceStatus.STARTING,
            pid=210,
        )

        async def scenario() -> None:
            with patch.object(self.manager, "_build_external_process_snapshot", return_value=snapshot):
                with self.assertRaisesRegex(RuntimeError, "external"):
                    await self.manager._external_duplicate_after_spawn(service_id, state)

        asyncio.run(scenario())

    def test_qq_fresh_scan_caches_all_four_and_rejects_nonselected_external_backend(self) -> None:
        selected = tuple(process_manager.GROUP_DEFS["qq_adapter"].services)
        conflicting = process_manager.ProcessManager._qq_conflicting_service_ids(selected)
        observations = self._observations({conflicting[0]: "external_ready"})

        async def scenario() -> None:
            with patch.object(self.manager, "_scan_external_adapters", return_value=observations):
                with self.assertRaises(RuntimeError):
                    await self.manager.prepare_start_group("qq_adapter")
            self.assertTrue(
                set(process_manager.QQ_SERVICE_IDS).issubset(self.manager.adapter_observation_cache)
            )
            with self.assertRaises(ValueError):
                process_manager.assert_qq_adapter_switch_allowed(self.manager)

        asyncio.run(scenario())

    def test_fully_external_selected_qq_group_is_a_noop(self) -> None:
        selected = tuple(process_manager.GROUP_DEFS["qq_adapter"].services)
        observations = self._observations({service_id: "external_ready" for service_id in selected})

        async def scenario() -> None:
            self.manager._ensure_required_components = lambda _ids: None  # type: ignore[method-assign]
            self.manager._ensure_snowluma_launch_boundary = MagicMock()  # type: ignore[method-assign]
            self.manager.refresh_core_observation = AsyncMock(return_value=None)  # type: ignore[method-assign]
            with patch.object(self.manager, "_scan_external_adapters", return_value=observations):
                await self.manager.start_group("qq_adapter")
            self.assertEqual(self.manager.states, {})
            for service_id in selected:
                status = self.manager.get_service_status(service_id)
                self.assertEqual(status["status"], "running")
                self.assertFalse(status["managed"])
                self.assertIsNone(status["pid"])

        asyncio.run(scenario())

    def test_snowluma_adapter_reuses_external_runtime_without_free_port_requirement(self) -> None:
        self.manager.core_observation = process_manager.CoreObservation("ok", "potato", 0.0)
        self.manager.adapter_observation_cache["snowluma_runtime"] = process_manager.AdapterObservation(
            "snowluma_runtime", "external_ready", 0.0, "external evidence"
        )
        boundary = MagicMock()
        self.manager._ensure_snowluma_launch_boundary = boundary  # type: ignore[method-assign]
        self.manager._validate_service_start("snowluma_adapter")
        self.assertTrue(boundary.call_args.kwargs.get("require_free_ports") is False)

    def test_mixed_group_rolls_back_only_webui_owned_services_and_stop_preserves_external(self) -> None:
        external_id = process_manager.GROUP_DEFS["discord"].services[0]
        owned_id = process_manager.GROUP_DEFS["discord"].services[1]
        failed_id = process_manager.GROUP_DEFS["discord"].services[2]
        self.manager.adapter_observation_cache[external_id] = process_manager.AdapterObservation(
            external_id, "external_ready", 0.0, "external evidence"
        )
        self.manager._validate_group_start = MagicMock()  # type: ignore[method-assign]
        self.manager._ensure_required_components = lambda _ids: None  # type: ignore[method-assign]
        self.manager._broadcast = AsyncMock()  # type: ignore[method-assign]
        stopped: list[str] = []

        async def fake_start(service_id: str, **_kwargs) -> None:
            if service_id == external_id:
                return
            status = process_manager.ServiceStatus.ERROR if service_id == failed_id else process_manager.ServiceStatus.RUNNING
            self.manager.states[service_id] = process_manager.ServiceState(status=status, pid=7000)

        async def fake_stop(service_id: str, **_kwargs) -> None:
            stopped.append(service_id)

        async def scenario() -> None:
            self.manager.start_service = fake_start  # type: ignore[method-assign]
            self.manager.stop_service = fake_stop  # type: ignore[method-assign]
            with patch.object(process_manager.asyncio, "sleep", new=AsyncMock()):
                await self.manager.start_group("discord")

        asyncio.run(scenario())
        self.assertEqual(stopped, [owned_id])
        self.assertNotIn(external_id, stopped)

        del self.manager.stop_service
        self.manager.states.pop(failed_id, None)
        self.manager.states[owned_id] = process_manager.ServiceState(
            status=process_manager.ServiceStatus.RUNNING,
            pid=7001,
        )
        terminated = AsyncMock()
        self.manager._terminate_state_process = terminated  # type: ignore[method-assign]
        asyncio.run(self.manager.stop_group("discord"))
        terminated.assert_awaited_once()
        self.assertEqual(self.manager.states[owned_id].status, process_manager.ServiceStatus.STOPPED)

    def test_group_start_exception_rolls_back_earlier_owned_service(self) -> None:
        first_id, second_id, *_ = process_manager.GROUP_DEFS["discord"].services
        self.manager._validate_group_start = MagicMock()  # type: ignore[method-assign]
        self.manager._ensure_required_components = lambda _ids: None  # type: ignore[method-assign]
        stopped: list[str] = []

        async def fake_start(service_id: str, **_kwargs) -> None:
            if service_id == second_id:
                raise RuntimeError("later fresh guard failed")
            self.manager.states[service_id] = process_manager.ServiceState(
                status=process_manager.ServiceStatus.RUNNING,
                pid=7200,
            )

        async def fake_stop(service_id: str, **_kwargs) -> None:
            stopped.append(service_id)

        async def scenario() -> None:
            self.manager.start_service = fake_start  # type: ignore[method-assign]
            self.manager.stop_service = fake_stop  # type: ignore[method-assign]
            with patch.object(process_manager.asyncio, "sleep", new=AsyncMock()):
                with self.assertRaisesRegex(RuntimeError, "later fresh guard failed"):
                    await self.manager.start_group("discord")

        asyncio.run(scenario())
        self.assertEqual(stopped, [first_id])


if __name__ == "__main__":
    unittest.main()
