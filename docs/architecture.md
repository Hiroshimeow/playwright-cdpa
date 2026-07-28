# Architecture and reliability contract

## Public boundary

`ChatGPTClient` is the sole application-facing boundary:

```text
send(prompt, fresh|conversation, request_id?) -> Result
get(request_id)                           -> Result
status(request_id)                        -> Result
cancel(request_id)                        -> Result
```

The Python API is authoritative. `playwright-api` parses arguments, builds `ClientConfig`, calls one method, and renders the same `Result`/exit taxonomy. Browser transport, graph resolution, persistence, ownership, recovery, and page lifecycle do not live in CLI code.

Caller-supplied request IDs must contain 1-160 ASCII letters, digits, dot, underscore, or hyphen. Only `None` generates a UUID. Every public method validates this before state, coordination, or browser access. No retired command or library aliases are retained.

No daemon, loopback HTTP server, MCP server, plugin framework, browser-profile manager, or second orchestrator is part of this core. Those are adapter concerns and must call this API rather than duplicate it.

## State planes

Two state planes have different scope:

1. **Repository-local result state** (`StateStore`): exact request record, prompt digest/length, strict identity fields, baseline graph fingerprints, send provenance, cancellation revision, failure, helper target lifecycle, and result digest/length.
2. **Deployment-wide coordination** (`CoordinationStore`): one exact active request ID per conversation for every process allowed to mutate the same persistent browser profile.

The coordination path is absolute and namespaced by deployment ID. A repository-local path is never stored in coordination. A foreign owner is not loaded from another repository, stolen, expired heuristically, or guessed stale.

Idempotent same-ID recovery is local to one result state root: every process handling the same logical request must use the same `state_dir`. Coordination is not a cross-repository result ledger and does not make a request record visible in another result root.

## Existing-conversation Send

The ownership sequence is intentionally ordered:

1. Validate prompt, exact target, caller request ID, and local duplicate request ID.
2. Poll the shared coordination record to the bounded deadline.
3. For each attempt, take the existing per-conversation file lock, reload the record, and claim only when `active_request_id is None`.
4. Only after the exact claim succeeds, create the local request record.
5. If local creation fails, release only that exact claim.

This removes the previous wait-then-create-then-claim race. Competing processes cannot both create a local turn and cannot both cross Send.

## Exact page ownership

For an existing conversation, the resolver enumerates every open page and normalizes only exact `https://chatgpt.com/c/<conversation-id>` URLs.

- No exact candidate: create one page and navigate to the exact URL.
- One exact candidate: reuse it.
- More than one exact candidate: fail closed.
- Preferred persisted target: accept it only when it is still the exact conversation page.
- Unrelated page: ignore it.

A page created by this core is **owned** and may be closed automatically. A pre-existing exact page is **borrowed** and is never closed by Send/get/cancel. Chromium and unrelated pages are never closed. `browser.close()` is forbidden.

The schema-v4 `helper_page_keep` field remains internal. New requests normally use automatic lifecycle, but a pre-click failure caused by manual text, attachments, or a choice prompt durably preserves the exact owned page across later `get` and terminal `cancel` cleanup. This preservation decision is not caller-controlled. There is no public helper-tab policy option.

## Composer and steering state machine

Before filling:

- exact conversation URL and target must be stable;
- the composer must become present and editable within the bounded hydration window;
- composer text must be empty;
- attachments must be absent;
- blocking choice prompts must be absent.

The prompt is filled once through the real editor. While waiting for Send:

- exact target and URL are rechecked;
- shared owner must remain the same request ID;
- local revision and cancellation state must remain unchanged;
- exact composer text must remain the owned prompt;
- attachments and choice prompts must remain absent;
- the set and fingerprints of user graph nodes must remain unchanged.

Assistant streaming is allowed to advance while waiting; a new or changed user node is not. Immediately before persisting the click boundary, the core snapshots the current full graph so the eventual user node is resolved below the latest proven parent.

If Stop is visible and Send is absent, the core leaves the exact prompt in place and waits. If Send becomes visible and enabled while Stop remains visible, that is a supported immediate steering boundary. If Send appears only after Stop disappears, the same path sends then. Send never clicks Stop, Retry, Regenerate, or Continue.

The final browser-side dispatch rechecks the exact fresh-root or exact conversation URL, one visible editable composer, exact prompt text, zero attachments, and one visible enabled real Send button in the same JavaScript turn, then calls that button's real `click()`.

## Irreversible Send protocol

The durable order is:

```text
NOT_ATTEMPTED
  -> PREPARING + exact identity/baseline
  -> CLICK_BOUNDARY_ENTERED (persisted)
  -> real Send click
  -> FRONTEND_ACCEPTED
  -> exact accepted identity merge
  -> USER_IDENTITY_BOUND
  -> DURABLE_HANDOFF
  -> RUNNING
  -> COMPLETE
```

Any failure after `CLICK_BOUNDARY_ENTERED` is retry-prohibited and represented as `UNKNOWN` when completion is not yet proven. `send` never clicks again. Recovery uses the same request ID with `get`.

The frontend request/response is observed but never intercepted, modified, fulfilled, aborted, or replayed. Frontend acceptance matches only POST responses from the exact HTTPS ChatGPT conversation endpoint `/backend-api/f/conversation`, with no credentials, nondefault port, URL parameters, query, or fragment.

## Active `get`

`get(request_id)` is the result operation, not a metadata read.

- `RUNNING`: attach to the exact identity and wait for the exact final assistant response.
- transient `PREPARING`/`SENT`: briefly wait for the active sender to finish durable identity binding before attempting reconciliation.
- recoverable `UNKNOWN`: bind only from accepted transport identity or the strict persisted baseline/new-user proof; never resend.
- `COMPLETE`: retrieve the exact response from the conversation graph because response bodies are not persisted.
- missing helper: reuse one unique exact page or open the exact persisted conversation URL.
- duplicate exact page: fail closed.

Concurrent `get` observers may race to persist COMPLETE. A revision conflict is accepted only when the winning record is already COMPLETE; both observers return the same exact response.

`status(request_id)` only loads and strictly decodes the local record. Coordination is lazy, so status does not create coordination directories, instantiate `BrowserSession`, navigate, wait, inspect tabs, close helpers, or mutate state.

## Cancellation

Cancellation is separate from Send steering.

- Before the click boundary, cancellation may be positively persisted without browser mutation.
- After the click boundary, cancellation requires exact shared ownership, exact conversation identity, and exact branch proof.
- Cancellation uses the same exact-page resolver as `get`: one unique exact page is borrowed, no exact page causes one owned exact helper to be opened, and duplicate exact pages fail closed.
- Stop may be clicked only by explicit `cancel`; borrowed pages remain open and only an owned cancellation/helper page may be closed.
- Missing proof is `cancellation_unproven`; the core does not pretend the generation stopped.

## Failure and recovery taxonomy

`Result.disposition` is derived from durable state and failure category:

```text
complete
get_required
external_failure
invariant_failure
ownership_timeout
cancelled
cancellation_unproven
```

`get_required` means the caller must reuse the same request ID. It never authorizes another Send.

The classification is state-aware:

- `UNKNOWN` always exposes `get_required`; the nested failure remains the cause used for exit/fix policy.
- terminal pre-click `FAILED` timeout is `external_failure`, not same-ID recovery.
- terminal schema, identity, graph, corrupt-state, and local invariant failures are `invariant_failure`.
- `COMPLETE` with an exact response is `complete`; a retrieval failure is classified from its nested cause.
- `ownership_timeout` is reserved for the initial bounded foreign-owner wait where no local request record exists.
- duplicate request IDs and ownership conflicts after local state exists are `invariant_failure`.
- owner mismatch during post-click cancellation is `cancellation_unproven`.
- cancellation-unproven overrides generic state classification.

Thus exit 10 may accompany `UNKNOWN/get_required` for a recoverable external cause, exit 12 is reserved for active/ambiguous same-ID recovery, exit 20 requires invariant repair before retrying the same ID, exit 21 identifies only no-local-turn owner-wait expiry, and exit 23 identifies cancellation that was requested but not proven.

## Secret boundary

Tokens and browser credentials remain in memory. Persisted/public structures are allowlisted and sanitized. Public identity summaries truncate identifiers. The core does not persist or print cookies, authorization headers, access/resume/proof tokens, Sentinel/Turnstile material, prompt bodies, response bodies, or raw frontend/backend payloads.
