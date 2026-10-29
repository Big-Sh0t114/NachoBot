# Prompt cache accounting and diagnostics

NachoBot records prompt cache counts when a provider returns them. OpenAI-compatible
responses use `prompt_tokens_details.cached_tokens` and the compatible
`prompt_cache_hit_tokens` / `prompt_cache_miss_tokens` fields. Gemini responses use
`cached_content_token_count` when the SDK exposes it. An explicit zero is recorded
as reported; missing or invalid cache metadata remains unreported.

## Cache pricing

Model prices are per million tokens. Add `cache_price_in` to a model entry when its
cached prompt tokens have a different price:

```toml
[[models]]
model_identifier = "example-model"
name = "example-model"
api_provider = "ExampleProvider"
price_in = 2.0
price_out = 8.0
cache_price_in = 0.25
```

Omitting `cache_price_in` or setting it to `None` uses `price_in` for cached prompt
tokens. An explicit `cache_price_in = 0.0` makes cache hits free. Values must be
finite and nonnegative. The template shows the field as an optional comment; existing
model configurations continue to use their regular input price until a cache price
is set.

When the provider reports hit tokens, cost is calculated as:

```text
(prompt_tokens - hit_tokens) * price_in + hit_tokens * effective_cache_price
```

The calculation is then divided by one million and combined with output cost. A
missing cache report receives the ordinary input price. When the provider reports a
cache count, the hit count is clamped to the prompt total and the miss count is
normalized to the remaining prompt tokens. A reported miss count without a hit count
can supply the hit count by subtraction when the prompt total is present.

Usage detail rows store hit tokens, miss tokens, and a reported flag. Hourly rows keep
hit and miss totals plus `prompt_cache_reported_calls`, so older calls remain unknown
instead of appearing as zero-hit calls. A hit rate can be calculated from hourly rows
as `hit_tokens / (hit_tokens + miss_tokens)` for buckets with reported calls.

## Optional request diagnostics

Diagnostics are off by default. Set this in `bot_config.toml` to enable them:

```toml
[debug]
enable_llm_cache_stats = true
```

Successful response requests then write:

- `logs/llm_cache_stats/llm_cache_stats.jsonl`
- `logs/llm_cache_stats/llm_cache_stats.html`

The report groups requests by provider, model alias and identifier, request type, and
session. Session identifiers are hashed before grouping. Requests without an explicit
session are marked `callsite`; no user or system identifier is substituted. Replies,
planners, and multimedia response requests use the effective messages supplied to the
provider call. A 413 retry records the compressed messages used for the successful
retry.

The prefix length is an estimate measured in characters from the pre-provider
`Message` representation. Provider SDKs can transform messages, tools, schemas,
multimodal content, and response formats before sending them, and provider cache
matching uses provider-specific token rules. The estimate is not a token count or a
promise of a provider cache hit. Actual hit and miss counts are shown separately and
only when provider usage metadata reported them. The comparison checks the best
matching prompt among the most recent retained entries for that key, reports the
first changed field and character offset, and flags possible sliding content. A
truncated snapshot is explicitly marked partial.

Diagnostics do not write raw prompt text, binary payloads, API keys, endpoints, or
provider configuration. Binary parts are hashed. The HTML output escapes displayed
values. Collection is bounded to 16,384 text characters, 256 fields, and 2 MiB of
binary hashing per prompt. Retained history is limited to 128 entries per key and
64 keys, with an 8 MiB estimated retained-payload budget based on Python object
sizes. The entry and key limits bound runtime overhead outside that estimate. The
writer queue holds at most 128 reports. JSONL output is rotated at 2 MiB with one
backup, and the HTML report stays within 2 MiB.

Cache diagnostics are observational: they do not change prompt construction, model
selection, or the existing context window behavior.

## Related cache improvements

The graph cache tracks revisions and batches related work. Media conversion uses
content and option keys, with limits of 512 entries, 32 MiB, and two workers.
Periodic emoji/media cleanup uses `Images.last_used_time` and
`Images.no_file_flag`; it expires images after 14 days, emoji after 30 days,
descriptions after 30 days, and videos after 14 days, processing up to 1,000 items
per existing periodic check.

For isolated verification, run `.venv/Scripts/python.exe
scripts/run_cache_checks.py` from the `NachoBot` directory. The runner executes the
required cache suites from a temporary shadow copy, so package initialization cannot
migrate the working database or rewrite local configuration. These checks cover code
behavior; they do not exercise live providers or services.

## Image conversion working-set bounds

The encoded conversion cache is limited to 32 MiB, but that limit does not constrain
temporary decoded Pillow images. Image conversion now also applies working-set limits:

- Source images above 16 million pixels are rejected before frame conversion or
  compositing. This covers 3840x2160 imagery while preventing very large declared
  dimensions from reaching a full RGBA allocation.
- GIF description sheets decode frames in order, scan no more than 256, retain at
  most 15 already-resized selected frames, and cap the output sheet at 8192x200.
  Similarity comparison keeps the legacy uint8 arithmetic and uses 256 KiB scratch
  chunks; a source-size guard bounds the two full-resolution RGB comparison arrays
  to about 96 MB (92 MiB) combined at the 16-million-pixel limit.
- Animated message compression plans over at most 256 frames and 64 million
  cumulative source pixels, retaining up to 32 evenly sampled frames. Rendering
  replays only that scanned prefix and checks the same cumulative pixel budget again.
  The two passes therefore touch at most 128 million source pixels combined. Delays
  from skipped frames are added to the next retained frame. The encoded output is
  limited to 16 million total pixels and a 2048-pixel longest edge. Animations longer
  than the bounded scan become a representation of the scanned prefix; the tail is
  omitted. If frame dimensions change between the plan and render passes, the original
  message image is kept.
- Collage preprocessing rejects sources above the same pixel limit, scans at most 256
  frames, and thumbnails the chosen frame to a 1600-pixel longest edge before RGBA
  compositing. For animations longer than 256 frames, the chosen middle frame comes
  from that scanned prefix. Oversized or unreadable candidates are skipped.

Conversion option keys use version 2 for these bounds, so prior encoded results do
not bypass the new processing behavior.

An isolated 180-frame 128x128 GIF reproduced the old frame-retention peak: 181 live
frame copies and 8,896,512 estimated pixel bytes before selection. With streaming,
the same GIF description path peaked at 17 tracked Pillow allocations / 3,616,384
estimated pixel bytes, including the selected frames and output sheet. Message
compression peaked at 76 tracked Pillow allocations / 278,528 estimated pixel bytes;
that object count includes short-lived decoder intermediates, while its output holds
no more than 32 frames. These figures come from live Pillow-object instrumentation,
not a process RSS measurement. They show the conversion working set is bounded for
the tested path; they do not establish whether any separate process has a leak.

The memory regression is included in the default shadow-runner suite list. Its
verification command remains `.venv/Scripts/python.exe scripts/run_cache_checks.py`.

## Tool and web-search result cache lifetime

Tool execution and web-search results use one process-wide bounded cache with a
separate namespace for each cache instance and chat. Entries expire after five
minutes of monotonic time even when they are never requested again. Existing
`cache_ttl` settings still count cache reuses; age expiry is an additional limit.

Each owner retains at most 64 entries and 2 MiB of estimated key, metadata, and
payload size. The process-wide limit is 2,048 entries and 32 MiB. Least recently
used entries are evicted when either budget is reached. Expired and exhausted
entries are pruned on later cache reads or writes, without a background cleanup
task.

Cache values are copied on insertion and lookup. Only built-in JSON-like values,
tuples, bytes, and scalar types are eligible; cyclic, deeply nested, unsupported,
non-finite, or oversized results are returned normally by the tool or search path
but are skipped by the cache. This keeps caller mutation and unusually large
provider responses from expanding or corrupting retained cache data.

## Default prompt prefix ordering

The built-in main LLM prompts now place stable identity, task instructions, output
rules, and stable reply style before the per-session label and chronological chat
history. Focus handoff content and turn-specific recall, memory, tool, action,
appointment, target, moderation, and time fields follow that history. Integrated
reply planning keeps its conditional plan style with the turn-specific fields.
Rewrite prompts keep the session label and history together, then place mood,
expression and keyword context, the target and original reply, moderation, and time
after the history. Tool and MCP executors and the web-search decider put their fixed
role and rules first, then history, then the current task and time. The capability
router keeps the existing JSON values and catalog filtering while serializing keys
in this order: `bot_name`, `mcp_catalog`, `sandbox_edit_available`, `chat_history`,
`current_request`, `time_now`.

This ordering applies to the registered built-in templates and router envelope.
Custom prompts and plugin overrides retain their supplied layout. Template
registration names, formatting fields and multiplicity, instructions, output
contracts, routing behavior, and history contents are preserved.

Using real rendered templates with fixed identity, style, session, and history and
varying turn-specific fields, the longest common prefix increased by these measured
character counts:

| Prompt | Before | After |
| --- | ---: | ---: |
| Reply | 23 | 402 |
| Reply self follow-up | 23 | 293 |
| Private reply | 23 | 264 |
| Private self follow-up | 23 | 230 |
| File edit | 23 | 319 |
| Integrated reply planner | 276 | 850 |
| Rewrite | 246 | 280 |
| Action planner | 32 | 1,192 |
| Brain planner | 32 | 1,405 |
| Tool executor | 46 | 375 |
| MCP executor | 53 | 330 |
| Web-search decider | 93 | 548 |

All 18 registered prompt targets retained their original placeholder counts in the
byte-exact source comparison. The isolated tests render the actual templates through
the `Prompt` formatter, check a fixed prefix through the history, append a history
line, exercise A/B/A session renders, preserve actorless event and moderation markers,
and capture the actual router JSON sent to a mock decider. These prefix lengths are
characters in the rendered prompt, not provider token counts or a guarantee of a
provider cache hit.

The six import-heavy Focus, private system-event, offline recovery, planner snapshot,
MCP executor, and reply-tool prose suites were also attempted in the shadow runner
with the optional public-template config seed. They stop during config initialization
because the public model template has an empty API key rejected by `APIProvider`;
the child runs zero tests and the runner reports failure rather than a false pass.
No live config, credentials, database, logs, provider, or service were used. The new
template-order suite is included in the default cache-check module list.

## Explicit user-message boundaries and cache-key isolation

Annotated built-in prompts may retain up to three source-defined text chunks:
stable prefix, history-bearing middle, and turn-specific suffix. Each non-empty
chunk is sent as a separate `user` message. Their concatenated text is byte-for-byte
the legacy flattened prompt, and caller-supplied system instructions remain in the
system role. Plain strings and custom prompts without the explicit annotation still
produce one user message. Untrusted rendered values are never scanned for boundary
markers.

The supplied cloud trace showed a 2,794-character stable action catalog after
dynamic chat, with the whole prompt still sent as one user message. That trace does
not show that the catalog was a reusable prefix. An ordinary HTTP proxy that groups
requests from the initial system message and the complete first user message still
sees all content in that user message as one cache group. OpenAI documents implicit
lookups at message endings for GPT-5.6 and newer, subject to a 1,024 visible-token
minimum. Those boundaries can provide lookup points, but implicit writes continue
through the last eligible message. A changing later message does not make the earlier
stable prefix an explicit reusable write. This implementation can mark the first
stable user chunk with `prompt_cache_breakpoint: {"mode": "explicit"}` when the
provider's separate `enable_prompt_cache_breakpoints` flag is enabled. The marker is
attached to that text part in both requests so subsequent dynamic history does not
extend the write. The standard Chat Completions message schema documents this field
for text content parts. [OpenAI prompt caching guide](https://developers.openai.com/api/docs/guides/prompt-caching),
[Chat Completions create reference](https://developers.openai.com/api/reference/resources/chat/subresources/completions/methods/create)

The explicit breakpoint flag is off by default and independent of the session-key
flag. Ordinary message boundaries alone do not write a shorter stable prefix. A
proxy still needs to forward or correctly translate the documented marker; this
implementation does not change or probe any deployed proxy. No padding is added.

The commented provider flags are `enable_session_cache_key = true` and
`enable_prompt_cache_breakpoints = true`; both default to false. The session key is
generated only for response calls carrying an explicit session ID. Its 64-character
SHA-256 digest uses the provider name, client type, session ID, and task label. It does
not use a prompt/template version, prompt contents, model selection, time, randomness,
or API keys, and it does not send raw session IDs. Requests for the same provider,
session, and task reuse
the key, while different providers and tasks get different keys. An existing
`prompt_cache_key` in merged request parameters wins, including `None` or an empty
string. This key scopes the provider's cache group; it does not force routing or prove
that the provider served cached tokens. Embedding and audio requests do not receive a
generated key, and Gemini ignores this metadata.

The cloud/provider cache-read figures observed before and after prompt changes were
3.302% and 1.743%. They came from incomparable request mixes, so they establish no
measured improvement. Continue to use provider-reported cache usage for hit evidence;
mocked wire tests verify message boundaries, explicit marker serialization, and key
stability. They do not verify a live provider cache hit.
