# Shared media download deadlines

`src.common.http_download.stream_with_deadline` manages one cumulative deadline
for opening and consuming a streamed HTTP response. Its required
`total_timeout` must be an exact positive `int` or `float` and finite. Other
keyword arguments pass directly to `client.stream`, so the total budget stays
independent from HTTPX's connect, read, write, and pool inactivity limits.

The deadline starts before `client.stream` is called and covers response
context entry, body reads performed by the caller, and normal response cleanup.
After cleanup, the helper checks the monotonic deadline again so a transport
that suppresses cancellation or synchronous work that overruns cannot return a
late response as a success. The helper closes the response through its context
manager and leaves the client open. Cancellation remains cooperative; a
cleanup method that suppresses cancellation can take longer than the budget,
but the helper will not publish its result afterward.

| Caller | Total budget | Transport timeout behavior |
| --- | --- | --- |
| Video URL download | 60 seconds | Forwards the existing `timeout=60` to HTTPX. |
| Sandbox URL attachment | 60 seconds | Keeps `AsyncClient`'s existing default timeout. `save_upload` runs only after a successful response cleanup and deadline check. |
| Buffered local TTS audio | `LocalMultimodalClient.timeout` | Forwards request kwargs unchanged; total expiry remains a standard `TimeoutError`. |

All three callers continue to use `read_bounded_response` for decoded-byte
limits. Local JSON requests keep their existing timeout, and streaming TTS
keeps its separate 300-second duration policy.
