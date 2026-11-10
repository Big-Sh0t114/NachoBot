# Shared tool cancellation boundaries

The plugin and MCP executors use `src.common.task_cancellation` to stop
host-controlled dispatch and result publication after a request task receives a
new cancellation. The helper is stdlib-only and records the cancellation count
at the root boundary. Nested boundaries in the same `asyncio.Task` borrow that
baseline; a task created with an inherited context starts its own scope.

`@cancellation_boundary` checks at entry and before returning or re-raising an
ordinary error. `cancellation_checkpoint()` yields to the event loop and then
checks for a new cancellation. `check_cancellation()` avoids a healthy-path
yield and yields only when the task's cancellation count increased. A native
pending `CancelledError` keeps its original reason. A cancellation already
handled before a new root scope, and an `asyncio.timeout()` cancellation that
restores its count, do not poison later work.

The executors place checkpoints after prompt formatting, model and tool work,
preparation hooks, result normalization, and between batch or MCP round calls.
They place fair yields before fresh authorization gates, then authorize and
invoke without another healthy-path yield. Cache admission and result
publication perform a final conditional cancellation check.

These boundaries are cooperative. They stop later host dispatch and prevent
unpublished results or new cache writes after observed cancellation. They do
not stop a callback already running, roll back completed external effects,
cancel SDK-owned tasks, or release resources owned by callbacks.
