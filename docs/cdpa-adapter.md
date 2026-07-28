# Future CDPA V2 adapter design

This document describes a future adapter only. The CDPA repository was inspected read-only; this task does not modify or integrate it.

## Scope boundary

Current CDPA V2 owns:

- task/team/role orchestration, manifests, routes, dependencies, and worker scheduling;
- exact physical-role tab ownership and operator/manual-composer policy;
- prompt construction and immutable attachment snapshots;
- dashboard commands, projections, controls, and operator continuation;
- durable task/hop records, `DurableSendBlock`, and request-ledger lifecycle.

`playwright-gpt-core` should own only reusable browser execution beneath that control plane:

- deployment-wide conversation mutation ownership;
- exact page/conversation resolution;
- composer readiness and steering policy;
- irreversible real-Send protocol;
- exact response retrieval/recovery;
- internal owned-helper lifecycle.

The adapter must not become a second worker, router, task store, dashboard API, maintainer, or scheduler.

## Identity mapping

Use one identity namespace:

```text
CDPA hop request ID == core request_id
```

Do not generate a second adapter/core request ID. The current CDPA hop request identity already supplies the durable idempotency key. Every retry, worker restart, operator continuation, and same-hop recovery must reuse it.

Repository-local core state must use one CDPA-managed result root for every process handling the same migrated hop. That path is not the deployment-wide owner identity: coordination is not a cross-repository result ledger, and another result root cannot recover the hop merely by reusing its request ID.

## Ownership mapping

Current CDPA distinguishes task/team/physical-role/tab ownership. Preserve that authority above the core:

```text
CDPA exact role/team/task/page proof
        -> adapter authorizes one core call
        -> core exact conversation/page claim
```

CDPA remains responsible for deciding whether a role is allowed to use a page and whether manual composer content belongs to an operator. The core independently requires one exact conversation URL, one exact page candidate, an empty/owned composer, no foreign attachments, and one shared conversation claim before Send.

Every CDPA worker/process allowed to mutate CDP 9222 must use one shared absolute core coordination root and one deployment ID. Different worktrees or repository-local result directories must not create separate ownership namespaces for the same browser profile.

## Durable Send migration

Current CDPA durability is spread across the hop/request ledger and `DurableSendBlock`. The migration must preserve those records as the control-plane projection while making the core request record the browser-execution source of truth.

Suggested mapping:

| CDPA concept | Core concept |
|---|---|
| hop request ID | `request_id` |
| pre-send durable block | local request + baseline + `PREPARING` |
| accepted Send receipt | `FRONTEND_ACCEPTED` identity |
| exact accepted user node | `USER_IDENTITY_BOUND` |
| durable browser handoff | `DURABLE_HANDOFF` / `RUNNING` |
| uncertain accepted outcome | `UNKNOWN` + `get_required` |
| final exact response | `COMPLETE` returned by `get` |
| terminal external defect | `external_failure` |
| invariant/schema/identity defect | `invariant_failure` |
| conversation owner wait expiry | `ownership_timeout` with no local turn |

Projection is state-aware. `UNKNOWN` always exposes `get_required`; the nested failure remains the cause for CDPA error text and retry/repair policy. A terminal pre-click `FAILED` timeout is `external_failure`, while terminal schema, identity, graph, corrupt-state, and local invariant defects are `invariant_failure`. `ownership_timeout` is reserved for the initial foreign-owner wait that expires before the core creates a local request. Duplicate IDs and later ownership drift are invariant defects, not owner-wait timeouts. CDPA must not infer the next action from the nested failure alone and must never turn an `UNKNOWN/get_required` result into a new Send.

During migration, the adapter should project core state into existing CDPA ledger fields rather than create parallel browser-send state transitions. Existing records need a one-way compatibility decoder/migration keyed by the same hop request ID.

## Prompt and attachments

CDPA currently constructs prompts and owns immutable attachment snapshots. Keep that contract:

1. CDPA freezes prompt text and attachment identity before adapter invocation.
2. Adapter passes the exact prompt and hop request ID.
3. Core validates the composer at the irreversible boundary.
4. Attachment support must be added as an explicit core API extension before CDPA routes attachment-bearing hops through it.

The current core API is text-only. Therefore the first migration flag must exclude hops with attachments; they remain on legacy transport until exact attachment ownership and boundary tests exist. The adapter must never silently drop, re-upload, or approximate attachments.

## Operator continuation and steering

Later operator/user turns must remain attached to the same CDPA hop where current CDPA policy says so. The same CDPA hop request ID remains the only durable request identity.

Current CDPA can observe a later operator user node and continue response waiting on the same hop. The current core graph contract intentionally rejects a later non-tool user node, so this cohort is **not yet eligible** for migration to core-owned response retrieval.

Until a bounded, tested same-request continuation rule exists in the core:

- CDPA keeps legacy response waiting for same-hop operator continuations.
- The adapter must not allocate another request ID or call `send` again for the accepted hop.
- `status(<same hop request ID>)` may still project local core state when that hop was already migrated.
- `get(<same hop request ID>)` is used only for cohorts whose exact graph shape is accepted by the current core contract.
- A future migration must prove behavior equivalent to current CDPA latest-user continuation before enabling this cohort.

For a new CDPA hop that is independently authorized to Send, normal steering rules still apply: the core claims the exact conversation, fills only an empty safe composer, waits for the real Send boundary, permits immediate steering only when Send is enabled, and fails closed on manual text, attachments, choice prompts, ownership drift, page replacement, or graph/user drift.

The adapter must not click Stop, Retry, Regenerate, or Continue to manufacture readiness.

## Restart, refresh, and reopen

Worker restart, browser refresh, F5, tab cleanup, or role-tab reopen must never call `send` for an already accepted hop.

```text
same hop request ID
  -> core.status(request_id) for projection
  -> core.get(request_id) for active recovery/result
  -> never resend
```

If the persisted helper target is gone, `get` locates one unique exact conversation page or opens the exact persisted URL. Duplicate exact candidates fail closed. The adapter must not select the latest tab, first tab, title match, approximate URL, or page order.

## Stop control

Map the CDPA Stop control to `cancel(request_id)` only after CDPA proves the operator selected the exact active hop.

- Pre-click cancellation may complete locally.
- Post-click cancellation requires exact conversation ownership and branch proof.
- An owner mismatch during post-click cancellation is `cancellation_unproven`; it is not `ownership_timeout` because the durable hop already exists.
- `cancellation_unproven` must remain visible in CDPA; it is not equivalent to stopped.
- A Stop command must not authorize a new Send or detach the hop.

## Dashboard projection

Use `status(request_id)` for local, non-browser projection. CDPA combines that result with its own task/team/role/worker state.

Suggested projection:

```text
core state/disposition
+ CDPA task/hop route state
+ CDPA exact role/tab ownership
+ CDPA command/operator state
= dashboard view
```

The dashboard must not call browser transport directly. `status` does not prove a final response body; the worker uses `get` when result retrieval is required.

## Feature-flagged migration

A safe rollout needs mutually exclusive transport authority:

1. **Shadow status only**: create/read core-compatible local records and compare projection; no core Send.
2. **Fresh text-only canary**: selected teams/hops use core Send/get; legacy transport is disabled for those conversations before the first core claim.
3. **Existing-conversation text-only**: enable default owner waiting, exact page reuse/open, and steering wait path.
4. **Restart/recovery**: worker restart and F5 use same-ID `get`; prove no resend.
5. **Operator continuation/cancel**: route approved later turns and Stop through the core contract.
6. **Attachment extension**: only after an exact immutable attachment API and acceptance suite exist.
7. **Default core transport**: legacy path remains available only for explicitly excluded unsupported cases.
8. **Retirement**: delete legacy primitives after the criteria below are met.

At every phase, one conversation must have exactly one authorized Send transport. A feature flag must never permit legacy CDPA transport and the core to send on the same conversation concurrently.

## Legacy deletion criteria

Retire CDPA browser primitives only after production acceptance proves all of the following for the migrated scope:

- hop request ID is the sole idempotency identity;
- fresh and existing sends produce one accepted user node;
- foreign-owner waiting creates no local turn before claim;
- worker restart/F5/reopen uses same-ID `get` and never resends;
- exact page recovery rejects unrelated and duplicate tabs;
- operator continuation preserves composer/manual-input policy;
- Stop maps to honest cancellation proof;
- dashboard projection is complete from CDPA state plus core `status`;
- one shared absolute coordination root/deployment ID is enforced for CDP 9222;
- secret scans and forbidden-operation checks remain green;
- attachment-bearing hops have either migrated with exact proof or remain explicitly excluded.

Then remove, in order:

1. duplicate CDPA tab lookup/open/close routines for migrated hops;
2. duplicate composer readiness/Send-click logic;
3. duplicate accepted-Send parsing and exact user binding;
4. duplicate response-wait/reconciliation paths;
5. obsolete `DurableSendBlock` browser-execution fields after ledger migration is complete;
6. compatibility flags and projection shims after all retained records are migrated or terminal.

CDPA task orchestration, role ownership, manifests, worker scheduling, controls, and dashboard projection remain; they are not replaced by this core.
