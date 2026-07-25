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

CDPA should supply its durable request ID to avoid a second identity namespace. The core state directory should be repository- or deployment-scoped and excluded from product commits.

## Failure mapping

- Browser/network/auth failures: CDPA environmental recovery path.
- Ownership conflict: keep the exact hop pending; never allocate another sender implicitly.
- Ambiguous or identity-missing outcome: retain request identity and route to exact watch/recovery, never resend.
- Schema/invariant failure: block and require code repair/review.
- Cancellation unproven: show Stop requested but backend outcome unknown; do not mark the hop cancelled.

## Migration phases

1. Add adapter behind an explicit feature flag for disposable acceptance tasks.
2. Compare old and core observations without allowing dual Send ownership.
3. Move watch/recovery for selected tasks to the core.
4. Move Send ownership to the core for selected teams.
5. Run restart, concurrency, helper cleanup, and secret audits in CDPA context.
6. Delete retired CDPA tab/send/watch primitives after acceptance.

At no phase may both CDPA legacy code and this core be authorized to Send on the same conversation.
