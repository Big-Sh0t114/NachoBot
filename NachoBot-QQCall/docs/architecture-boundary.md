# QQCall and UniversalVC boundary

QQCall voice messages, private and group, declare `mcp_enabled=false` in
the generic runtime-capabilities contract. The immutable trigger-task scope
controls this gate before MCP access-context/catalog lookup and before MCP
decision/execution. Omission retains ordinary text behavior, including text in
the same chat concurrently. Other preparation capabilities keep their existing
rules; a shared web/sandbox routing request is not an MCP request when MCP is
disabled and its catalog is empty. The proposed merged preparation decision
was not adopted. Group ingress validates the exact voice capability envelope.

QQCall voice ingress also declares `tts_language=zh`, independently of the
ordinary TTS default. Private voice resolves the QQ nickname through the current
adapter connection, bounds profile lookup, verifies the returned UIN, scopes
successful caching to the call, and rechecks turn/connection ownership afterward.
Failed lookup uses the known QQ number; it never invents a generic caller name.

An entered group's complete, validated native audio zero-member notification
may request the same scene-3 `ClearRoom11(sceneId, relationId)` operation used by
QQ's official clear-room handler. The Bridge pins the original group scope and
Start request. Host verifies its independently entered reservation, current login,
audio selection and entry sequence; generic cmd11 remains forbidden. Neither the
notification, post receipt nor ACK alone releases occupancy: fresh SDK14
string-zero is required. Failed confirmation retains occupancy; scoped leave can
retry confirmation without reposting ClearRoom or Quit. Late results cannot affect
a newer confirmation ticket or room. SDK20011 remote-end callbacks also support
independent typed room-absence readback and complete-roster fallback.

A matching successful native query ACK plus an explicit empty error response
completes that query negatively, without creating membership/absence proof or a
timeout quarantine. Missing, malformed, unpaired and genuinely timed-out queries
retain their previous fail-closed behavior.

Membership callback diagnostics retain only the kinds of a fixed allowlist of
root and notifyInfo fields. They never invoke accessors, retain member identities
or raw field values, alter event forwarding, grant admission, or clear occupancy.
The pinned onGroupAudioMemNumChange native callback supplies direct args[0]
membership fields, as verified in Host349. The renderer's notifyInfo envelope
must not be imposed on this native event. Audio field types, generation, and TTL
validation remain unchanged; the video callback contract is not inferred from it.

A typed scene-1 invitation received while the exact, independently entered group
owns the SDK room is a busy invitation, not a second private room. It remains
forwarded for the existing scoped busy notice. Host cmd5 stays blocked by the
group reservation, and group Quit still needs its owned ACK plus fresh witnessed
SDK absence. A real private entry, changed native entry, unproven group entry,
or uncertain group state retains the conservative private/context barriers.
The bounded busy-invitation counter is diagnostic only and grants no release.
Group lease rejection logging uses only the immutable, fixed-code route rejection
and distinguishes refresh timeout/error/rejection/context loss. It retains the
original two-second proof fence and does not expose scopes, endpoints or raw errors.

A completed control command gives its exact surviving lease owner the ordinary
leaseMs response window, including on failure. This does not renew audio proofs.
Queued commands that would expire when the last execution pin drops stay rejected;
only ordinary owner activity can preserve them. Service stop or lease replacement
cannot revive the old owner, and diagnostic reads never refresh the response window.

QQCall owns call controls, call lifecycle events, call-scope proofs, and the
QQ-specific low-level PCM device bridge. Call control remains explicit and
scoped to a live adapter, peer, service instance, host generation, connection
generation, call ID, and call generation. A control request means that the
adapter accepted the request; it does not prove that QQ completed the action.
Private scene-1 controls have typed native Quit8/Reject9/Close10 parameter
builders. A posted command or successful ACK is not, by itself, proof that a
call ended or an invitation was rejected; terminal state still requires its
independent correlation. For an unentered private invitation, rejection can now
release its reservation only after the exact posted Reject9 success tuple and
independent Host state proof, with the same login, peer, invitation, generation,
and current native scope throughout. A monotonic Host-incarnation record proves
whether any admission command 4/5 may have been dispatched or any 20004 entry
callback was observed. Only a never-admitted Host can retire its unentered
invitation without a room getter. After possible room admission, the same login
must have a successful typed private SDK entry and fresh SDK14 string-zero
readback. The uninitialized SDK is never queried. Host325 real first-invitation
Core refusal, restoration and same-Host redial verified the cold proof.
An attempted accept on the current invitation, room
entry, reused request ID, stale scope, failed/missing readback, or foreign ACK
keeps the barrier. This proves a declined invitation with no SDK room; it does
not label an entered room destroyed. Diagnostic reads alone never release it.
Host310 real controls observed a numeric private room while connected and
string-zero after normal remote end and after dependency refusal. Reloaded
Host319 also observed successful refusal, restored Core, and redial in the same
Host generation. Host331 also verified TTS-specific refusal and restored redial;
its successful first read did not exercise the bounded reject retry branch.
The full fault matrix remains pending. Verified Kernel room end synchronizes the Host's exact entered private
reservation through SDK absence; late publication has three bounded reads in
one five-second budget and stops for any changed private state or different room.

The Core scheduler accepts only the provider-neutral
`additional_config.realtime_session` declaration. It owns bounded session
leases, row IDs, replay suppression, expiry, and priority over offline
recovery. It does not inspect QQ call fields or contact a QQ service. The
QQCall plugin validates the declaration against the independently parsed call
scope, private peer route, current QQ adapter status, and current service turn.
It rechecks the canonical QQCall enable switch during registration and proof.
Senderless lifecycle events continue through ordinary message context and
Planner ingress; authorized speech rows use the direct Replyer path.

When the canonical QQCall switch is enabled, the supervisor checks the
configured UniversalVC loopback health endpoint before adapter discovery. A
healthy API instance is reused. Only a confirmed connection refusal from an
HTTP loopback root permits startup through the already-installed UniversalVC
`.venv` and `service_main.py` CLI; no dependency installation or model download
is part of startup. An existing UniversalVC `service.toml` is read as-is. If it
is absent, QQCall passes an empty private TOML from its runtime directory so
UniversalVC keeps its own project-root model defaults. Environment overrides
for the documented UniversalVC host, port, and token are passed only to the
child process. An empty public `token_file` resolves to a private runtime token
file; the supervisor reuses a valid existing environment/config token or
creates a private token on the first enabled run. It never places that token
in command-line arguments, receipts, or public status.

`check`, `plan`, and disabled runs do not probe, create token/config files, or
launch the API. Startup failure leaves QQ control supervision running and
surfaces a fixed UniversalVC audio-unavailable status. API health confirms the
service protocol and pipeline capability only; it does not verify QQ device
routing or report a live audio call. A service started by QQCall is shared and
continues running after QQCall stops, because the public API has no global
shutdown operation. Startup failure cleanup uses only the exact recorded
Popen process family and verifies those identities are gone; it never kills a
process by port.

On Windows, the WebUI grants breakaway permission only to the QQCall
kill-on-close job. The UniversalVC CLI child explicitly requests
`CREATE_BREAKAWAY_FROM_JOB`, so it can outlive QQCall's job cleanup; Host and
Bridge remain contained in the QQCall job. If Windows rejects the breakaway
request, startup fails closed and does not retry without the flag.

UniversalVC owns its frozen service API and inbound-audio VAD, ASR, speaker,
and playback pipeline. Its `external` / `output=none` mode has no external
output sink. Core creates reply audio through the common voice/voice-stream
message contract, the adapter binds that output to the call, and the QQCall-only
PCM driver sends it to the existing QQ device endpoint. The driver may decode
WAV, resample, pace device writes, drain, and cancel playback; it does not
implement VAD, ASR, or TTS. All low-level PCM capture and sink code stays under
`NachoBot-QQCall/**`.

PCM downsampling uses a bounded, phase-cached windowed-sinc FIR to reject
frequencies above the destination Nyquist limit. It preserves chunk-independent
output and exact flushed sample counts; upsampling keeps its existing path.
At 48 kHz to 16 kHz the lookahead is about 1.31 ms. This remains transport
resampling, with no VAD, recognition, speaker classification, new model, or
change to the frozen UniversalVC API. Host397 has loaded the correction;
frequency-response and live transport evidence do not establish that the
remaining QQ speaker-separation failure is fixed.

The shared Core generator binds a random `reply_generation_id` and a validated
`reply_source_row` to structlog's existing task context while preparing and
generating a reply. Parallel preparation tasks inherit it; leaving the scope,
including cancellation, restores the caller context. These diagnostic fields
carry no peer identity, prompt, endpoint, permission, or call proof. They do not
change model selection, retries, tool admission, or reply generation behavior.
Host397 real private input 907163 verified the correlation after the Core reload.

Capability routing rules use a caller-owned system message; bounded history,
catalog metadata and the current request remain user-role JSON data. Other
LLMRequest callers retain their default one-user-message contract. A strict,
complete quoted say/read request does not promote actions inside that literal
into MCP or sandbox instructions; trailing real actions and external-state
requests continue through routing. These boundaries have unit and isolated
classification evidence, but Host397 real unquoted speech still misroutes to
browser MCP. They do not establish low latency or complete routing correctness.
The proposed merged reply-preparation design is not implemented or enabled.

The QQ plugin's current-turn status proof rejects an already-queued row before
Replyer dispatch when new speech has invalidated its delivery turn. If the turn
changes after Replyer generation has begun, the adapter delivery fence refuses
stale output, but the LLM work may continue; this design does not promise
generation cancellation.

The frozen API files are owned by UniversalVC and must remain aligned with its
service contract. The existing QQ endpoint call-scope proof remains in place.
The root README records observed SnowLuma private-call playback and the current
group-room validation separately. These observations do not establish every
lifecycle case or long-term stability. Static tests alone do not prove a live
call, audio capture, or playback path.

Group Kernel observations are maintained by the QQCall Bridge as diagnostics.
`POST /v1/group/diagnostics` uses the control token and the current adapter
lease, with the same `accountId`, `clientInstance`, and `groupId` request as
`/v1/group/status`. It returns that status plus `diagnosticOnly=true` and a
bounded diagnostic snapshot. Membership counts, invitation metadata, and
native-timeout/zero-member observations expire independently after 30 seconds;
at most 32 groups are retained. Member identities are omitted. Connection
invalidation permanently stops the observer for that Bridge instance.

An invitation's native room identifier and member counts do not authorize a
join. When the global call slot is idle, `/v1/group/status` obtains a fresh
native existing-room membership proof and may return `nativeJoinVerified=true`
with a live room. Occupancy, an empty room, or a changed connection prevents
positive admission. `/v1/group/join` must additionally pass adapter ACL and
command/regex-plus-LLM policy, then receive a correlated successful scene-3
entry callback. Admission does not create an audio session or prove joining.
An exact-generation audio notification with zero online members and a complete
empty member list may refuse an idle admission for at most five seconds. A
newer native self-invitation or nonempty invitation update suppresses that
negative hint. Missing, partial, expired and conflicting observations retain
the normal native room query. No observation authorizes a join or releases a
scope, and an existing native query quarantine remains in place.

The diagnostic endpoint reads only cached observations and occupancy; it does
not issue a room query, refresh the adapter lease, or report a positive room
proof. Its `nativeJoinVerified` is always false and `room` is null. The separate
admission status endpoint retains the original fresh native query and fences.

Production supervisor runs use Broker service mode, which has no probe
lifetime timer. The separate CLI probe mode retains a bounded lifetime. Loss
of a consumed one-shot Shim connection currently requires a fresh robot QQ
process before rebinding; this limitation and the outstanding recovery tests
must remain explicit in the delivery audit.

Private rejection keeps its exact ACK, invitation, identity and generation
fences. On an initialized SDK, a numeric room value that remains unchanged
can permit at most three fresh reads with 250 ms gaps within the original
five-second total budget. That value is never attributed to the unentered
invitation and never grants clearance. Only a successful new string-zero
readback clears that current invitation. A different numeric value, unknown
string, error or changed context keeps ownership. The separate cold Host
proof still requires no possible admission in the entire Host incarnation
and never reads an uninitialized SDK. `privateRejectObservation` exposes
fixed kinds, lengths, counters and booleans; it grants no recovery authority.
Native control diagnostics also count invitation, error and timeout callbacks
and retain their last observation time and bounded numeric/shape fields only.
An undocumented first integer (including a scene ID) is never labeled a return
code. These observations neither prove call correlation nor authorize cleanup.

Authenticated administrative official-AV start/drain/stop diagnostics reuse the
existing finite Agent observer in the exact controller/Host generation. They do
not claim or refresh adapter leases, dispatch call commands, grant admission or
clear occupancy. Unknown/raw reports are rejected, generation loss discards the
result and cleans the observer, and service shutdown cleans a late installation.
The service uses incoming-only mode: no native method replacement or debugger
attachment. The standalone CLI's explicit native-conditional mode is not enabled
by the service after the pinned QQ reported an unconfirmed debugger connection.
Only known private Reject9 reason enums are projected; peer/request identifiers
remain document-local references. Outgoing observations are attempts, with the
observer's explicit coverage gap, never completion or full-coverage proof.

Incoming command-9 SDK replies can additionally project only an int32
`rejectAck.returnCode` from the exact two-field number/string ACK shape.
This passive path works without replacing the native method or attaching a
debugger. ACKs contain no scene or peer identity: they are document-scoped
diagnostics, not proof of a particular private call, rejection reason,
successful network delivery, or authority to close a window. Raw error text
is not retained; malformed, accessor-backed or extra projection fields fail
validation. Earlier reports whose allowlist omitted incoming 9 have no
coverage of these ACKs.

Admission's UniversalVC health validation requires the same nonempty string
`service_instance_id` (at most 128 characters) as the supervisor reuse contract,
alongside protocol 1, ready, and available pipeline/VAD/speaker capabilities.
An invalid instance identity is reported as a missing UniversalVC link even when
health claims ready; other missing links are still collected in the same notice.

`POST /v1/call/diagnostics` is an administrative read using the existing
loopback control bearer and exact `{accountId}` body. It projects only fixed
call stages, booleans, known failure codes and event counters, with no peer,
call ID, credentials, native commands or scope proof. It neither claims nor
refreshes an adapter lease, consumes events, nor clears occupancy. Generation
changes discard the snapshot; unknown labels and codes are redacted.

Host `nativeControlObservations` records bounded accept/signal ACK and native
entry/end callback facts. It includes only kinds, bounded first integers, counts
and recent-request-match booleans. Only verified command 5 and entry 20004 expose
that integer as a return code; signal 55 and end-event integers remain uninterpreted. It is diagnostic only: no payload strings,
identities or protocol authority are derived from it, and it cannot clear a
private reservation. The recent-request history is capped at 256 entries and
matching is limited to five seconds; a reused or already observed ID is not
reported matched.

`groupRoomQueryObservation` separates command-13 acknowledgement counts and
matched first-integer/type facts from command-20010 room-result counts. Neither
an acknowledgement nor its first integer supplies a room proof or lifts the
timeout quarantine. These diagnostic fields omit payload strings and identity.

A confirmed owned group Quit also establishes that the shared SDK room getter
has been initialized for that exact acknowledged login. This requires the
existing matched group entry, positive physical-room witness, own Quit ACK and
fresh post-Quit string-zero readback. It permits only a new private-rejection
read after a current matching Reject ACK; it never reuses the old zero as
absence proof, changes the cold-Host no-admission rule, or lifts an uncertain
slot without a fresh read. A changed login, newer invitation or entry, nonzero
room, wrong ACK or changed context retains the private barrier.

Production official-private-invite cleanup is independent of diagnostics. The
Agent captures only fresh canonical AV documents and matches native invitation
evidence, listener registration, peer/self identities and all seven invite
parameters. Authenticated IPC accepts a fixed lifecycle operation, never a
script, selector or window ID. A current Reject ACK permits one scoped official
receiving-control action; document termination must be confirmed before a new
Host room-absence proof can release occupancy. Ambiguous UI, user input, newer
invites, entry, stale documents or late scripts fail closed. Accept only retires
cleanup eligibility. See `official-private-invite-lifecycle.md` for the ownership
and document-epoch cancellation rules; component coverage is not live recovery.

The pinned official CallStore also hides a reusable AV document on hangup.
Receiving closure is a separate completion kind: the scoped action must start
on a visible, non-minimized window, receive exactly one subsequent successful
official Reject ACK, and observe one hide without show, newer invite, entry or
user input. Neither hidden state nor ACK alone suffices. This does not claim
document destruction and still precedes the mandatory fresh Host absence proof.
Only a confirmed closure rearms the reused document with a new capture epoch.
