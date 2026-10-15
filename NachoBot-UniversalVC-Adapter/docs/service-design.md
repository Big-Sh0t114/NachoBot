# UniversalVC loopback service

Status: implemented as an optional, standalone UniversalVC service. This delivery changes UniversalVC-owned packaging, namespaced audio components, the API, client, tests, and documentation. Existing adapter entrypoints and the other adapters keep their current runtime integrations.

## Runtime boundary

```text
external caller ── lightweight HTTP/WebSocket client ── UniversalVC loopback service
                                                          ├─ per-session/source VAD and pipeline state
                                                          ├─ explicitly selected process or microphone capture
                                                          ├─ optional denoise and speaker identification
                                                          ├─ Core streaming-ASR bridge plus WAV fallback
                                                          └─ optional local output device
                                                                  │
                                                                  ▼
                                                              NachoBot Core
```

The service owns session isolation, audio processing, capture/output device lifecycle, and scoped Core ASR receipts. Core continues to own ASR model selection and fallback, conversation routing, and TTS. The service does not contain platform router, prompt, or chat-message policy. The legacy `main.py`/`adapter.py` path remains available and does not require the service.

The public client lives at `universalvc_service.client`. Importing it uses only Python's standard library and `aiohttp`; callers do not import NumPy, Sherpa, ProcTap, or audio-device modules. A caller can put the UniversalVC adapter directory on `PYTHONPATH` and install only `aiohttp`, or deploy `universalvc_service/client.py` with its lightweight package initializer. Do not install the adapter's native audio/model dependencies into an external adapter just to use the client.

## Start the service

Create a service-owned config and set a private token:

```powershell
cd NachoBot-UniversalVC-Adapter
if (-not (Test-Path service.toml)) { Copy-Item service.example.toml service.toml }
# Edit service.toml and replace server.token; keep an existing file intact.
.venv\Scripts\python.exe service_main.py --config service.toml
```

The Windows helper `launch_universal_vc_service.bat` runs that entrypoint from the existing UniversalVC virtual environment. It does not start the legacy adapter or install packages. `python service_main.py --help` lists the CLI option without loading the audio engine. `NACHOBOT_UNIVERSALVC_TOKEN` overrides the TOML token. The service refuses to start if the token is missing, and accepts only a literal loopback bind address (`127.0.0.1` or `::1`).

`service.toml` is ignored by Git. It owns service bind/auth settings, Core connection, server-side model paths, speaker database root, and bounded resource defaults. It never supplies a caller's session capture, output, denoise, speaker, or speaker-log choices. The live adapter `config.toml` remains for the legacy path and is not read by the service. The service does not download models; requested capabilities fail at session creation if their configured model/runtime is unavailable.

## Session request

Every session sends its complete audio choices in `create_session(options)`. The service defaults `denoise`, `speaker`, and `speaker_log` to `false`; omitted flags never inherit the legacy adapter config. Source IDs are unique within a session. Process capture requires exactly one `target_pid`, `target_process_name`, or `system_capture_device`. A `system_capture_device` selects a sounddevice capture/input endpoint; it does not mean WASAPI render loopback. Microphone capture selects `capture.device_name` and can opt into push-to-talk. External PCM input opens no capture device. Output is either `none` or an explicitly named local device.

This example asks the service to capture a named application and a separate opted-in microphone, play replies to a selected device, and enable per-session denoise, speaker identification, and speaker-result logging:

```python
options = {
    "platform": "my_adapter",
    "scope": "call:example-42",
    "mode": "pipeline",
    "sources": [
        {
            "id": "app-audio",
            "input": "process",
            "format": "f32le",
            "sample_rate": 48000,
            "channels": 2,
            "capture": {"target_process_name": "chat.exe"},
            "identity": {"mode": "speaker"},
        },
        {
            "id": "my-microphone",
            "input": "microphone",
            "format": "f32le",
            "sample_rate": 48000,
            "channels": 1,
            "capture": {
                "device_name": "Microphone (USB Audio)",
                "push_to_talk": True,
                "ptt_key": "caps_lock",
            },
            "identity": {"mode": "fixed", "id": "owner", "name": "Local user"},
        },
    ],
    "output": {
        "mode": "device",
        "device_name": "Virtual Cable Input",
        "sample_rate": 48000,
    },
    "denoise": True,
    "speaker": True,
    "speaker_log": True,
    "max_duration_seconds": 30,
}
```

Set `speaker_log` false to suppress speaker identity and recognized-speaker result logs. These logs contain session/source-correlated speaker metadata and segment duration, not raw audio or transcripts. Fixed identities bypass speaker inference. `vad` mode is available for one externally fed `s16le` 16 kHz mono source; its caller receives completed PCM and can request `encode_wav` without using the service's capture pipeline.

Client usage:

```python
from universalvc_service.client import UniversalVCClient

client = UniversalVCClient()  # URL/token: NACHOBOT_UNIVERSALVC_URL/TOKEN
try:
    session = await client.create_session(options)
    try:
        async for event in session.events():
            if event["event"] == "speech_result":
                print(event["source_id"], event["speaker_name"])
    finally:
        await session.close()
finally:
    await client.close()
```

The public asynchronous methods are `UniversalVCClient.create_session(options)` and `UniversalVCClient.close()`, plus `UniversalVCSession.send_pcm(pcm, source_id="main")`, `feed_vad(pcm, source_id="main")`, `encode_wav(pcm, max_duration_seconds=60)`, `play_audio(segment)`, `interrupt()`, `events()`, and `close()`. `feed_vad` returns `{"speaking": bool, "completed_pcm": bytes, "completed_segments": tuple[bytes, ...]}`; when one input frame completes several utterances, the tuple preserves their separate boundaries and `completed_pcm` is their concatenation. `encode_wav` returns base64-encoded WAV text. `send_pcm` returns `{"accepted": True}`. Playback returns a status of `queued`, `render_drained`, or `aborted`.

Pipeline `speech_result` events contain `platform`, `speaker_id`, `speaker_name`, `voice_base64`, `precomputed_asr_result_id`, `precomputed_asr_text`, `asr_scope`, and `turn_id`. Reuse a non-empty `precomputed_asr_result_id` only as the Core-issued streaming-ASR receipt it represents. If no streaming receipt is present, submit `voice_base64` through Core's ordinary voice/ASR path using the session platform and event `asr_scope`, preserving Core's model selection and fallback behavior. The service does not manufacture a receipt for that path. The opaque `asr_scope` binds platform, session generation, source, and utterance; do not replace it with a global or cross-session scope.

The request has bounded defaults: at most 8 sessions, 4 sources per session, 32 total sources, 32 queued capture frames per source, and 60 seconds per utterance. Capture/output selections are reserved across sessions; a conflicting selection is rejected. A WebSocket disconnect deletes its session. A live client sends a heartbeat every 10 seconds, and an idle session is deleted after 30 seconds. Reconnect creates a new session generation; it does not resume prior audio.

## API and wire contract

All routes require `Authorization: Bearer <token>`. The client rejects non-loopback URLs, redirects, and proxy use.

| Route | Purpose |
| --- | --- |
| `GET /v1/health` | Authenticated protocol version, service instance, readiness and capability status |
| `POST /v1/sessions` | Validate explicit options and open a new isolated session |
| `WS /v1/sessions/{id}/stream` | Bind the one current WebSocket, send requests/audio, receive replies and source events |
| `POST /v1/sessions/{id}/interrupt` | Interrupt audio and invalidate the session's current pipeline turn/output |
| `DELETE /v1/sessions/{id}` | Cancel operations and close capture/output/Core resources |

JSON request/reply/event messages carry `protocol_version`, `service_instance_id`, `session_id`, and `session_generation`. Audio frames are `4-byte big-endian JSON-header length + UTF-8 JSON header + PCM`; headers are limited to 8192 bytes, PCM to 65536 bytes per frame, and each source's sequence must advance monotonically. The server bounds outstanding operations and combined pending/active PCM, rejects duplicate IDs and stale session identities, and disconnects on malformed or discontinuous audio. WebSocket event messages are limited to 4 MiB.

Pipeline events are `speech_start`, `speech_end`, `speech_result`, and `error`. They include `source_id` and correlated `turn_id`; results include the fixed or inferred identity, WAV payload, and any Core ASR receipt/text. Receipt scope is generated server-side from platform, session generation, source, and utterance. A Core stream unsupported for a platform or unavailable at runtime falls back to the complete WAV path; the service does not change Core's provider selection policy.

Playback accepts Core `voice` and `voice_stream` segments only when output mode is `device`. It reports `queued`, `render_drained`, or `aborted`; a queue acceptance is not described as device drain. Output mode `none` rejects playback. There is no caller-provided external sink mode in this API.

## Verification boundary

The service has real loopback HTTP/WebSocket tests with injected fake VAD/Core/output components. They cover authentication, independent sessions, generation fencing, ordered PCM, disconnect and lease cleanup, VAD/WAV conversion, voice streams, bounded requests, and cancellation/interruption races without opening a physical device or downloading a model. The legacy UniversalVC tests remain part of the regression suite. These checks do not claim live audio-device or real platform-call acceptance.
