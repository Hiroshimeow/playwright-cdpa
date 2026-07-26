# Future CDPA adapter design

This task does not modify CDPA. A later adapter should be thin and should delete, rather than wrap, duplicated browser transport logic after independent migration acceptance.

## Adapter responsibilities

CDPA should retain:

- task/role routing;
- prompt and attachment construction;
- report validation and route parsing;
- dashboard controls and task manifests;
- CDPA-specific recovery policy.

The core should own:

- CDP connection and helper-page lifecycle;
- real composer and Send/Stop operations;
- irreversible-boundary persistence;
- exact request/turn identity;
- graph monitoring and convergence;
- conversation mutation ownership;
- restart recovery and no-duplicate-Send policy;
- sanitized transport failure taxonomy.

## Proposed call mapping

| CDPA operation | Core call |
|---|---|
| New role conversation | `send(prompt, fresh=True, request_id=<CDPA request id>)` |
| Continue role conversation | `send(prompt, conversation=<id>, request_id=<CDPA request id>)` |
| Attach after worker restart | `watch(<request id>)` |
| Resume exact in-flight hop | `recover(<request id>)` |
| Wait before next queued task | `wait_idle_and_send(...)` |
| Operator Stop | `cancel(<request id>)` |
| Dashboard status | `get(<request id>)` plus CDPA projection |

CDPA should supply its durable request ID to avoid a second identity namespace. The adapter must use that same exact ID for the turn filename, public `get/watch/recover/cancel` calls, and embedded core request identity; it must treat any mismatch as corrupt state and must not remap or repair it heuristically. Each CDPA repository may keep its own result `state_dir`, but every worker that can mutate the same persistent CDP browser must use one shared absolute `coordination_dir` and one explicit deployment ID. The adapter must not derive that path from a repository CWD or a relative `XDG_STATE_HOME`. Neither the repository path nor local turn-store path belongs in the shared coordination record. Both persistence locations must be excluded from product commits.

## Failure mapping

- Browser/network/auth failures: CDPA environmental recovery path.
- Ownership conflict: keep the exact hop pending; never allocate another sender implicitly. A foreign or stale shared claim must be resolved operationally; the adapter must not inspect another repository's local turn store or delete the claim heuristically.
- Ambiguous or identity-missing outcome: retain request identity and route to exact watch/recovery, never resend. For an uncertain existing-conversation click, the adapter must preserve the core baseline record unchanged; it must not rebuild, weaken, or discard baseline fingerprints to force recovery.
- Schema/invariant failure: block and require code repair/review. Unknown or missing nested identity fields are corrupt state, not forward-compatible metadata to be silently dropped.
- Cancellation unproven: show Stop requested but backend outcome unknown; do not mark the hop cancelled.

## Migration phases

1. Add adapter behind an explicit feature flag for disposable acceptance tasks.
2. Compare old and core observations without allowing dual Send ownership.
3. Move watch/recovery for selected tasks to the core.
4. Move Send ownership to the core for selected teams.
5. Run restart, concurrency, helper cleanup, and secret audits in CDPA context.
6. Delete retired CDPA tab/send/watch primitives after acceptance.

At no phase may both CDPA legacy code and this core be authorized to Send on the same conversation.
