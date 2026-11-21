# Runtime speech language policy

Core owns the effective speech policy. `src.multimodal.speech_policy.SpeechPolicy`
is an immutable snapshot with `backend`, `ready`, `fixed_language`, and
`language_switch_allowed` fields. Synchronous callers can read the router's
`tts_policy` cache. Async callers use `await get_tts_policy()`; the cache is
bounded to two seconds, and command boundaries can request a fresh probe with
`refresh=True`. The adapter-facing snapshot is also available at
`GET /api/multimodal/tts/policy`.

When the active runtime fixes speech to Japanese, Core uses Japanese as the
effective TTS language even if a chat preference, history record, or direct
reply `text_lang` says Chinese. The saved preference remains unchanged, so it
becomes effective again after switching to a language-flexible backend.
`#lang_switch` registration follows the plugin's `enable_lang_switch_command`
setting. Its handler checks fresh runtime policy and preserves preferences and
history while switching is locked. `#help` and `#help_all` show the current
runtime lock in the command description. Regular display replies are not
translated by this policy.

User-facing speech requests that may need language preparation pass
`prepare_text=True` to Core TTS. Core translates only when the runtime declares
a fixed Japanese language, returns the original source text for display, and
fails closed if its runtime policy is unknown or unavailable. Raw low-level
`synthesize_tts` and `synthesize_tts_stream` calls keep the default
`prepare_text=False` behavior. Core's explicit `tts_text` reply materialization,
the WebUI buffered and streaming voice-call paths, and Bilibili idle speech
opt into shared preparation. WebUI's audio cache includes a preparation recipe
version so older unprepared audio cannot satisfy a prepared request.
