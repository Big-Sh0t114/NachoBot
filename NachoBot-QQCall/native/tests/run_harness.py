#!/usr/bin/env python3
"""Build and exercise the shim only in a disposable mock host process."""

from __future__ import annotations

import os
import queue
import re
import secrets
import subprocess
import sys
import threading
from pathlib import Path


ROOT = Path(__file__).resolve().parents[3]
NATIVE = Path(__file__).resolve().parents[1]
TESTS = Path(__file__).resolve().parent
OUT = TESTS / "build" / "x64"

NODE_VALIDATOR = r"""
const assert = require('node:assert/strict');
const vm = require('node:vm');
const source = require('node:fs').readFileSync(0, 'utf8');
new vm.Script(source, { filename: 'qqcall-bootstrap.js' });
function make(mode, behavior = 'ok', withImmediate = true) {
  const files = Object.create(null), queued = [], stderr = [];
  let agentCalls = 0;
  let createRequireCalls = 0;
  const fsMock = { writeFileSync(path, data) { files[path] = String(data); } };
  let context;
  const hostRequire = (name) => {
    if (name === 'fs') return fsMock;
    if (name === context.__NACHOBOT_QQCALL_BOOTSTRAP_CONFIG__.modulePath) {
      if (behavior === 'missing') throw new Error('module absent');
      return { bootstrap(config) {
        agentCalls++;
        fsMock.writeFileSync(config.reportPath + '.agent.json', JSON.stringify({ phase: 'agent_written' }));
        if (behavior === 'reject') return Promise.reject(new Error('agent failed'));
        if (behavior && typeof behavior === 'object' && Object.prototype.hasOwnProperty.call(behavior, 'rejection')) {
          return Promise.reject(behavior.rejection);
        }
        return Promise.resolve();
      }};
    }
    throw new Error('module unavailable');
  };
  context = {};
  context.process = { stderr: { write(value) { stderr.push(String(value)); } } };
  if (mode === 'global') context.require = hostRequire;
  if (mode === 'process') context.process.mainModule = { require: hostRequire };
  if (mode === 'esm') context.process.getBuiltinModule = (name) => {
      assert.equal(name, 'module');
      return { createRequire(filename) {
        assert.equal(filename, context.__NACHOBOT_QQCALL_BOOTSTRAP_CONFIG__.modulePath, 'createRequire must use the owned absolute modulePath');
        createRequireCalls++;
        return hostRequire;
      }};
    };
  if (mode === 'esm-unavailable') context.process.getBuiltinModule = () => ({});
  if (withImmediate) context.setImmediate = (callback) => { queued.push(callback); return 1; };
  const result = new vm.Script(source).runInNewContext(context);
  return { context, result, queued, files, stderr, get agentCalls() { return agentCalls; }, get createRequireCalls() { return createRequireCalls; } };
}
async function drain(test) {
  assert.equal(test.queued.length, 1);
  test.queued[0]();
  await Promise.resolve();
  await Promise.resolve();
  await Promise.resolve();
}
async function rejectedReport(error) {
  const test = make('global', { rejection: error });
  assert.equal(test.result, 3, 'bootstrap rejection is observed after scheduling');
  await drain(test);
  const reportPath = test.context.__NACHOBOT_QQCALL_BOOTSTRAP_CONFIG__.reportPath;
  const report = JSON.parse(test.files[reportPath + '.bootstrap.json']);
  return { test, report, serialized: JSON.stringify(report) + test.stderr.join('') };
}
(async () => {
  const success = make('global');
  assert.equal(success.result, 3, 'global require should schedule the bootstrap');
  await drain(success);
  const report = success.context.__NACHOBOT_QQCALL_BOOTSTRAP_CONFIG__.reportPath;
  assert.equal(success.agentCalls, 1);
  assert.equal(JSON.parse(success.files[report + '.agent.json']).phase, 'agent_written');
  assert.equal(JSON.parse(success.files[report + '.bootstrap.json']).phase, 'bootstrap_returned');
  assert.equal(success.files[report + '.agent.json'].includes('bootstrap_returned'), false, 'loader must not overwrite agent report');

  const mainModule = make('process');
  assert.equal(mainModule.result, 3, 'process.mainModule.require should be a supported fallback');
  await drain(mainModule);
  assert.equal(mainModule.agentCalls, 1);

  const esmWorker = make('esm');
  assert.equal(esmWorker.result, 3, 'ESM workers should load the CJS shim bootstrap through builtin module.createRequire');
  await drain(esmWorker);
  assert.equal(esmWorker.createRequireCalls, 1);
  assert.equal(esmWorker.agentCalls, 1);

  const esmUnavailable = make('esm-unavailable');
  assert.equal(esmUnavailable.result, 1, 'missing createRequire must remain an observable loader-unavailable result');
  assert.equal(esmUnavailable.queued.length, 0);

  const noLoader = make('none');
  assert.equal(noLoader.result, 1, 'missing host loader should be observable');
  assert.equal(noLoader.queued.length, 0);

  const noImmediate = make('global', 'ok', false);
  assert.equal(noImmediate.result, 2, 'missing setImmediate should be observable');

  const missingModule = make('global', 'missing');
  assert.equal(missingModule.result, 3);
  await drain(missingModule);
  const missingReport = missingModule.context.__NACHOBOT_QQCALL_BOOTSTRAP_CONFIG__.reportPath;
  assert.equal(JSON.parse(missingModule.files[missingReport + '.bootstrap.json']).errorCode, 'MODULE_REQUIRE_FAILED');
  assert.equal(missingModule.agentCalls, 0);

  const allowedCodes = [
    'bootstrap_config_missing', 'bootstrap_module_path_invalid', 'app_path_invalid', 'invalid_pipe_name',
    'generation_invalid', 'session_name_invalid', 'auth_token_invalid', 'wrapper_module_path_invalid',
    'report_path_invalid', 'explicit_attach_required', 'bootstrap_initializing', 'duplicate_bootstrap_active',
    'pipe_disconnected', 'invalid_generation', 'invalid_auth_token', 'pipe_client_closed', 'pipe_connect_failed',
    'pipe_not_connected', 'request_timeout', 'frame_too_large', 'backpressure_limit', 'empty_frame',
    'malformed_json', 'unterminated_frame', 'invalid_envelope', 'unsupported_schema', 'invalid_message_type',
    'invalid_sequence', 'out_of_order_sequence', 'stale_generation', 'unauthorized', 'invalid_request_id',
    'event_request_id_must_be_null', 'request_id_required', 'operation_required', 'unexpected_response',
    'response_operation_mismatch', 'request_failed', 'message_handler_failed', 'invalid_broker_frame', 'pipe_frame_error',
  ];
  const privateMessage = 'private-bootstrap-rejection-token-7c5f';
  for (const code of allowedCodes) {
    const error = new Error(privateMessage);
    error.code = code;
    const { test, report, serialized } = await rejectedReport(error);
    assert.equal(report.phase, 'bootstrap_failed');
    assert.equal(report.errorCode, code, `allowlisted own data code should be retained: ${code}`);
    assert.equal(serialized.includes(privateMessage), false, 'rejection messages and stacks must not be emitted');
    assert.equal(test.agentCalls, 1);
  }

  const rawToken = 'raw-agent-private-token-a31f';
  const rawTokenError = new Error(`rejected with ${rawToken}`);
  rawTokenError.code = rawToken;
  const rawTokenResult = await rejectedReport(rawTokenError);
  assert.equal(rawTokenResult.report.errorCode, 'BOOTSTRAP_REJECTED');
  assert.equal(rawTokenResult.serialized.includes(rawToken), false, 'arbitrary codes and messages must not leak tokens');

  const prefixedError = new Error(privateMessage);
  prefixedError.code = `wrapper_module_path_invalid:${rawToken}`;
  const prefixedResult = await rejectedReport(prefixedError);
  assert.equal(prefixedResult.report.errorCode, 'BOOTSTRAP_REJECTED', 'a known-code prefix must not accept an arbitrary suffix');
  assert.equal(prefixedResult.serialized.includes(rawToken), false);

  let getterReads = 0;
  const accessorError = {};
  Object.defineProperties(accessorError, {
    code: { get() { getterReads++; return 'pipe_disconnected'; } },
    message: { get() { getterReads++; return rawToken; } },
    stack: { get() { getterReads++; return rawToken; } },
  });
  const accessorResult = await rejectedReport(accessorError);
  assert.equal(accessorResult.report.errorCode, 'BOOTSTRAP_REJECTED', 'accessor properties must not supply diagnostic values');
  assert.equal(getterReads, 0, 'the writer must not invoke error property getters');
  assert.equal(accessorResult.serialized.includes(rawToken), false);

  const inheritedError = Object.create({ code: 'pipe_disconnected' });
  const inheritedResult = await rejectedReport(inheritedError);
  assert.equal(inheritedResult.report.errorCode, 'BOOTSTRAP_REJECTED', 'only an own code data property is trusted');

  process.stdout.write(`PASS safe bootstrap rejection codes (${allowedCodes.length} allowlisted), sensitive and accessor failures remain generic\n`);
  process.stdout.write('PASS vm syntax, loader fallback/unavailable, scheduling, module failure containment, report ownership\n');
})().catch((error) => { process.stderr.write(String(error.stack || error) + '\n'); process.exitCode = 1; });
"""


def run_build() -> None:
    # Harness binaries are disposable and must never rebuild/replace the
    # production shim: Recovery2 may be loaded by a live target process.
    script = TESTS / "build_harness.ps1"
    completed = subprocess.run(
        ["powershell", "-NoProfile", "-ExecutionPolicy", "Bypass", "-File", str(script)],
        cwd=ROOT,
        check=False,
    )
    if completed.returncode:
        raise SystemExit(f"build failed: {script} ({completed.returncode})")


def make_config(path: Path, pid: int) -> None:
    token = secrets.token_urlsafe(48)
    values = {
        "modulePath": r"C:\mock\agent\bootstrap.cjs",
        "pipeName": rf"\\.\pipe\nachobot-qqcall-{pid}-{secrets.token_hex(16)}",
        "appPath": r"C:\mock\QQ",
        "sessionName": "isolated_probe",
        "reportPath": str(OUT / f"native-test-{pid}"),
        "ipcToken": token,
        "connectionGeneration": 1,
    }
    content = "[QQCallShim]\r\n" + "".join(f"{key}={value}\r\n" for key, value in values.items())
    path.write_text(content, encoding="utf-16")


def run_scenario(name: str) -> str:
    exe = OUT / "qqcall_harness.exe"
    config = OUT / f"native-test-{os.getpid()}-{name}.ini"
    process = subprocess.Popen(
        [str(exe), name, str(config)],
        cwd=OUT,
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
    )
    try:
        assert process.stdout is not None and process.stdin is not None
        ready_queue: queue.Queue[bytes] = queue.Queue(maxsize=1)
        threading.Thread(target=lambda: ready_queue.put(process.stdout.readline()), daemon=True).start()
        try:
            ready_bytes = ready_queue.get(timeout=10)
        except queue.Empty:
            process.kill()
            process.communicate(timeout=5)
            raise RuntimeError(f"harness scenario {name} did not emit READY within 10 seconds")
        ready = ready_bytes.decode("ascii", errors="replace").strip()
        match = re.fullmatch(r"READY (\d+)", ready)
        if not match:
            process.kill()
            tail = process.communicate(timeout=5)[0].decode("utf-8", errors="replace")
            raise RuntimeError(f"harness did not become ready for {name}: {ready!r} {tail}")
        make_config(config, int(match.group(1)))
        process.stdin.write(b"x")
        process.stdin.close()
        process.stdin = None
        try:
            output_bytes, _ = process.communicate(timeout=20)
        except subprocess.TimeoutExpired as exc:
            process.kill()
            output_bytes, _ = process.communicate(timeout=5)
            raise RuntimeError(f"harness scenario {name} exceeded 20 seconds:\n{output_bytes.decode('utf-8', errors='replace')}") from exc
        output = output_bytes.decode("utf-8", errors="replace")
        code = process.returncode
        if code != 0 or "PASS flags=" not in output:
            raise RuntimeError(f"harness scenario {name} failed (exit {code}):\n{output}")
        if name == "success":
            script_match = re.search(r"SCRIPT_BEGIN\n(.*?)\nSCRIPT_END\n", output, re.DOTALL)
            if not script_match:
                raise RuntimeError("successful scenario did not return its generated bootstrap script")
            validator = subprocess.run(
                ["node", "-e", NODE_VALIDATOR],
                input=script_match.group(1),
                text=True,
                capture_output=True,
                cwd=ROOT,
                check=False,
            )
            if validator.returncode:
                raise RuntimeError(f"generated bootstrap JS validation failed:\n{validator.stdout}{validator.stderr}")
            print(validator.stdout.strip())
        print(f"PASS native mock scenario: {name}")
        return output
    finally:
        if process.poll() is None:
            process.kill()
            process.wait(timeout=5)
        try:
            config.unlink(missing_ok=True)
        except OSError:
            pass


def main() -> int:
    if os.name != "nt":
        raise SystemExit("native QQ shim harness requires Windows x64")
    run_build()
    for scenario in (
        "success",
        "overlapping-starts",
        "cancel",
        "preexisting-exception",
        "shim-exception",
        "original-fails",
        "loader-unavailable",
        "setimmediate-unavailable",
        "schedule-throws",
        "foreign-hook",
    ):
        output = run_scenario(scenario)
        summary = re.search(r"PASS flags=(.*)", output)
        if summary:
            print(f"  status {summary.group(1)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
