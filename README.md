# playwright-gpt-core

`playwright-gpt-core` is a fail-closed Python execution core for ChatGPT Web in an existing persistent Chromium exposed through loopback CDP.

It uses the real ChatGPT composer and real Send/Stop controls, observes the unmodified frontend request/response, persists an irreversible Send boundary, resolves the exact conversation graph turn, and returns success only when the final assistant response is proven to belong to that request.

## Safety contract

- Connects only to a loopback CDP endpoint by default: `http://127.0.0.1:9222`.
- Never calls `browser.close()`.
- Never intercepts, aborts, fulfills, mutates, or replays the conversation POST.
- Persists `CLICK_BOUNDARY_ENTERED` before clicking the real Send control.
- Never sends again after an uncertain click. Recovery uses the same request ID with `get`.
- Existing-conversation `send` waits boundedly for the deployment-wide owner, atomically claims the conversation, and only then creates repository-local request state.
- A foreign durable owner is never stolen or guessed stale.
- Manual composer text, attachments, choice prompts, page replacement, duplicate exact tabs, ownership drift, cancellation drift, and user-graph drift fail closed before Send; the exact URL is rechecked in the same browser callback as the real click.
- Send steering never clicks Stop. When a response is active, the prompt is filled once; the core sends only when the real Send button becomes enabled.
- `get` never sends. It reuses one uniquely proven exact conversation page or opens the exact persisted conversation URL. Unrelated and duplicate tabs are never selected.
- Helper-tab lifecycle is internal. Only a page created or durably owned by this core is eligible for automatic close; a pre-click manual text, attachment, or choice-prompt failure is durably preserved instead. Borrowed Send/get/cancel pages, unrelated tabs, and Chromium remain open.
- Prompt bodies, response bodies, cookies, authorization material, access/resume/proof tokens, Sentinel/Turnstile values, and raw network payloads are not persisted.

## Install

```bash
uv sync --all-groups
```

Python 3.10 or newer is required. Cross-process locking currently requires POSIX `fcntl.flock`.

## Personal CLI flow

Fresh conversation:

```bash
uv run playwright-gpt send "Reply with exactly OK" \
  --fresh \
  --request-id personal-001
```

Continue an exact conversation. Waiting for a foreign core owner is the default:

```bash
uv run playwright-gpt send "Continue" \
  --conversation '<conversation-id-or-https://chatgpt.com/c/...>' \
  --request-id personal-002
```

Retrieve or wait for the exact final response without sending:

```bash
uv run playwright-gpt get personal-002
```

Read local metadata only; this does not connect to Chromium or change coordination:

```bash
uv run playwright-gpt status personal-002 --json
```

Request cancellation:

```bash
uv run playwright-gpt cancel personal-002
```

The complete command surface is `send`, `get`, `status`, and `cancel`. No retired command or library aliases are retained. There is no public `--wait-idle` or helper-tab retention option.

Caller-supplied request IDs are exact durable idempotency identities. They must contain 1-160 ASCII letters, digits, dot, underscore, or hyphen. Only `None` asks the Python API to generate a UUID; an empty or malformed explicit ID is invalid input.

Shared CLI options:

```text
--cdp-endpoint
--state-dir
--coordination-dir
--deployment-id
--timeout
--poll
--send-timeout
--identity-timeout
--json
```

## Agent/application API flow

The Python API is authoritative. CLI JSON is a thin adapter over the same methods and result model.

```python
import asyncio
from pathlib import Path

from playwright_gpt_core import ChatGPTCore, CoreConfig


async def main() -> None:
    core = ChatGPTCore(
        CoreConfig(
            cdp_endpoint="http://127.0.0.1:9222",
            state_dir=Path(".playwright-gpt"),
            coordination_dir=Path("/var/tmp/playwright-gpt-coordination"),
            deployment_id="shared-cdp-9222",
            timeout=300,
            poll=0.5,
        )
    )

    sent = await core.send(
        "Reply with exactly OK",
        fresh=True,
        request_id="caller-owned-id-001",
    )
    if sent.disposition == "get_required":
        sent = await core.get(sent.request_id)
    if not sent.success:
        raise RuntimeError(sent.failure)
    print(sent.response)

    metadata = core.status(sent.request_id)
    print(metadata.state, metadata.disposition)


asyncio.run(main())
```

Primary methods:

- `await send(prompt, fresh=True, request_id=...)`
- `await send(prompt, conversation=..., request_id=...)`
- `await get(request_id)`
- `status(request_id)`
- `await cancel(request_id)`

No retired command or library aliases are retained. Callers use `get` for active result retrieval and `send` for submission.

## Result dispositions

| Disposition | Meaning |
|---|---|
| `complete` | Exact final response returned. |
| `invalid_input` | Caller input failed the public boundary; no browser or coordination mutation occurred. |
| `get_required` | Same-ID `get` may wait/recover; never resend. |
| `external_failure` | Browser, authentication, network, backend, or frontend external failure. |
| `invariant_failure` | Schema, corrupt state, identity, graph, or local invariant failure. |
| `ownership_timeout` | Foreign durable owner remained through the bounded wait; no local turn was created. |
| `cancelled` | Cancellation was positively represented. |
| `cancellation_unproven` | Cancellation was requested but could not be proven. |

`UNKNOWN` always exposes `get_required`: the caller reuses the same request ID and never sends again. The nested failure remains the cause and determines whether the caller can retry immediately, must repair an invariant first, or observed an external outage. A terminal pre-click `FAILED` timeout is instead `external_failure`; same-ID `get` cannot recover a request that never crossed Send.

Schema, identity, graph, corrupt-state, and local invariant failures are `invariant_failure` when terminal. If one occurs on an `UNKNOWN` request, disposition remains `get_required`, but exit 20 signals that code/state repair is required before retrying same-ID `get`.

`ownership_timeout` is reserved for the initial bounded foreign-owner wait where no local request record was created. Duplicate request IDs and ownership conflicts after local state exists are `invariant_failure`. An owner mismatch during post-click cancellation is `cancellation_unproven`, because Stop was not performed or proven.

### Exit codes

| Code | Meaning |
|---:|---|
| 0 | Exact success, successful `get`, or cancel of an already complete request. |
| 2 | Invalid input or configuration. |
| 10 | Recoverable external cause. `UNKNOWN` JSON still reports `get_required`; terminal results report `external_failure`. |
| 11 | Terminal external cause. |
| 12 | Active or uncertain result requiring same-ID `get`, including UNKNOWN timeout/ambiguous outcome. |
| 20 | Schema, identity, graph, corrupt-state, or local invariant failure; UNKNOWN retains `get_required` after repair. |
| 21 | Initial foreign-owner wait timed out before any local request record was created. |
| 22 | Cancellation positively represented. |
| 23 | Cancellation requested but unproven. |
| 130 | Operator interruption. |

JSON mode writes one result object to stdout. Diagnostics are not mixed into stdout. Identity values are truncated in public output.

## Persistence and coordination

Repository-local request/result state defaults to:

```text
.playwright-gpt/
├── turns/<request-id>.json
├── conversations/*.json
└── locks/*.lock
```

Deployment-wide browser ownership is separate:

```text
<coordination-base>/<deployment-id>/
├── conversations/<sha256-of-conversation-id>.json
└── locks/*.lock
```

Every process allowed to mutate the same CDP deployment must use one shared absolute coordination base and deployment ID. Different repositories may use different `state_dir` values, but they must not use different coordination namespaces for the same browser profile.

For idempotent recovery, the same logical request must use the same result state root. Shared coordination serializes conversation mutation, but coordination is not a cross-repository result ledger; reusing one caller request ID under another `state_dir` does not recover the original local request record.

State uses strict schemas, atomic replacement, file/directory `fsync`, revisions, and POSIX locks. Corrupt bytes are preserved and rejected before browser or ownership mutation. The coordination plane stores only conversation ownership metadata; it does not contain repository paths, prompts, responses, or browser credentials.

## Documentation

- [Architecture and reliability contract](docs/architecture.md)
- [Live facts and remaining assumptions](docs/live-facts-and-assumptions.md)
- [Live acceptance evidence](docs/live-acceptance.md)
- [Future CDPA V2 adapter design](docs/cdpa-adapter.md)
