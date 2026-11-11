# Shared SDK plugin lifecycle

Each live SDK adapter instance owns one `SDKPluginLifecycle` for its actual V2
plugin object. Command, action, tool, and event callbacks all use that coordinator.
The temporary V2 object created while inspecting the SDK module is separate and
does not own the adapter lifecycle.

The first callback publishes one initialization task. Concurrent callbacks join
that task, and a callback runs only after initialization succeeds. A failed or
cancelled attempt reaches every waiter; a later callback may retry after that
attempt has ended. Cancelling a callback waiter does not cancel initialization.
The `_on_load_called` field remains a compatibility mirror and is not lifecycle
authority. Live initialization and cleanup tasks also remain strongly referenced
in a small helper-module set until completion, with their final exception or
cancellation observed before the reference is released. This lets a pending SDK
hook finish if the adapter and its owner are collected.

`close()` seals callback admission before it waits. It requests cancellation of a
pending initialization once and waits for that task to actually finish before
deciding whether `on_unload` should run. A load that completes successfully after
the close request is cleaned up, while its waiting callbacks are still refused.
The unload hook runs at most once. A close caller waits up to the configured
positive finite deadline (10 seconds by default); timeout returns `False` while
the cleanup task continues, and a later caller can join that same task. Cancelling
one close caller does not cancel cleanup.
An `on_unload` exception or hook-raised `CancelledError` makes cleanup terminal
`False`; the hook is not retried. Existing manager removal and reload transaction
semantics remain unchanged, so cleanup failure does not roll back registry
removal. Reload can overlap across different plugin instances because each
coordinator serializes only its own adapter instance.

The coordinator binds to the first event loop that uses it. Calls from another
loop and recursive lifecycle calls made by the plugin's own load or unload hook
raise promptly. Hook context markers expire when the hook finishes, including for
descendant tasks that inherited the context.

The host still owns business callback tasks. This lifecycle does not drain
callbacks already running when close begins, serialize separate plugin instances,
or force native resources to stop during event-loop shutdown. A plugin whose load
hook partially initializes resources and then fails remains responsible for its
own rollback.

The isolated regression test can be run with:

```powershell
python -B -m unittest discover -s tests -p test_sdk_plugin_lifecycle.py
python -B -m unittest discover -s tests -p test_plugin_manager_lifecycle_isolated.py
python -B -m unittest discover -s tests -p test_plugin_lifecycle_isolated.py
```

The SDK adapter and lifecycle tests load the actual source against fake component,
plugin, and logger modules; the manager regression reuses the isolated manager
fixture. These checks do not import Core configuration or database initialization
and do not establish acceptance against a live plugin host or native resource.
