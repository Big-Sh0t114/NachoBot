# Model selection failure penalty recovery

`LLMRequest._select_model` ranks the currently available candidates with the
existing score:

```text
total_tokens + 300 * failure_penalty + 1000 * in_flight_usage
```

Before ranking, it now reads the clock once and clears a candidate's failure
penalty when that penalty is positive and more than 300 seconds old. It applies
this recovery to every candidate left after `exclude_models` and
`eligible_models` filtering, including candidates that are not selected. The
total token count, in-flight usage, and penalty timestamp stay unchanged. The
strict `> 300` boundary and insertion-order tie behavior are preserved.

This lets an expired candidate compete on its current score during the first
selection after recovery. The selected model's usage count is still read from
the live usage map after client creation, so synchronous client-factory updates
are retained before the existing in-flight increment. Client construction,
embedding and timeout ownership, and lease release behavior are unchanged.

Run the isolated regression coverage from `NachoBot` with:

```powershell
python -B -m unittest tests.test_model_selection_recovery
```

The tests compile only `_select_model`, `acquire_model_lease`, and
`release_model_lease` from the production source AST with fake clock,
configuration, registry, and logger objects. They do not import Core or touch
application configuration, databases, or logs.
