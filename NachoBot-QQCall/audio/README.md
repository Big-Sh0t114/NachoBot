# QQCall virtual audio backend

The optional Python component is an installable, QQCall-only PCM transport in
`NachoBot-QQCall/qqcall_pcm`. It captures the verified WASAPI loopback PCM and
plays Core-generated `voice` and `voice_stream` payloads through the matching
verified virtual microphone endpoint. VAD, ASR, speech-result WAV production,
denoising, speaker identification, and TTS stay outside this driver.

## Current public API integration

QQCall creates a session with the frozen UniversalVC public client using
`mode = "pipeline"`, one fixed-identity `external` source named `main`, and
`output = { mode = "none" }`. The PCM driver sends only captured PCM to
UniversalVC. The service owns VAD and ASR and emits speech events with a full
WAV result; QQCall forwards that event as a normal private voice message into
Core, preserving any matching Core ASR receipt and its exact `asr_scope`.
Core-generated audio travels directly from Core to the QQCall PCM output
driver, which reports drain only after its local output callback completes.

The supervisor's private route plan and a current route verification are
required before the binding creates a service session or opens a device. The
captured route must be the verified QQ speaker loopback and playback must be
the separately verified virtual endpoint selected as QQ's microphone. An
unverified route, unsupported capture plan, or invalid token leaves call
controls available and reports audio unavailable. At runtime QQCall first
reuses a healthy UniversalVC service; only a refused HTTP loopback connection
permits it to launch the existing UniversalVC CLI. If the service cannot
become pipeline-ready, status reports audio unavailable. The binding never
opens a default or physical input as fallback. The public API has no
render-loopback input or external playback sink, so this integration uses
neither feature. A successfully launched UniversalVC process remains running
after QQCall stops because other clients may share it.

This integration contract does not establish live group-room capture, remote TTS playback, interruption, late-output suppression, or multi-speaker separation. SDK acknowledgments, synthetic fixtures, and local route checks are not end-to-end call evidence.

Bridge end correlation accepts a terminal signal only when the captured reason is 1, exactly one room-list item matches the current verified 20004 entry, the caller UID and ancillary fields match, and current call/generation checks pass. Audio stopping alone is not end proof.

## Current hangup and reject behavior

The current hangup path sends QRTC cmd 8. A related positive ACK is required before the Host guard reserves and sends cmd 10; neither ACK is terminal evidence, so completion still requires a correlated termination event.

The reject path sends QRTC cmd 9, but its ACK does not prove rejection
completed; a real native reject outcome remains unverified. If a send result is
uncertain or the Host is lost, the current call remains unconfirmed and keeps
its slot. The current cmd 8/10 and cmd 9 distinctions are summarized in the
[root integration README](../README.md#私有通话结束与普通消息).

## Probe process lifetime

The ten-minute default and one-hour maximum apply to bounded probe-mode Broker runs using `--lifetime-ms`. The formal service manager starts Bridge with `--service-mode`; that mode disables the Broker lifetime timer and rejects an explicit `--lifetime-ms`.

## Isolated endpoint routes

Use two distinct virtual routes:

1. Core TTS renders to a virtual playback endpoint whose paired virtual input
   is selected as the QQ microphone.
2. QQ speaker audio renders to a different virtual endpoint. Its WASAPI
   loopback is captured by the QQCall PCM driver and sent to UniversalVC's
   external pipeline source.

The inbound loopback render name and outbound playback name must differ. The
backend resolves exact device names at each start, filters to Windows WASAPI,
requires virtual-device name markers, rejects duplicate or missing matches,
and never selects a Windows default device. PortAudio indexes are process-local
and are not configuration values. A ready Python backend proves only that the
chosen PortAudio endpoints opened; it does not prove QQ selected either route.

## Supervisor-pinned automatic names

The public schema-6 `[qqcall.audio]` table is optional and accepts only the
`capture_device` and `playback_device` name overrides. Either may be omitted or
left empty for automatic resolution; a partial override is valid. The service
does not choose a Windows default endpoint. Before starting Host, it resolves
the two stable IDs already present in the required private native guard config
against active Core Audio metadata, then checks the unique same-name render
endpoint needed by Host playback. It requires distinct routes and the same
virtual-name markers used by `audio/devices.py`. A supplied capture override
may include `(loopback)` or `[loopback]`; after suffix normalization it must
match the guard-pinned render endpoint. A playback override must exactly match
the corresponding guard-pinned name.

Inventory runs on a bounded COM MTA worker and reads only active endpoint IDs,
friendly names, and capture/render flow. It does not open an audio stream,
change an endpoint, or ask for a default device. The service repeats inventory,
native artifact hash, and adapter binding checks immediately before Host
launch and again before publishing the generation-bound private audio plan.
Missing or inactive guard IDs, wrong flow, duplicate names/IDs, physical or
unrecognized endpoints, route replacement, and mismatched overrides all fail
closed; the service never silently retargets after a device change. The
`.node` and `.dll` guard artifacts must match their configured hashes. The
supervisor writes raw endpoint IDs only to the private runtime plan and omits
them from public status and logs.

Real QQ acceptance must separately verify capture, playback, and end-to-end group behavior. Synthetic and local route checks do not establish those outcomes.

List current endpoint names without opening streams by running:

    python .\audio\devices.py

For explicit synthetic routing validation, provide all four exact names. The
outgoing pair is tested by rendering a generated tone and measuring its paired
virtual input; the separate incoming pair is tested by rendering a different
tone and measuring its loopback. The tool measures cross-route leakage and
callback drain/cancel, and writes no PCM or audio files.

    python .\audio\selftest.py --outgoing-render "Line 1 (Virtual Audio Cable)" --outgoing-capture "Line 1 (Virtual Audio Cable)" --incoming-render "Voicemeeter In 5" --incoming-loopback "Voicemeeter In 5 [Loopback]" --max-seconds 3 --report .\audio-selftest-report.json

Check the current enumeration first; the names above are examples and must be
replaced if the local driver labels differ. Do not use physical microphones or
speakers for this test. A successful synthetic report verifies only those
virtual endpoint routes, not AVSDK device selection, QQ transmission, or what a
remote caller heard.

## Backend contract

`NachoBot-QQCall/qqcall_pcm` exports QQCallAudioConfig,
QQCallAudioDeviceScope, and QQCallAudioBackend. The shared
`qqcall_client.qqcall_audio` module is only a compatibility re-export; control
startup does not import the optional package. Audio is disabled by default.
The config table is strict: unknown keys, ambiguous endpoints, physical
endpoints, stale route names, and same-route capture/playback are rejected.
PyAudioWPatch/PyAudio is loaded only when an enabled QQCall PCM backend starts
or enumerates devices. It is a runtime dependency of the QQCall audio extra,
not a dependency of control-only adapter imports.

stream_end() means only that the producer supplied EOF. It returns a local
receipt only after the output callback returned paComplete and the stream
became inactive. Even then, render_drained means only the configured local
PortAudio/WASAPI render queue completed; remote_playback_confirmed remains
false. Cancellation purges pending blocks and aborts by invalidating the
callback generation before closing the stream.

The backend keeps at most the configured queue duration in capture and
playback blocks. Capture overflow fails closed; output underruns are filled
with zeroes and counted. A call-scope fence is checked before and throughout
playback; stale scope aborts the current stream. No audio samples, WAV payloads,
base64 input, or opaque call scope are written to logs or status reports.

The resampler is a stateful PCM16 implementation and has no audioop dependency,
so it supports the Python 3.11 through 3.13+ target without a compatibility
package.

## Windows QQ Host route observer

`audio/windows_routes.py` is a separate, read-only diagnostic. Given a Host PID
and the exact Host/child PID plus creation-time identities already supplied by
the controller, it checks active Core Audio sessions on the Host's expected
capture and render endpoints. It does not discover or add processes, read
process command lines, open a stream, query a default device, alter routing, or
read PCM. Each observation initializes COM in a dedicated MTA worker and
releases COM interfaces, process handles, and COM initialization on that same
thread.

The schema-1 request has these required fields; it may also contain the
optional `loopbackReaders` list described below. `creationTime100ns` is a
decimal string because Windows FILETIME values exceed JavaScript's exact integer range.
The two expected routes are the Host capture endpoint used for its microphone
input and the Host render endpoint used for QQ speaker audio:

```json
{
  "schema": 1,
  "hostGeneration": 23,
  "hostPid": 4100,
  "processes": [
    {"role": "host", "pid": 4100, "creationTime100ns": "134000000000000000"},
    {"role": "child", "pid": 4101, "creationTime100ns": "134000000100000000"}
  ],
  "expectations": [
    {"key": "host_input", "direction": "capture", "endpointName": "Line 1 (Virtual Audio Cable)"},
    {"key": "host_output", "direction": "render", "endpointName": "Voicemeeter In 5 (VB-Audio Voicemeeter VAIO)"}
  ]
}
```

Run one metadata-only observation with private request/report paths:

```powershell
python .\audio\windows_routes.py --request-file .\private-route-request.json --report .\private-route-report.json
```

The report source is `windows_active_sessions`; it contains `hostGeneration`,
`hostPid`, an epoch-millisecond `observedAtMs`, both route records, fixed error
codes, the currently active Host-subtree sessions' PID/generation/start time,
`firstObservedUtc`, and SHA-256 endpoint/session identifiers. Inactive and
expired sessions are omitted; they do not count as proof. Raw endpoint IDs, session IDs,
process paths, account data, and audio samples are omitted. The process start
time is not the audio session's start time; Core Audio exposes no session start
timestamp here, so `firstObservedUtc` is explicitly only when this observer
saw the session.

`verified` is true only when each exact, unique endpoint has an active session
attributed to one of the explicitly supplied Host process instances, their
creation times remain unchanged for the snapshot, and the resolved capture
and render endpoint IDs differ. A shared-process session is recorded as
unverified and cannot prove either route. Same friendly names are resolved
independently within capture and render directions, but the selected endpoint
identities must still differ. Windows' session enumerator can omit sessions
created concurrently; therefore a missing active session reports
`verified: false` and does not prove that the endpoint was not selected. An
active session proves only that Windows has a running stream on that endpoint,
not that samples are nonzero, transmitted, or heard remotely.

If the adapter's separate WASAPI loopback-reader process creates an active
session on the Host's expected render endpoint, its current process identity
may be supplied in the private route config as an optional `loopbackReaders`
array of `{pid, creationTime100ns}` pins. The bridge forwards those exact
identities only to the local observer. A pinned reader is ignored for
render-conflict detection, never counts as a Host route session, and is never
allowed to explain a capture-endpoint session. Its PID and FILETIME must be
refreshed after that reader process restarts; missing or stale pins keep the
route unverified. The observer does not discover reader processes itself.

An active foreign render session adds `endpoint_session_conflict`, because it
can contaminate the render loopback route; its PID and session identifier are
not returned. Foreign capture sessions do not establish Host routing. Active
shared or unattributed sessions remain unverified, while known inactive or
expired background sessions are ignored.

The implementation follows Microsoft's `AudioSessionState` values (inactive
0, active 1, expired 2). `GetState` documents `S_OK` on success. For
`GetProcessId`, `AUDCLNT_S_NO_SINGLE_PROCESS` means the session spans multiple
processes and returns only the initial process ID; the observer never treats
that result as single-process attribution. Session instance identifiers are
released with `CoTaskMemFree` as required by the API.

Microsoft API references: [AudioSessionState](https://learn.microsoft.com/en-us/windows/win32/api/audiosessiontypes/ne-audiosessiontypes-audiosessionstate), [IAudioSessionControl::GetState](https://learn.microsoft.com/en-us/windows/win32/api/audiopolicy/nf-audiopolicy-iaudiosessioncontrol-getstate), [IAudioSessionControl2::GetProcessId](https://learn.microsoft.com/en-us/windows/win32/api/audiopolicy/nf-audiopolicy-iaudiosessioncontrol2-getprocessid), [GetSessionInstanceIdentifier](https://learn.microsoft.com/en-us/windows/win32/api/audiopolicy/nf-audiopolicy-iaudiosessioncontrol2-getsessioninstanceidentifier), and [IAudioSessionManager2::GetSessionEnumerator](https://learn.microsoft.com/en-us/windows/win32/api/audiopolicy/nf-audiopolicy-iaudiosessionmanager2-getsessionenumerator).

The observer uses Core Audio endpoint/session interfaces documented for
Windows 7 and later desktop applications. Its ctypes implementation is intended
for current 64-bit Windows; no live QQ route observation is part of the unit
tests. A real report requires a separately authorized parent/controller run.
