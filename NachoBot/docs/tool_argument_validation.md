# Tool argument validation

Tuple metadata returned by `BaseTool.get_tool_definition()` is the authoritative
argument contract. Before a definition is sent to a model, the host validates and
copies its name, description, five-tuple parameters, enum values, and optional
`input_schema` into an immutable snapshot. Each provider call receives freshly
rendered mutable data, so changing a class list or schema cannot mutate a definition
that was already offered.

The snapshot token covers every provider-visible field. A request scope pins the
token for each advertised tool alongside its exact class. Catalog refreshes and
pre-dispatch checks narrow that scope when a class is replaced, metadata changes,
or the tool becomes unavailable. Result-cache scopes include the same tokens, so a
same-class metadata change invalidates entries once without changing the result
cache's mapping shape, expiry, or TTL behavior.

Declared values are checked exactly: strings require `str`; integers require `int`
and reject `bool`; floats accept finite `float` values or integers without
conversion; booleans require `bool`. Required fields must be present. Optional
fields stay omitted when absent, and a supplied `None` is invalid. Enums must be
nonempty lists of compatible scalar values; an empty list is normalized to `None`,
matching the existing provider behavior. The host injects `llm_called=True` into a
fresh argument copy before validating declared fields. `BaseTool.direct_execute`
uses the same validation but does not add this host marker.

Calls may omit all arguments with `None`, which becomes a fresh empty dictionary.
Other roots must be mappings with string keys. Unknown keys are preserved for
open-record compatibility; only declared fields receive type and enum checks.
Validation errors expose a stable code and, when applicable, the declared
parameter name. They never include argument values or raw mappings.

`input_schema` is detached and checked for finite JSON-compatible values before it
is passed through. This stage does not interpret JSON Schema keywords or validate
arguments against JSON Schema; tuple metadata controls host-side validation.
