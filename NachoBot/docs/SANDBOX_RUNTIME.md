# Sandbox task and artifact delivery runtime

Sandbox file tasks start only after the adapter confirms that the exact
server-generated acknowledgement was delivered. The receipt must be
`SendStatus.DELIVERED`, include the target stream and platform message ID, and
match the acknowledgement content fingerprint bound to the handoff.

The task claim is committed to SQLite under `data/sandbox/.tasks` before an
async task is created. `.outbox` contains one immutable, hash-checked copy for
each committed artifact, with its original relative filename and extension.
The copy comes from the immutable postimage recorded by the sandbox transaction
journal; publication never reads the mutable user workspace path. Transaction
journals and outbox copies are protected from sandbox session cleanup.

## Recovery

Startup acquires the OS file lock at `.tasks/owner.lock`, recovers unfinished
file transactions, and reconciles task rows against committed transaction
journals before plugins or message ingress can start. A second live process
cannot own the same store. Graceful shutdown closes admission, drains or cancels
tracked work, and releases the lock. A process crash releases the OS lock.

Persisted `QUEUED` tasks are reauthorized and wait for their original platform
route before they start. Persisted `RUNNING` tasks become `INTERRUPTED` after a
restart; model work is never assumed to resume from a checkpoint. A committed
journal reconstructs its artifact manifest even when a crash happened before
the task row recorded the commit. Persisted `SENDING` artifacts become
`DELIVERY_UNKNOWN` after restart and are never sent automatically again.

## Artifact states and operator review

- `PENDING` and `RETRYABLE` artifacts may be sent from the outbox.
- `DELIVERED` records the real platform message ID; that artifact is never
  automatically sent again.
- `DELIVERY_UNKNOWN` means the send may have reached the platform. ACK timeout,
  arbitrary exceptions, invalid receipt values, and an in-flight shutdown or
  crash all stay in this state without automatic retry.
- `RETRY_EXHAUSTED` means every automatic attempt was rejected by an explicit,
  allowlisted pre-send result. The task remains known-unsent for that artifact.
- `INVALID_SNAPSHOT` and `BLOCKED_AUTHORIZATION` prevent publication until the
  snapshot or permission issue is resolved.

Automatic retries are limited to five attempts and only to demonstrable
pre-send results (`STALE_LEASE`, the exact `SUPPRESSED/reply_policy` result, or
specific `FAILED` details that return before adapter enqueue). A generic
`FAILED` receipt is not enough to retry. Receipt sequence numbers remain
monotonic across operator retry cycles; only the automatic-attempt counter
resets for a new cycle.

The runtime exposes persisted status through
`sandbox_delivery_gate.status(handoff_id)`. An exhausted, known-unsent artifact
can be retried with `sandbox_delivery_gate.retry_artifact(handoff_id, path)`.
Retrying `DELIVERY_UNKNOWN` requires the explicit
`allow_possible_duplicate=True` override after an operator checks the target
platform; that override accepts duplicate delivery risk. There is no user
command or UI for these methods in this scope.

A standalone `SandboxAgentCoordinator` with nonempty changed paths fails closed
unless its caller supplies a publication callback. Production binds the
durable gate publisher during startup; tests or other explicit integrations may
continue to provide a custom `publish` callback.

Task rows, transaction journals, postimages, and outbox copies currently have
no automatic retention policy. Keep pending and unknown records until an
operator resolves them; delivered records also remain available for receipt
and recovery audits.

## Remaining crash window

The upstream send caller receives the platform ACK before it persists the
handoff into `.tasks`. A crash in that ACK-to-persistence gap can lose a
delivered handoff. The runtime does not infer an acknowledgement from a missing
task row and never starts model work without a persisted, validated ACK claim.
