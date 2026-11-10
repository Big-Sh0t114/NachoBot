# Low latency direct memory

Adapters can request the shared direct memory stage with these message runtime
capabilities:

```json
{
  "planner_bypass": true,
  "memory_retrieval": true,
  "memory_retrieval_mode": "direct"
}
```

`planner_bypass` controls reply scheduling. `memory_retrieval_mode` independently
selects the memory path; its default is `standard`, which keeps the existing
Planner/ReAct retrieval behavior. `memory_retrieval: false` disables both paths.
The direct path is shared by private and group Replyers and receives the
authoritative current human message plus bounded recent human history. It runs a
filtered `MemoryService.search` without asking Planner or ReAct to formulate a
question, and does not read the standard path's ThinkingBack answers.

The search has a 2.5 second limit and the complete preparation stage, including
SQLite access, has a 3.5 second limit. A timeout or failure degrades to the
current conversation context. Identified users get correction overlays scoped
to `(chat_id, platform, user_id)` in the lazily opened
`data/dialogue_memory.sqlite3`; evidence and corrections are bounded, and
corrections retain source message IDs and timestamps. Recognized direct
first-person corrections suppress only matching actor/attribute claims. Recognized
ambiguous correction cues are retained as disputes for the prompt to clarify.
Recognition is conservative and pattern-based: unmatched wording, including some
denials without a replacement value, stays ordinary dialogue and does not create
a persistent correction. If correction state cannot be read or committed, retrieved facts are
withheld for that turn.

Verified anonymous realtime speakers may use read-only recall filtered to the
current chat. Their temporary speaker references are not stored as principals,
and no correction overlay is read or written for them. Unverified actorless and
system messages do not use direct memory.

The direct path, scope filtering, correction behavior, timeouts, cancellation,
and Replyer prompt integration are covered by isolated tests. Live-model recall
quality and latency have not been measured.

The current planner-bypass voice integrations opt into this mode for Bilibili
live, UniversalVC, Discord voice, WebUI voice, and QQCall private and group
speech. Their replyer templates include `{memory_retrieval}` so the shared
stage's recall can reach the model. Ordinary text messages keep the standard
Planner/ReAct path unless an adapter explicitly selects direct mode. The
existing global and filtered memory opt-outs still disable retrieval.
