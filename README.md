# playwright-api

`playwright-api` is an installable Python SDK for exact, fail-closed ChatGPT Web execution through an existing Chromium exposed on a loopback CDP endpoint.

The Python API is authoritative. The `playwright-api` command is a thin debug adapter. The package is not an HTTP service, MCP server, daemon, scheduler, browser-profile manager, or multi-site framework.

## Runtime status

- Linux: supported and live-tested.
- Windows: supported by the platform-specific atomic state-write path. The earlier `local invariant failure: PermissionError` caused by POSIX directory `fsync` is fixed on this branch.
- Python: 3.10 or newer.
- Browser: an already authenticated Chromium exposed through a loopback CDP endpoint, normally `http://127.0.0.1:9222`.

Native Windows import and persistence behavior is regression-tested. Run the Windows smoke command below after pulling the latest branch; full native Windows browser acceptance remains environment-dependent.

## Core guarantees

- Uses the real ChatGPT frontend composer, file input, project flow, and Send control.
- Never calls `browser.close()` and never closes unrelated or borrowed pages.
- Never intercepts, aborts, fulfills, mutates, or replays private conversation/upload APIs.
- Persists attachment mutation and Send click boundaries before irreversible browser actions.
- Treats each `request_id` as the exact durable idempotency identity.
- Never sends or uploads again after an uncertain boundary; recovery uses `get(request_id)`.
- Supports fresh chats, ordinary conversations, project roots, and project conversations through one typed target model.
- Uses one shared cross-process coordination namespace for every process that can mutate the same Chromium profile.
- Persists prompt/response hashes and lengths, not prompt or response bodies. Public projections omit local attachment paths.

## Install

Python 3.10 or newer is required.

Local checkout, non-editable:

```bash
uv add /absolute/path/to/playwright-cdpa
# or
python -m pip install /absolute/path/to/playwright-cdpa
```

Built wheel:

```bash
uv build
python -m pip install dist/playwright_api-0.1.0-py3-none-any.whl
```

Pinned Git revision:

```bash
uv add "playwright-api @ git+<repository-url>@<commit-sha>"
```

The Chromium instance must already be authenticated and exposed on a loopback CDP endpoint, normally `http://127.0.0.1:9222`.

### Source-checkout quick start

Linux/macOS shell:

```bash
git switch feat/playwright-api-sdk
git pull
uv sync --all-groups
uv run playwright-api send "Reply with exactly OK" \
  --target / \
  --request-id test-001 \
  --json
```

Windows Command Prompt or PowerShell:

```text
git switch feat/playwright-api-sdk
git pull
uv sync --all-groups
uv run playwright-api send "Reply with exactly OK" --target / --request-id test-win-001 --json
```

The executable is `playwright-api`, not the retired `playwright-gpt`. The old `--fresh` option was removed; use `--target /` for a fresh root chat.

If an older checkout returns `local invariant failure: PermissionError` on Windows, pull the latest `feat/playwright-api-sdk` branch and run `uv sync --all-groups` again. Changing `--state-dir` is not the fix for that old build.

## Thin three-agent flow

`main.py` runs one minimal loop: `PLAN -> REVIEW -> DEV -> PLAN`. Only PLAN may return `DONE`.

Continue the ChatGPT conversation used for manual brainstorming:

```text
uv run main.py --task "Implement the agreed goal" --url-id <conversation-id>
```

Start PLAN in a new chat:

```text
uv run main.py --task "Implement the agreed goal"
```

Create or reuse a ChatGPT Project named `abc` for new role conversations:

```text
uv run main.py --task "Implement the agreed goal" --project abc
```

The only workflow flags are `--task`, `--url-id`, and `--project`. Every role writes its report under `.plan/three-agent/` in the repository it works on and returns the exact path to the next role. Role sends are separated by two seconds; Project creation pauses one second after each UI action.

## Async usage

```python
import asyncio
from pathlib import Path

from playwright_api import (
    AttachmentInput,
    ChatGPTClient,
    ChatTarget,
    ClientConfig,
)


async def main() -> None:
    client = ChatGPTClient(
        ClientConfig(
            cdp_endpoint="http://127.0.0.1:9222",
            state_dir=Path("/var/lib/my-agent/playwright-api-state"),
            coordination_dir=Path("/var/lib/shared/playwright-api-coordination"),
            deployment_id="chromium-profile-9222",
        )
    )

    result = await client.send(
        "Read the attachment and reply with exactly ATTACHMENT_OK.",
        request_id="agent-job-20260729-001",
        target=ChatTarget.fresh(),
        attachments=(AttachmentInput.from_path("./evidence.txt"),),
    )

    if result.disposition == "get_required":
        result = await client.get(result.request_id)
    if not result.success:
        raise RuntimeError(result.failure)

    print(result.response)


asyncio.run(main())
```

Never call `send` again for the same logical request after `get_required`. Reuse the same `request_id` with `get`. Caller-supplied IDs accept 1-160 ASCII letters, digits, dot, underscore, or hyphen.

## Projects

```python
project = await client.ensure_project(
    key="cdpa-task-id",
    name="Disposable SDK Project",
)
project_target = await client.open_project(project)

result = await client.send(
    "Reply with exactly PROJECT_OK.",
    request_id="cdpa-task-id:project-chat:1",
    target=project_target,
)
```

`ensure_project` is idempotent by caller key and exact project identity:

- zero exact matches: persist an unknown-create marker, then create once;
- one exact match: reuse it;
- more than one exact match: fail closed;
- uncertain creation: reconcile exact identity and never click Create again blindly;
- concurrent apps sharing the coordination namespace: serialize by key, then reconcile and reuse one identity.

`ProjectMemoryScope.PROJECT_ONLY` is supported. ChatGPT Work mode is unavailable for project-only-memory projects.

## Sync usage

```python
from playwright_api import ChatTarget, SyncChatGPTClient

client = SyncChatGPTClient()
result = client.send(
    "Reply with exactly SYNC_OK.",
    request_id="sync-job-001",
    target=ChatTarget.fresh(),
)
if result.disposition == "get_required":
    result = client.get(result.request_id)
```

`SyncChatGPTClient` contains no browser or state implementation. It delegates to `ChatGPTClient` and raises a clear error when used from an already-running event loop.

## CLI

From a source checkout, prefix commands with `uv run`. After installing the wheel, call `playwright-api` directly.

```bash
uv run playwright-api send "Reply with exactly OK" \
  --target / \
  --request-id cli-001 \
  --json

uv run playwright-api send "Continue" \
  --target /c/<conversation-id> \
  --request-id cli-002 \
  --json

uv run playwright-api send "Read this file" \
  --target / \
  --attach ./evidence.txt \
  --request-id cli-003 \
  --json

uv run playwright-api get cli-003 --json
uv run playwright-api status cli-003 --json
uv run playwright-api cancel cli-003 --json
```

The complete CLI command surface is `send`, `get`, `status`, and `cancel`. `status` reads local state only. There is no `playwright-gpt` executable and no `--fresh` flag.

## Targets

`ChatTarget` accepts exactly:

| Kind | Canonical path |
|---|---|
| Fresh root | `/` |
| Ordinary conversation | `/c/<conversation-id>` |
| Project root / fresh project chat | `/g/g-p-<project-id>/project` |
| Project conversation | `/g/g-p-<project-id>/c/<conversation-id>` |

Credential-bearing, query-bearing, fragment-bearing, malformed, non-HTTPS, non-ChatGPT, and ambiguous targets are rejected before browser mutation. Conversation targets must resolve to one exact HTTPS ChatGPT conversation endpoint.

## State and coordination

Default per-user base locations:

- Linux: `${XDG_STATE_HOME:-~/.local/state}/playwright-api`
- Windows: `%LOCALAPPDATA%\playwright-api`

Request/project state and deployment coordination are separate:

```text
<state-dir>/
├── turns/
├── projects/
├── project-locks/
└── locks/

<coordination-dir>/<deployment-id>/
├── conversations/
├── locks/
├── project-creation-claims/
└── project-creation-locks/
```

Every application/process allowed to mutate the same Chromium profile must use the same absolute `coordination_dir` and `deployment_id`. The same logical request must use the same result state root, so processes recovering it must also use the same `state_dir`. Coordination serializes browser mutation and stores only the Project key/name/scope claim needed to prevent duplicate Create actions; exact `ProjectRef` results remain in each caller's `state_dir`, and coordination is not a cross-repository result ledger or a substitute for durable request state.

## Results and failures

`Result` exposes typed `state`, `identity`, `failure`, `success`, and `disposition`. Stable failure projections include a machine-readable `code`, `category`, `disposition`, `retryable`, and `external` flag.

No retired command or library aliases are retained.

Important dispositions:

| Disposition | Required caller action |
|---|---|
| `complete` | Consume the exact response. |
| `get_required` | Call `get` with the same request ID; never resend. |
| `invalid_input` | Correct input; no browser mutation occurred. |
| `ownership_timeout` | Retry later with a new logical request only if no local request was created. |
| `external_failure` | Repair/wait according to `retryable`. |
| `invariant_failure` | Repair schema/state/identity drift before continuing. |
| `cancelled` | Cancellation was positively proven. |
| `cancellation_unproven` | Treat outcome as unresolved; do not assume stopped. |

`UNKNOWN` always exposes `get_required`; the nested failure remains the cause. A terminal pre-click `FAILED` timeout is an `external_failure`. Terminal schema, identity, graph, corrupt-state, and local invariant defects are `invariant_failure`. `ownership_timeout` is reserved for the initial foreign-owner wait where no local request record was created. ownership conflicts after local state exists are `invariant_failure`, and an owner mismatch during post-click cancellation is `cancellation_unproven`.

## Documentation

- [Public API reference](docs/api-reference.md)
- [Architecture contract](docs/architecture.md)
- [Agent integration guide](docs/agent-integration.md)
- [CDPA adapter contract](docs/cdpa-adapter.md)
- [Migration from the unreleased old name](docs/migration.md)
- [Live acceptance evidence](docs/live-acceptance.md)
- [Proven facts and remaining assumptions](docs/live-facts-and-assumptions.md)
