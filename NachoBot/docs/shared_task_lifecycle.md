# Shared async task lifecycle

`AsyncTaskManager` owns the application's named `AsyncTask` runners. The existing
manager remains the shared entry point for online time, statistics, telemetry,
advanced idle work, database aggregation, and mood tasks. Its public call shapes stay
the same: `add_task(task, call_back=None)`, `get_tasks_status()`, and
`stop_and_wait_all_tasks()`.

## Registration and replacement

The manager captures `task.task_name` when a task is registered and uses that stable
key for ownership and cleanup. Changing the task object's name later does not leave a
stale registry entry. Completion cleanup removes the entry only if it still points to
that exact `asyncio.Task`, so a late callback cannot remove a newer owner.

Adds for one name are serialized. Replacing a task requests cancellation once and
waits up to five seconds by default. A replacement is published only after the old
runner has actually finished. If it does not finish before the deadline,
`add_task()` raises `TimeoutError`; the old task remains owned and appears as
`{"status": "stopping"}` until its completion callback drains it. Other names can
still be registered while a retiring task finishes.

If an add caller is cancelled while it waits for a replacement, that
`CancelledError` propagates and no replacement starts. The old task stays owned while
it drains. A task also cannot register a replacement under its own managed name.
Per-name coordination records are removed when their last holder or waiter exits.

`AsyncTask.start_task()` observes its generation's abort event during both the initial
delay and repeat interval. It checks cancellation around calls to `run()`, including
when `run()` catches `CancelledError`, so a retiring periodic runner cannot start
another iteration. An exception from `run()` still ends that runner.

The completion observer always retrieves the task result. A supplied completion
callback runs once, synchronously, with callback errors logged and contained.

## Shutdown

The first shutdown call closes admission, sets the current generation's abort event,
invalidates adds already waiting to register, and snapshots current owners. Concurrent
callers join the same manager-owned shutdown operation through a shield; cancelling
one waiting caller does not cancel shutdown.

Shutdown requests cancellation once for each owned task and uses one ten-second global
deadline by default. At the deadline, unfinished tasks remain strongly owned and are
reported as `stopping`; they are not removed to make the registry look empty. Their
completion callbacks remove them and retrieve their outcomes when they eventually
finish. The manager then reopens admission with a fresh abort event. The old event
stays set, and a still-running task with the same name must finish before a new owner
can be published.

A managed task that calls `stop_and_wait_all_tasks()` is included in the shutdown
snapshot. The manager cancels that caller as part of shutdown, so the call propagates
`CancelledError` to the managed task; the shared shutdown operation continues and
drains it without waiting on itself.

Timeouts can be shortened for deterministic callers or tests with the optional
`replacement_timeout` and `shutdown_timeout` constructor keywords. Both accept finite,
nonnegative values. The global manager keeps the default five-second replacement and
ten-second shutdown limits.

## Limits

Cancellation is cooperative. A coroutine that suppresses cancellation can outlive the
deadline; the manager keeps it visible and will not overlap another registered task
with the same name. Cancellation of an `asyncio.Task` also cannot stop a thread already
running through `run_in_executor`, nor undo external side effects. In particular, the
database aggregation runner's executor thread is outside this task-level no-overlap
guarantee.

These deadlines apply to `AsyncTaskManager` operations only. Other process shutdown
cleanup, including the task cleanup path in `bot.py`, has its own waiting behavior and
is not made globally bounded by this manager change.
