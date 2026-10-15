"""Track Replyer cancellation separately from ordinary Planner interruption."""

import asyncio
from contextlib import contextmanager

from src.chat.realtime_ingress import parse_realtime_session, realtime_ingress_manager
from src.chat.focus.reply_context import release_reply_context
from src.llm_models.model_client.base_client import _cancel_and_drain_request_task


@contextmanager
def reply_generation_interrupt(runtime, source_message, fallback_flag=None):
    """Keep ordinary flags; give direct realtime Replyer its own abort signal."""
    flag = fallback_flag if fallback_flag is not None else asyncio.Event()
    flags = getattr(runtime, "_reply_generation_interrupt_flags", None)
    if flags is None:
        flags = []
        runtime._reply_generation_interrupt_flags = flags
    flags.append(flag)
    try:
        declared, _ = parse_realtime_session(source_message)
        if declared and not realtime_ingress_manager.is_current_turn_message(source_message, runtime.stream_id):
            flag.set()
        yield flag
    finally:
        flags.remove(flag)


def interrupt_reply_generation(runtime) -> int:
    """Only the receiver's verified realtime priority path invokes this."""
    interrupted = 0
    for flag in tuple(getattr(runtime, "_reply_generation_interrupt_flags", ())):
        if not flag.is_set():
            flag.set()
            interrupted += 1
    aborted = 0
    for event in tuple(getattr(runtime, "_reply_generation_abort_events", ())):
        if not event.is_set():
            event.set()
            aborted += 1
    return max(interrupted, aborted)


async def _release_unused_result(task) -> None:
    if task.cancelled():
        return
    try:
        success, response = task.result()
    except Exception:
        return
    refs = getattr(response, "context_refs", None) if success else None
    if refs:
        await release_reply_context(refs, "generation_cancelled")


async def _cancel_generation(task) -> None:
    try:
        await _cancel_and_drain_request_task(task)
    finally:
        if task.done():
            await _release_unused_result(task)
        else:
            # Resistant tasks remain tracked by the common drain helper. A
            # successful late result must also release its context reservation.
            def release_late(done):
                cleanup = asyncio.create_task(_release_unused_result(done))
                cleanup.add_done_callback(lambda result: result.exception() if not result.cancelled() else None)
            task.add_done_callback(release_late)


async def run_reply_generation(runtime, source_message, fallback_flag, generate, **kwargs):
    """Abort the whole generation for verified realtime priority ingress.

    The separate abort event preserves ordinary messages' existing behavior:
    their Planner flag still reaches the LLM client, without cancelling prompt
    preparation. Realtime priority also cancels prompt/auxiliary requests.
    """
    abort = asyncio.Event()
    events = getattr(runtime, "_reply_generation_abort_events", None)
    if events is None:
        events = []
        runtime._reply_generation_abort_events = events
    events.append(abort)
    generation = waiter = None
    cleanup_started = False
    try:
        with reply_generation_interrupt(runtime, source_message, fallback_flag) as flag:
            declared, _ = parse_realtime_session(source_message)
            if declared and flag.is_set():
                return False, None
            generation = asyncio.create_task(generate(interrupt_flag=flag, **kwargs))
            waiter = asyncio.create_task(abort.wait())
            await asyncio.wait({generation, waiter}, return_when=asyncio.FIRST_COMPLETED)
            if abort.is_set():
                cleanup_started = True
                await _cancel_generation(generation)
                return False, None
            return await generation
    except asyncio.CancelledError:
        if generation is not None and not cleanup_started:
            await _cancel_generation(generation)
        raise
    finally:
        events.remove(abort)
        if waiter is not None:
            waiter.cancel()
            await asyncio.gather(waiter, return_exceptions=True)
