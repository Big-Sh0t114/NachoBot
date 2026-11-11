"""Shared, loop-scoped Playwright ownership for browser page consumers."""

from __future__ import annotations

import asyncio
import importlib.util
import logging
import weakref
from contextlib import asynccontextmanager
from typing import Any, AsyncIterator, Callable

logger = logging.getLogger(__name__)

_MAX_PAGES = 4
_QUEUE_WAIT_SECONDS = 5.0
_CREATE_WAIT_SECONDS = 12.0
_CLOSE_WAIT_SECONDS = 2.0
_SHUTDOWN_DRAIN_SECONDS = 3.0


class BrowserRuntimeError(RuntimeError):
    """Base class for browser runtime lifecycle errors."""


class BrowserRuntimeClosed(BrowserRuntimeError):
    """Raised when a page is requested after shutdown has started."""


class BrowserRuntimeUnavailable(BrowserRuntimeError):
    """Raised when Playwright cannot be imported or started."""


class BrowserRuntimeLoopError(BrowserRuntimeError):
    """Raised when a live runtime is used from a different event loop."""


def browser_available() -> bool:
    """Return whether Playwright is importable without importing it eagerly."""
    try:
        return importlib.util.find_spec("playwright.async_api") is not None
    except (ImportError, ModuleNotFoundError, ValueError):
        return False


class _PageLease:
    def __init__(
        self,
        runtime: BrowserRuntime,
        browser: Any = None,
        context: Any = None,
        page: Any = None,
    ) -> None:
        self.runtime = runtime
        self.browser = browser
        self.context = context
        self.page = page
        self._slot_transferred = False
        self._slot_released = False
        self._close_operation: asyncio.Task[None] | None = None
        self._finalizer: asyncio.Task[None] | None = None

    async def close(self) -> None:
        if self._close_operation is None:
            self._close_operation = asyncio.create_task(self.runtime._close_resources(self))
            self.runtime._track_background(self._close_operation)

        try:
            await asyncio.wait_for(asyncio.shield(self._close_operation), timeout=self.runtime.close_timeout)
        except asyncio.TimeoutError:
            if self._close_operation.done():
                if not self.runtime._browser_is_connected(self.browser):
                    self.runtime._release_lease(self)
                raise
            logger.warning("Timed out closing a browser page context; retaining its lease until cleanup completes")
            self._schedule_finalizer()
        except asyncio.CancelledError:
            self._schedule_finalizer()
            raise
        except BaseException:
            if not self.runtime._browser_is_connected(self.browser):
                self.runtime._release_lease(self)
            raise
        else:
            self.runtime._release_lease(self)

    def _schedule_finalizer(self) -> None:
        if self._finalizer is not None:
            return

        async def finalize_after_close() -> None:
            try:
                await self._close_operation
            except BaseException:
                if not self.runtime._browser_is_connected(self.browser):
                    self.runtime._release_lease(self)
                return
            self.runtime._release_lease(self)

        self._finalizer = asyncio.create_task(finalize_after_close())
        self.runtime._track_background(self._finalizer)


class BrowserRuntime:
    """Own one Playwright browser and a bounded set of isolated page contexts.

    A runtime is bound to the first event loop that uses it. The process-wide
    default is intended for the application's single loop; tests and other
    multi-loop callers should inject a separate runtime per loop.
    """

    def __init__(
        self,
        *,
        max_pages: int = _MAX_PAGES,
        queue_timeout: float = _QUEUE_WAIT_SECONDS,
        create_timeout: float = _CREATE_WAIT_SECONDS,
        close_timeout: float = _CLOSE_WAIT_SECONDS,
        shutdown_drain_timeout: float = _SHUTDOWN_DRAIN_SECONDS,
        playwright_factory: Callable[[], Any] | None = None,
    ) -> None:
        if max_pages < 1:
            raise ValueError("max_pages must be at least 1")
        self.max_pages = max_pages
        self.queue_timeout = queue_timeout
        self.create_timeout = create_timeout
        self.close_timeout = close_timeout
        self.shutdown_drain_timeout = shutdown_drain_timeout
        self._playwright_factory = playwright_factory

        self._loop: asyncio.AbstractEventLoop | None = None
        self._slots: asyncio.Semaphore | None = None
        self._shutdown_event: asyncio.Event | None = None
        self._browser: Any = None
        self._playwright: Any = None
        self._browser_init_task: asyncio.Task[Any] | None = None
        self._creating: set[asyncio.Task[Any]] = set()
        self._background: set[asyncio.Task[Any]] = set()
        self._active_leases: set[_PageLease] = set()
        self._closing = False
        self._closed = False
        self._closed_browsers: weakref.WeakSet[Any] = weakref.WeakSet()
        self._stopped_playwrights: weakref.WeakSet[Any] = weakref.WeakSet()
        self._closed_browser_fallback: list[Any] = []
        self._stopped_playwright_fallback: list[Any] = []
        self._shutdown_task: asyncio.Task[None] | None = None

    @staticmethod
    def browser_available() -> bool:
        return browser_available()

    @property
    def is_closed(self) -> bool:
        return self._closed

    def _bind_loop(self) -> None:
        loop = asyncio.get_running_loop()
        if self._loop is None:
            self._loop = loop
            self._slots = asyncio.Semaphore(self.max_pages)
            self._shutdown_event = asyncio.Event()
        elif self._loop is not loop:
            raise BrowserRuntimeLoopError(
                "BrowserRuntime belongs to a different event loop; shut it down on its owning loop "
                "or inject a runtime for this loop"
            )

    def _ensure_open(self) -> None:
        if self._closing or self._closed:
            raise BrowserRuntimeClosed("Browser runtime is shutting down")

    def _release_disconnected_browser_leases(self, browser: Any, *, confirmed: bool = False) -> None:
        if not confirmed and self._browser_is_connected(browser):
            return
        for lease in tuple(self._active_leases):
            if lease.browser is browser:
                self._release_lease(lease)

    def _reclaim_disconnected_leases(self) -> None:
        for lease in tuple(self._active_leases):
            self._release_disconnected_browser_leases(lease.browser)

    def _watch_browser_disconnect(self, browser: Any) -> None:
        register = getattr(browser, "on", None)
        if not callable(register):
            return
        try:
            register(
                "disconnected",
                lambda *_args: self._release_disconnected_browser_leases(browser, confirmed=True),
            )
        except Exception:
            logger.debug("Could not subscribe to the browser disconnect event", exc_info=True)

    def _track_background(self, task: asyncio.Task[Any]) -> None:
        self._background.add(task)

        def finished(done_task: asyncio.Task[Any]) -> None:
            self._background.discard(done_task)
            try:
                done_task.exception()
            except BaseException:
                pass

        task.add_done_callback(finished)

    async def _acquire_slot(self) -> None:
        self._bind_loop()
        self._ensure_open()
        assert self._slots is not None
        assert self._shutdown_event is not None

        acquire_task = asyncio.create_task(self._slots.acquire())
        shutdown_task = asyncio.create_task(self._shutdown_event.wait())
        acquired = False
        transferred = False
        try:
            done, _ = await asyncio.wait(
                (acquire_task, shutdown_task),
                timeout=self.queue_timeout,
                return_when=asyncio.FIRST_COMPLETED,
            )
            if acquire_task in done and acquire_task.result():
                acquired = True
                if self._closing or self._closed:
                    raise BrowserRuntimeClosed("Browser runtime is shutting down")
                if not shutdown_task.done():
                    shutdown_task.cancel()
                await asyncio.gather(acquire_task, shutdown_task, return_exceptions=True)
                if self._closing or self._closed:
                    raise BrowserRuntimeClosed("Browser runtime is shutting down")
                transferred = True
                return
            if shutdown_task in done or self._closing or self._closed:
                raise BrowserRuntimeClosed("Browser runtime is shutting down")
            raise asyncio.TimeoutError("Timed out waiting for a browser page slot")
        finally:
            if not transferred:
                for task in (acquire_task, shutdown_task):
                    if not task.done():
                        task.cancel()
                try:
                    await asyncio.gather(acquire_task, shutdown_task, return_exceptions=True)
                finally:
                    if not acquired and acquire_task.done() and not acquire_task.cancelled():
                        try:
                            acquired = bool(acquire_task.result())
                        except BaseException:
                            pass
                    if acquired:
                        self._slots.release()

    @asynccontextmanager
    async def page(self, user_agent: str | None = None) -> AsyncIterator[Any]:
        """Acquire a page in a fresh context, within the shared page cap."""
        self._bind_loop()
        self._ensure_open()
        self._reclaim_disconnected_leases()
        await self._acquire_slot()
        slot_owned = True
        lease = _PageLease(self)
        create_task = asyncio.create_task(self._create_lease(user_agent, lease))
        self._creating.add(create_task)
        create_task.add_done_callback(self._creating.discard)

        try:
            try:
                lease = await asyncio.wait_for(asyncio.shield(create_task), timeout=self.create_timeout)
            except asyncio.TimeoutError:
                self._adopt_late_creation(create_task, lease)
                slot_owned = False
                raise
            except asyncio.CancelledError:
                self._adopt_late_creation(create_task, lease)
                slot_owned = False
                raise
            except BaseException:
                if lease._slot_transferred:
                    slot_owned = False
                raise

            slot_owned = False
            if self._closing or self._closed:
                await lease.close()
                raise BrowserRuntimeClosed("Browser runtime is shutting down")

            try:
                yield lease.page
            except BaseException:
                try:
                    await lease.close()
                except BaseException:
                    logger.debug("Browser page cleanup failed while preserving the request exception", exc_info=True)
                raise
            else:
                await lease.close()
        finally:
            if slot_owned:
                self._release_unleased_slot()

    def _release_unleased_slot(self) -> None:
        if self._slots is not None:
            self._slots.release()

    def _adopt_late_creation(self, create_task: asyncio.Task[_PageLease], lease: _PageLease) -> None:
        async def clean_late_result() -> None:
            try:
                await create_task
            except BaseException:
                if not lease._slot_transferred:
                    self._release_unleased_slot()
                return
            await lease.close()

        watcher = asyncio.create_task(clean_late_result())
        self._track_background(watcher)

    async def _create_lease(self, user_agent: str | None, lease: _PageLease) -> _PageLease:
        browser = await self._get_browser()
        self._ensure_open()
        context_options = {"user_agent": user_agent} if user_agent else {}
        context = await browser.new_context(**context_options)
        lease.browser = browser
        lease.context = context
        lease._slot_transferred = True
        self._active_leases.add(lease)
        try:
            if self._closing or self._closed:
                raise BrowserRuntimeClosed("Browser runtime is shutting down")
            lease.page = await context.new_page()
        except BaseException:
            try:
                await lease.close()
            except BaseException:
                logger.debug("Failed to close a partially created browser context", exc_info=True)
            raise

        browser_disconnected = not self._browser_is_connected(browser)
        if self._closing or self._closed or browser_disconnected:
            try:
                # Shutdown or browser disconnect may already have closed the
                # partial lease while new_page() was pending.
                await lease.page.close()
                lease.page = None
            except BaseException:
                logger.debug("Failed to close a late browser page", exc_info=True)
            try:
                await lease.close()
            except BaseException:
                logger.debug("Failed to close a late browser context", exc_info=True)
            if self._closing or self._closed:
                raise BrowserRuntimeClosed("Browser runtime is shutting down")
            raise BrowserRuntimeError("Browser disconnected during page creation")
        return lease

    async def _get_browser(self) -> Any:
        self._bind_loop()
        self._ensure_open()
        task = self._browser_init_task
        if task is None:
            task = asyncio.create_task(self._initialize_browser())
            self._browser_init_task = task

            def clear_init(done_task: asyncio.Task[Any]) -> None:
                if self._browser_init_task is done_task:
                    self._browser_init_task = None
                try:
                    done_task.exception()
                except BaseException:
                    pass

            task.add_done_callback(clear_init)
        return await asyncio.shield(task)

    async def _initialize_browser(self) -> Any:
        self._ensure_open()
        if self._browser is not None:
            if self._browser_is_connected(self._browser):
                return self._browser
            stale_browser = self._browser
            self._browser = None
            await self._close_browser_once(stale_browser)
            self._release_disconnected_browser_leases(stale_browser)

        playwright = self._playwright
        if playwright is None:
            factory = self._playwright_factory
            if factory is None:
                if not browser_available():
                    raise BrowserRuntimeUnavailable("Playwright is not installed")
                try:
                    from playwright.async_api import async_playwright
                except Exception as exc:
                    raise BrowserRuntimeUnavailable("Playwright is unavailable") from exc
                factory = async_playwright
            playwright = await factory().start()
            if self._closing or self._closed:
                await self._stop_playwright_once(playwright)
                raise BrowserRuntimeClosed("Browser runtime is shutting down")
            self._playwright = playwright

        try:
            browser = await playwright.chromium.launch(headless=True)
        except BaseException:
            if self._playwright is playwright:
                self._playwright = None
            await self._stop_playwright_once(playwright)
            raise

        if self._closing or self._closed:
            await self._close_browser_once(browser)
            await self._stop_playwright_once(playwright)
            raise BrowserRuntimeClosed("Browser runtime is shutting down")

        self._browser = browser
        self._watch_browser_disconnect(browser)
        self._release_disconnected_browser_leases(browser)
        logger.info("Created shared Playwright browser")
        return browser

    async def get_browser(self) -> Any:
        """Compatibility accessor for callers that need the shared browser."""
        self._bind_loop()
        self._ensure_open()
        task = asyncio.create_task(self._get_browser())
        try:
            return await asyncio.wait_for(asyncio.shield(task), timeout=self.create_timeout)
        except asyncio.TimeoutError:
            self._track_background(task)
            raise
        except asyncio.CancelledError:
            self._track_background(task)
            raise

    async def _close_resources(self, lease: _PageLease) -> None:
        async def close_one(resource: Any) -> BaseException | None:
            try:
                await resource.close()
            except Exception as exc:
                return exc
            return None

        close_operations = []
        if lease.page is not None:
            close_operations.append(close_one(lease.page))
        if lease.context is not None:
            close_operations.append(close_one(lease.context))
        if not close_operations:
            return
        errors = await asyncio.gather(*close_operations)
        page_error = errors[0] if lease.page is not None else None
        context_error = errors[-1] if lease.context is not None else None
        if page_error is not None:
            logger.debug("Failed to close a browser page; its context close was still attempted", exc_info=page_error)
        if context_error is not None:
            # Keep the slot until shutdown closes the whole browser. A failed
            # context close may otherwise leave a live context outside the cap.
            raise BrowserRuntimeError("Could not close browser page context") from context_error

    @staticmethod
    def _browser_is_connected(browser: Any) -> bool:
        if browser is None:
            return False
        try:
            return bool(browser.is_connected())
        except Exception:
            logger.debug("Could not confirm browser disconnection; retaining its leases", exc_info=True)
            return True

    def _release_lease(self, lease: _PageLease) -> None:
        if lease._slot_released:
            return
        lease._slot_released = True
        self._active_leases.discard(lease)
        self._release_unleased_slot()

    async def _close_browser_once(self, browser: Any) -> None:
        if browser is None:
            return
        try:
            if browser in self._closed_browsers:
                return
            self._closed_browsers.add(browser)
        except TypeError:
            if any(previous is browser for previous in self._closed_browser_fallback):
                return
            self._closed_browser_fallback.append(browser)

        async def close_browser() -> None:
            try:
                await browser.close()
            except Exception:
                logger.debug("Failed to close shared Playwright browser", exc_info=True)

        task = asyncio.create_task(close_browser())
        try:
            await asyncio.wait_for(asyncio.shield(task), timeout=self.close_timeout)
        except asyncio.TimeoutError:
            self._track_background(task)
        except asyncio.CancelledError:
            self._track_background(task)
            raise

    async def _stop_playwright_once(self, playwright: Any) -> None:
        if playwright is None:
            return
        try:
            if playwright in self._stopped_playwrights:
                return
            self._stopped_playwrights.add(playwright)
        except TypeError:
            if any(previous is playwright for previous in self._stopped_playwright_fallback):
                return
            self._stopped_playwright_fallback.append(playwright)

        async def stop_playwright() -> None:
            try:
                await playwright.stop()
            except Exception:
                logger.debug("Failed to stop Playwright", exc_info=True)

        task = asyncio.create_task(stop_playwright())
        try:
            await asyncio.wait_for(asyncio.shield(task), timeout=self.close_timeout)
        except asyncio.TimeoutError:
            self._track_background(task)
        except asyncio.CancelledError:
            self._track_background(task)
            raise

    async def _drain(self, deadline: float) -> None:
        while True:
            tasks = set(self._creating) | set(self._background)
            if self._browser_init_task is not None:
                tasks.add(self._browser_init_task)
            tasks = {task for task in tasks if not task.done()}
            if not tasks:
                return
            remaining = deadline - asyncio.get_running_loop().time()
            if remaining <= 0:
                logger.warning("Browser shutdown drain expired with %s owned tasks still running", len(tasks))
                return
            _, pending = await asyncio.wait(tasks, timeout=remaining, return_when=asyncio.FIRST_COMPLETED)
            if pending == tasks:
                logger.warning("Browser shutdown drain expired with %s owned tasks still running", len(tasks))
                return

    async def shutdown(self) -> None:
        """Reject new leases and close all owned browser resources, idempotently."""
        if self._closed:
            return
        if self._loop is None:
            self._closing = True
            self._closed = True
            return
        self._bind_loop()
        if self._shutdown_task is None:
            self._shutdown_task = asyncio.create_task(self._shutdown_impl())
        await asyncio.shield(self._shutdown_task)

    async def _shutdown_impl(self) -> None:
        self._closing = True
        if self._shutdown_event is not None:
            self._shutdown_event.set()

        deadline = asyncio.get_running_loop().time() + self.shutdown_drain_timeout
        leases = tuple(self._active_leases)
        if leases:
            await asyncio.gather(*(lease.close() for lease in leases), return_exceptions=True)
        await self._drain(deadline)

        browser = self._browser
        self._browser = None
        playwright = self._playwright
        self._playwright = None
        await self._close_browser_once(browser)
        await self._stop_playwright_once(playwright)

        # Once the browser is closed, any leases still registered cannot own live
        # browser resources. Their late cleanup tasks retain no page capacity.
        for lease in tuple(self._active_leases):
            if not self._browser_is_connected(lease.browser):
                self._release_lease(lease)

        await self._drain(asyncio.get_running_loop().time() + self.close_timeout)
        self._closed = True


_default_runtime: BrowserRuntime | None = None


def get_shared_browser_runtime() -> BrowserRuntime:
    """Return the process-wide runtime shared by URL and search consumers."""
    global _default_runtime
    if _default_runtime is None:
        _default_runtime = BrowserRuntime()
    return _default_runtime


async def shutdown_shared_browser_runtime() -> None:
    """Close the default runtime without creating an unused browser owner."""
    global _default_runtime
    if _default_runtime is None:
        _default_runtime = BrowserRuntime()
    await _default_runtime.shutdown()
