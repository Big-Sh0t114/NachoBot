# Shared event message pipeline

The event manager prepares a message snapshot for the current dispatch and keeps caller-owned message objects separate from plugin code. Each handler receives its own deep copy of a non-`None` message. A handler's in-place edits therefore stay with that callback unless an interceptor explicitly returns a replacement in the fifth slot of its result tuple.

Interceptors run in order. Each interceptor receives the latest committed replacement, or the original prepared snapshot if no interceptor has committed one. Returning `None`, mutating the input and returning `None`, or raising an ordinary execution exception leaves the latest committed snapshot in place. A valid non-`None` fifth value replaces the whole snapshot; the manager does not merge individual fields. Copying the returned snapshot detaches the host result from references retained by the plugin.

Non-intercepting observers receive independent copies of the original prepared snapshot. Their returned replacement and edits are ignored by the event pipeline. The `ON_START` and `ON_STOP` lifecycle callbacks continue to receive `None` when there is no message.

The existing continuation and history behavior remains in force. In particular, a returned `continue_processing=False` remains effective when the replacement is rejected, and later handlers still run under the existing pipeline policy. A fifth value with the wrong type, or a valid message whose detached copy fails or has the wrong type, rejects only that replacement and preserves the previous committed snapshot. Failure to copy a handler's input prevents that handler from being scheduled and uses the dispatch path's existing non-fatal fallback.

The pipeline uses `NachoMessages.deepcopy()` at ownership boundaries. This is message isolation for normal plugin use; it is not a security boundary or a resource limit for arbitrary objects with custom copy behavior.
