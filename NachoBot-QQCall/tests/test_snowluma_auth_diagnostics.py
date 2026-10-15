import unittest
import threading
import time
from types import SimpleNamespace
from unittest.mock import Mock

from service.manager import ServiceError, Supervisor, _safe_controller_failure_stage
from service.snowluma import SnowLumaBindingError
from service.windows import WindowsServiceError, WindowsSystem


class AuthDiagnosticsTests(unittest.TestCase):
    def test_monitor_diagnostics_distinguish_closed_binding_recheck_branches(self):
        manifest = dict(adapter="snowluma", accountId="2955610491", connected=True,
                        adapterPid=1, adapterCreationTime100ns="2", connectionGeneration=1)
        cases = (
            (None, True, "binding_manifest_unavailable"),
            ({**manifest, "connectionGeneration": 2}, True, "binding_manifest_changed"),
            (manifest, False, "binding_target_changed"),
            (manifest, ValueError("private runtime value"), "binding_recheck_exception"),
        )
        for current, result, stage in cases:
            with self.subTest(stage=stage):
                supervisor = object.__new__(Supervisor)
                supervisor.config = {}
                supervisor._read_manifest = lambda: current
                method = Mock(side_effect=result) if isinstance(result, Exception) else Mock(return_value=result)
                supervisor.system = SimpleNamespace(revalidate_binding_with_error=method)
                self.assertFalse(supervisor._binding_still_current(manifest, object()))
                with self.assertRaises(ServiceError) as caught:
                    supervisor._binding_still_current(manifest, object(), explain_failure=True)
                self.assertEqual(caught.exception._diagnostic_stage, stage)
                self.assertEqual(str(caught.exception), "adapter_binding_changed")
                self.assertNotIn("private", str(caught.exception))
        self.assertIsNone(_safe_controller_failure_stage("adapter_binding_changed", "private runtime value"))

    def test_binding_and_supervisor_preserve_only_fixed_auth_stages(self):
        for stage in ("snowluma_auth_runtime_resolution", "snowluma_auth_password_load",
                      "snowluma_auth_client_init", "snowluma_auth_login",
                      "snowluma_auth_process_list", "snowluma_auth_session_close"):
            with self.subTest(stage=stage):
                binding = SnowLumaBindingError("adapter_auth_unavailable", diagnostic_stage=stage)
                windows = WindowsServiceError(binding.code, diagnostic_stage=binding._diagnostic_stage)
                supervisor = ServiceError(windows.code, "waiting_adapter", diagnostic_stage=windows._diagnostic_stage)
                self.assertEqual(supervisor._diagnostic_stage, stage)
                self.assertEqual(_safe_controller_failure_stage(supervisor.code, supervisor._diagnostic_stage), stage)
                self.assertEqual(str(supervisor), "adapter_auth_unavailable")
        for value in ("private credential", {"password": "private"}, None, 1):
            self.assertIsNone(_safe_controller_failure_stage("adapter_auth_unavailable", value))
        self.assertIsNone(_safe_controller_failure_stage("other_failure", "snowluma_auth_login"))

    def test_strict_windows_recheck_preserves_error_and_boolean_api_remains_closed(self):
        system = object.__new__(WindowsSystem)
        error = WindowsServiceError("adapter_auth_unavailable", diagnostic_stage="snowluma_auth_login")
        system.bind_adapter = Mock(side_effect=error)
        with self.assertRaises(WindowsServiceError) as caught:
            system.revalidate_binding_with_error({}, {}, object())
        self.assertIs(caught.exception, error)
        self.assertFalse(system.revalidate_binding({}, {}, object()))
        self.assertEqual(system.bind_adapter.call_count, 2)

    def test_supervisor_preserves_auth_error_through_threaded_final_recheck(self):
        manifest = dict(adapter="snowluma", accountId="2955610491", connected=True,
                        adapterPid=1, adapterCreationTime100ns="2", connectionGeneration="fixed")
        supervisor = object.__new__(Supervisor)
        supervisor.config = {}
        supervisor._read_manifest = lambda: dict(manifest)
        supervisor._binding_fence_lock = threading.Lock()
        supervisor._identity_time_remaining = lambda deadline, **_: max(0, deadline-time.monotonic())
        strict = Mock(side_effect=WindowsServiceError(
            "adapter_auth_unavailable", diagnostic_stage="snowluma_auth_process_list"))
        legacy = Mock(return_value=True)
        supervisor.system = SimpleNamespace(revalidate_binding_with_error=strict, revalidate_binding=legacy)
        with self.assertRaises(ServiceError) as caught:
            supervisor._binding_still_current_until(manifest, object(), time.monotonic()+1)
        self.assertEqual((caught.exception.code, caught.exception.phase),
                         ("adapter_auth_unavailable", "waiting_adapter"))
        self.assertEqual(caught.exception._diagnostic_stage, "snowluma_auth_process_list")
        strict.assert_called_once()
        legacy.assert_not_called()
        self.assertFalse(supervisor._binding_fence_lock.locked())
