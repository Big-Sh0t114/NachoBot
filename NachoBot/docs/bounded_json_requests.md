# Bounded local JSON requests

`LocalMultimodalClient._request_json_at` handles metadata and operation responses
from the local perception and TTS readiness endpoints, plus the ASR stream
lifecycle. It uses the shared `stream_with_deadline` context to open each request
with HTTPX's async streaming API and read the response inside the stream context.

Successful JSON responses are limited to 1 MiB of decoded bytes. HTTP error
responses are limited to 64 KiB of decoded bytes so the existing error-code
allowlist can be checked without buffering an unbounded body. The shared
`read_bounded_response` reader uses `Content-Length` as an early rejection hint
when it describes decoded bytes, then enforces the cap while consuming decoded
chunks. Content-encoded responses are counted after HTTPX decoding. JSON is
decoded from bytes with the standard library, preserving HTTPX's encoding
detection behavior, and success bodies must be JSON objects.

The existing `LocalPerceptionError` reasons and status mapping remain in place.
Malformed, non-object, unknown, unhashable, or over-limit error payloads fall
back to the HTTP status mapping. Read timeouts and request errors retain their
existing timeout/runtime-unavailable categories. Response contexts close on
success, rejection, timeout, and cancellation; an injected HTTPX client remains
owned by its caller.

Each JSON request uses the shared helper's cooperative `self.timeout` budget
around stream opening, response use, JSON parsing, and normal response cleanup.
After cleanup, the helper checks both the timeout context's expired state and
the monotonic deadline. This rejects a late success when a transport, response
body, or cleanup suppresses cancellation, or when synchronous work overruns
before the event loop can deliver the timeout callback. A rejected deadline is
reported as `LocalPerceptionError("timeout")`. Cancellation remains
cooperative: a resistant transport or cleanup can delay the request until it
returns, and synchronous JSON decoding or HTTP decompression cannot be
preempted. The deadline does not impose a decompressor CPU/memory limit or a
hard interruption guarantee.

The timeout covers one request at a time. In particular, the two-request
health compatibility fallback has a separate budget for each endpoint, not a
single whole-health deadline.

The standalone regression suite loads the actual client, contracts, and bounded
reader source through temporary package aliases. It does not import the
multimodal app package or initialize Core, configuration, logging, or storage.
