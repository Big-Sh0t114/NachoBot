# A_Memorix graph cache compatibility

This checkout carries a focused compatibility port of the graph revision behavior
already present upstream in commit
`6bcb2e068cd98793a132cb8cc4ebf3cc00cb8fc3`. The local A_Memorix copy does not yet
expose `graph_revision` and `node_revision`, which let the PPR and entity matcher
caches distinguish same-size graph edits and same-count node replacement.

`graph_revision` advances when weighted adjacency changes. `node_revision` advances
when the ordered node names change, and those changes also advance the graph
revision. Relation-hash metadata changes without an adjacency change do not advance
the numeric graph revision. Explicit `batch_update()` scopes coalesce revisions at
the outer exit, while matrix-derived caches are invalidated as soon as a mutation
occurs.

The retrievers include the graph revision in PPR cache keys and gate entity matcher
reuse on the node revision. They skip graph-derived caches while a mutation is in
progress and make one bounded retry if a revision changes during PPR computation.
If the graph remains unstable after that retry, the request receives no PPR scores
and the unstable result is not cached.

This is a narrow local correctness port under the exception described in
`src/A_memorix/MODIFICATION_POLICY.md`; it does not change the storage schema or
initiate an upstream write.
