# Shared browser runtime

URL reads and browser search use the same process-wide `BrowserRuntime` from
`src.common.browser_runtime`. It starts Playwright only when the first browser
request arrives, launches one Chromium browser, and creates an isolated browser
context for each page lease. Each context receives its caller's user agent, so
cookies and storage are not shared between reads.

The runtime allows at most four live page/context leases. Requests wait up to
five seconds for a slot and page creation gets twelve seconds. Navigation and
DOM reads have bounded attempt timeouts in their consumers. A slot remains
reserved through context cleanup; if page creation is canceled or times out,
the runtime owns the late result and closes it before returning that capacity.
If a context cannot be closed, the runtime keeps its slot until browser shutdown
invalidates the context. A confirmed browser disconnect releases leases tied to
that dead browser and wakes queued requests; the next page request reconnects
before creating a replacement context.

Shutdown is terminal and idempotent for the default runtime. It rejects new
leases, wakes queued requests, drains creation and cleanup work for a bounded
period, then closes active contexts, the browser, and Playwright. Calling
shutdown before any browser use does not start Playwright, and still marks the
default owner terminal so later calls cannot start a replacement browser.
`MainSystem.shutdown` closes the browser runtime after chat runtimes have
stopped.

`UrlContentFetcher.browser_available()` and
`UrlContentFetcher.get_shared_browser()` remain compatibility facades. New page
users should acquire pages through `get_shared_browser_runtime().page(...)` so
they share the same cap and lifecycle. A runtime belongs to the event loop that
first uses it; inject a new `BrowserRuntime` when tests or tools need a different
loop. The application's default runtime is intended for its single main loop.

Browser acquisition and navigation failures keep the existing behavior: URL
reads and search can use their HTTP fallback, search can parse a usable DOM
after navigation reports an error, and caller cancellation propagates without
starting HTTP fallback.
