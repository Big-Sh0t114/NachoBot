# Windows AV Host audio route guard prototype

This is an opt-in, Windows-only prototype for the observed QQ 9.9.26-44343 AV
Host path. It has two separate artifacts: a parent `.node` module that owns the
process-creation barrier, and a child `.dll` that installs the Core Audio guard
before the suspended PPAPI or AudioService process resumes. The parent must opt in before any
AudioService candidate exists. Finding an existing direct child with the
configured QQ image blocks installation; a direct child whose image cannot be
inspected also blocks installation because its startup timing cannot be proven.

The binding is deliberately narrow: only a child whose image is exactly the
configured QQ executable and whose own command line contains both documented
exact `--type=ppapi` or the AudioService utility switches is eligible. The injector forces suspension, binds
PID plus FILETIME and generation into owner-ACL named objects, injects only the
paired guard DLL, waits for child-side identity/target/hook readiness, then
resumes only when the original caller did not request suspension. A failed
target launch terminates only the process returned by that same create call and
clears its output `PROCESS_INFORMATION`.

The cold-child loader path pins QQ.exe SHA-256
`8088142f4e6b31615c90ce576beaf88d3fee343c15d8f6e60b4bf8cd3df58eea`.
An owned compiler-verified no-op thread initializes the loader while the primary
thread remains suspended. Its allocation uses RX, then RW while writing, then
RX plus explicit CFG target registration through the SDK import. A cold-module
whitelist rejects QQNT, AVSDK, and audio-client implementation modules before
loading the guard. A timed-out remote thread retains referenced buffers until
the exact owned child is terminated. Named mappings/events are opened with
READ_CONTROL for owner verification. This path remains specific to the tested
QQ build and executable.

In the child, the COM API guard wraps the documented `IMMDeviceEnumerator`,
`IMMDeviceCollection`, `IMMDevice`, and `IMMEndpoint` paths. Default devices for
all three roles resolve to the configured full endpoint ID. `GetDevice` and
collection enumeration preserve source metadata while every wrapped device
redirects `Activate` to the configured active endpoint of the same flow. Device
references are held in the Global Interface Table and wrappers aggregate the
free-threaded marshaler, so callers in other apartments receive a safe wrapper
whose backing endpoint is resolved in the caller's apartment. Property stores
are read-only wrappers. Endpoint notification registration and unregistration
forward the documented `IMMNotificationClient` interface to the backing
enumerator. Callbacks carry opaque endpoint IDs and scalar metadata, not raw
`IMMDevice` interfaces; the caller keeps responsibility for the callback lifetime.
Property-store values pass a data-only whitelist: scalar strings/numbers,
bounded numeric and string vectors, GUIDs, FILETIMEs, and byte blobs up to 1 MiB
are preserved. Windows uses `VT_BLOB` for `WAVEFORMATEX` device format metadata.
Interface-bearing, by-reference, SAFEARRAY, and nested/unsupported
variants are cleared and rejected with `DISP_E_TYPEMISMATCH`.

The private ready status layout is version 3 (320 bytes). It retains exact
PID, creation-time, generation, and readiness binding and adds fixed counters
and HRESULT markers for public COM and legacy audio operations, plus the last
public COM interface IDs. Interface hints are best effort and never admission
or attribution evidence. Entry markers use `E_PENDING`
until an outcome is recorded; they do not indicate a stalled SDK operation.
Counters measure diagnostic observations, so an activation's entry and result
can each contribute one observation. No endpoint IDs, interface addresses,
account identities, PCM, or transcripts are written to this status.

The guard redirects `DirectSoundCreate` and `DirectSoundCreate8` to the concrete
DirectSound GUID read from the configured active render endpoint. Null/default
GUID aliases, missing targets, aggregation, and excessive recursion fail closed.
It denies `ActivateAudioInterfaceAsync`, DirectSound capture/full-duplex, and
waveIn/waveOut entry points. It does not claim
coverage for private/direct syscalls, vendor-private activation paths,
Media Foundation activation paths that bypass `IMMDevice`, undocumented COM
activation entry points, or any AVSDK version other than the pinned build.
The parent traps the documented `CreateProcessW/A`, `CreateProcessAsUserW/A`,
`CreateProcessWithTokenW`, and `CreateProcessWithLogonW` exports. This is not
evidence that an unobserved private process-creation path is covered.
Installation fails closed when any required documented hook or mitigation check
cannot be established. The child guard never changes system defaults, global
preferences, device volume, or mute state.

## Evidence boundary

The native harness uses only fake COM endpoints and fake process-launch
callbacks. It creates no audio client, accesses no physical or virtual endpoint,
starts no QQ/AV Host process, and sends no protocol/network traffic. A passing
harness proves wrapper and barrier behavior under those fakes only. It does not
prove real QQ PCM, ASR, or TTS.

Additional parent-run evidence on 2026-10-05 passed a real, owned QQ.exe
`--type=ppapi` creation barrier with its primary thread still suspended, followed
by native COM virtual capture/render clients for 25 seconds. Only the configured
two virtual endpoints were active; samples were dropped and output was silence.
The actual AV Host PPAPI process was then observed with the guard DLL loaded.
These checks do not establish remote-call PCM, ASR, TTS, or unseen API-path coverage.

The launcher accepts `-AudioRouteGuardConfig <private-json>` and binds this
absolute path inside the strict launch plan. Early startup verifies both artifact
digests and installs the guard before starting the Electron Host. The private
`audio-guard.status.json` distinguishes configured/installed state. Outputs are
`QQCallAudioRouteGuard-v2.node` and `QQCallAudioRouteGuard-v2.dll`; the typed child
bootstrap ABI remains V1. The option defaults off.

Build the distinct parent and child artifacts with
`powershell -NoProfile -ExecutionPolicy Bypass -File native/build_audio_route.ps1`.
The current script first uses the workspace SDK acquired by
`scripts/acquire-guard-sdk.py`; explicit SDK roots remain supported. It writes
objects, import libraries and a hash-checked artifact manifest to its output
directory. Use `scripts/prepare-audio-route.py` to dry-run or create a missing
machine-local source configuration from the canonical public TOML. That step
does not install the guard into a QQ process or authorize a call. The normal
fixed BAT still owns binding.
Run only the isolated mock harness with
`powershell -NoProfile -ExecutionPolicy Bypass -File native/tests/audio_route/build_harness.ps1`.
The verified build used MSVC toolset `14.44.35207` (compiler `19.44.35228`) and
the extracted Windows SDK `10.0.26100.0` headers plus x64 libraries. The include
root is `%TEMP%\windows-sdk-cpp-10.0.26100.8249\c\Include\10.0.26100.0`; the
library root is `%TEMP%\windows-sdk-cpp-x64-10.0.26100.8249\c`. The script
requires the `shared`, `um`, and `ucrt` include directories and the MSVC, `um\x64`,
and `ucrt\x64` library directories, and checks `mmdeviceapi.h` and `Mmdevapi.lib`
before compiling. No SDK package archive is available in the local extraction,
so this record does not claim a verified archive hash.

Both products and the fake harness compile with `/guard:cf`; linking also enables
`/guard:cf`, `/DYNAMICBASE`, `/NXCOMPAT`, and `/HIGHENTROPYVA`. The build scripts
inspect PE load-config data with `dumpbin /loadconfig` and fail unless both
`CF Instrumented` and `Guard CF function table` are present. The harness also
checks the running process CFG policy through `GetProcessMitigationPolicy` and
validates the GFIDS entries for resolved hook targets before a hook transaction
can commit.

The parent module exports `napi_register_module_v1` and exposes the N-API
`install(options)` / `status()` methods. The child DLL exports only the typed
`QQCallAudioRouteGuardChildStartV1` entry point. The mock harness never calls
`install` or installs process hooks.

The API contract references are Microsoft's public documentation:

- [IMMDeviceEnumerator::GetDefaultAudioEndpoint](https://learn.microsoft.com/en-us/windows/win32/api/mmdeviceapi/nf-mmdeviceapi-immdeviceenumerator-getdefaultaudioendpoint)
- [IMMDeviceEnumerator::EnumAudioEndpoints](https://learn.microsoft.com/en-us/windows/desktop/api/Mmdeviceapi/nf-mmdeviceapi-immdeviceenumerator-enumaudioendpoints)
- [IMMDevice::Activate](https://learn.microsoft.com/en-us/windows/win32/api/mmdeviceapi/nf-mmdeviceapi-immdevice-activate)
- [IMMDeviceEnumerator](https://learn.microsoft.com/en-us/windows/win32/api/mmdeviceapi/nn-mmdeviceapi-immdeviceenumerator)
- [COM QueryInterface identity rules](https://learn.microsoft.com/en-us/windows/win32/com/rules-for-implementing-queryinterface)
- [CoCreateFreeThreadedMarshaler](https://learn.microsoft.com/en-us/windows/win32/api/combaseapi/nf-combaseapi-cocreatefreethreadedmarshaler)
- [CreateProcessW](https://learn.microsoft.com/en-us/windows/desktop/api/processthreadsapi/nf-processthreadsapi-createprocessw)
- [GetProcessMitigationPolicy](https://learn.microsoft.com/en-us/windows/win32/api/processthreadsapi/nf-processthreadsapi-getprocessmitigationpolicy)
- [Control Flow Guard](https://learn.microsoft.com/en-us/windows/win32/secbp/control-flow-guard)
- [PROPVARIANT](https://learn.microsoft.com/en-us/windows/win32/api/propidlbase/ns-propidlbase-propvariant)
- [IMMNotificationClient](https://learn.microsoft.com/en-us/windows/win32/api/mmdeviceapi/nn-mmdeviceapi-immnotificationclient)
- [PE CFG metadata](https://learn.microsoft.com/en-us/windows/win32/secbp/pe-metadata)

MinHook v1.3.4 is vendored under `vendor/minhook` with its MIT license. No
upstream source is changed.
