# Shared global configuration reads

`src.plugin_system.apis.config_api.get_global_config(key, default=None)` resolves
the dotted path through attributes on the global config object and returns a
`deepcopy` of the selected value. Ordinary lists, dictionaries, sets, tuples,
scalars, and configuration dataclasses keep their Python types. Mutable nested
data in the returned value is detached from the object held by the host, so a
plugin can safely modify its result without changing what another consumer reads
through this API.

Each call reads the object currently bound to `config_api.global_config`; there
is no result cache. Changes to that object are visible on the next call, while
previous results remain snapshots. The API imports this object directly, so
replacing `src.config.config.global_config` later does not automatically update
the API's binding.

When attribute lookup fails, the API keeps its existing warning and returns the
exact caller-provided default. If copying a found value raises an `Exception`,
the API logs only the requested key and exception type, then returns that same
default. `BaseException` control signals still propagate. Defaults belong to the
caller and are returned as-is, without copying.

This is a defensive copy of normal configuration data. It is not an untrusted
plugin sandbox, an atomic reload mechanism, or a guarantee that custom
`__deepcopy__` hooks are pure. This API's behavior does not change the separate
`get_plugin_config` method or plugin-owned configuration accessors.
