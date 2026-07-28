# CDPA adapter contract

This task does not edit CDPA source. This document defines the smallest adapter boundary for consuming `playwright-api` from CDPA.

## Dependency pin

Install the SDK as a normal dependency, not by importing a sibling source tree:

```toml
[project]
dependencies = [
  "playwright-api @ git+<repository-url>@<reviewed-commit-sha>",
]
```

A released wheel pin is preferred when available:

```toml
dependencies = ["playwright-api==0.1.0"]
```

CDPA must verify the exact installed version/commit at startup or in deployment evidence. Do not use editable installs for the worker runtime.

## Authority split

CDPA retains:

- task/team/role routing, dependencies, queueing, Maintainers, and dashboard state;
- prompt construction and task-level attachment selection;
- operator authorization and physical-role/tab policy;
- hop lifecycle and business-level retry policy.

`playwright-api` owns:

- exact target parsing/navigation;
- deployment-wide conversation mutation ownership;
- real composer/file/project frontend mutation;
- irreversible attachment and Send boundaries;
- exact accepted-turn identity and response recovery;
- owned-helper cleanup.

The adapter must not duplicate these SDK browser mechanisms or introduce another worker/orchestrator.

## Identity mapping

Use one identity:

```text
CDPA durable hop request ID == playwright-api request_id
```

The adapter must not generate another SDK request ID. Worker restart, operator Resume, F5, role-tab reopen, and recovery reuse the same CDPA hop request ID.

## Configuration

All CDPA worker processes mutating CDP 9222 must share one coordination namespace:

```python
ClientConfig(
    cdp_endpoint="http://127.0.0.1:9222",
    state_dir=<one durable CDPA-managed SDK state root>,
    coordination_dir=<one absolute shared coordination root>,
    deployment_id="cdpa-cdp-9222",
)
```

`state_dir` must remain stable across worker restarts and every process that can recover the same hop. `coordination_dir` and `deployment_id` must be identical for all apps/processes using the same browser profile.

## Minimal adapter

```python
from playwright_api import (
    AttachmentInput,
    ChatGPTClient,
    ChatTarget,
    ClientConfig,
    Result,
)


class CDPAChatGPTAdapter:
    def __init__(self, config: ClientConfig) -> None:
        self.client = ChatGPTClient(config)

    async def send_hop(
        self,
        *,
        hop_request_id: str,
        prompt: str,
        target: ChatTarget,
        attachment_paths: tuple[str, ...] = (),
    ) -> Result:
        attachments = tuple(
            AttachmentInput.from_path(path) for path in attachment_paths
        )
        return await self.client.send(
            prompt,
            request_id=hop_request_id,
            target=target,
            attachments=attachments,
        )

    async def recover_hop(self, hop_request_id: str) -> Result:
        return await self.client.get(hop_request_id)

    def project_hop(self, hop_request_id: str) -> Result:
        return self.client.status(hop_request_id)

    async def cancel_hop(self, hop_request_id: str) -> Result:
        return await self.client.cancel(hop_request_id)
```

## Project-per-task mapping

For the intended CDPA pattern:

```python
project = await client.ensure_project(
    key=task_id,
    name=task_name,
)
target = await client.open_project(project)
```

Persist `ProjectRef.project_id` in the CDPA task projection if useful, but treat the SDK project registry as the browser-execution identity source. Never select by card order, creation time, fuzzy task name, or latest project.

A fresh project conversation uses the project-root target. Later hops in that exact conversation use:

```python
ChatTarget.project_conversation(project.project_id, conversation_id)
```

## Attachments

CDPA chooses the files; the SDK performs immutable snapshot validation and real frontend upload.

Rules:

1. Freeze the CDPA hop request ID and attachment path list before adapter invocation.
2. Let `AttachmentInput.from_path` compute size/hash/media identity.
3. Do not mutate files after snapshot.
4. Do not silently drop unsupported files.
5. After `UNKNOWN/get_required`, call `get` with the same hop request ID. Never invoke `send_hop` again to re-upload.

Project knowledge-file management remains outside scope.

## State projection

Map SDK results without inventing retry semantics:

| SDK result | CDPA action |
|---|---|
| `complete` | Store exact response and continue routing. |
| `get_required` | Keep the same hop/request ID and invoke `get`. Never resend. |
| `ownership_timeout` | Wait/retry acquisition only; no local SDK request was created. |
| `invalid_input` | Fail the hop as a caller/config defect. |
| `external_failure` | Apply bounded environmental policy only when the durable state permits it. |
| `invariant_failure` | Route repair/review; do not blind retry. |
| `cancelled` | Mark stopped/cancelled only after positive proof. |
| `cancellation_unproven` | Keep unresolved/blocked; do not claim Stop succeeded. |

The nested failure includes stable code/category/retryable/external fields. CDPA must not infer resend permission from `retryable=True` when disposition is `get_required`.

## Operator continuation

A later operator/user turn that current CDPA treats as continuation remains on the same CDPA hop request ID. Until the SDK has an independently accepted same-request continuation contract equivalent to CDPA's current behavior, CDPA keeps legacy response waiting for that cohort. The adapter must not allocate another request ID or issue a second Send for the accepted hop.

## Resume and restart

Resume/restart logic is:

```text
existing hop request ID
  -> status(request_id) for local projection
  -> get(request_id) for exact recovery/result
  -> never send again
```

Only a genuinely new CDPA hop with a new durable request ID may call `send`.

## Mutually exclusive transport authority

During migration, each hop/conversation must have exactly one Send implementation. A feature flag must not permit both legacy CDPA browser code and `playwright-api` to mutate the same composer.

Recommended rollout:

1. Pin/install SDK and validate local `status` projection.
2. Canary fresh text-only hops.
3. Enable existing ordinary conversations.
4. Enable project-per-task creation/reuse.
5. Enable attachment-bearing hops.
6. Enable restart/Resume recovery through same-ID `get`.
7. Remove duplicate legacy page lookup, composer, upload, Send, acceptance, and response-monitoring code for migrated cohorts.

## Deletion criteria for legacy browser primitives

Delete duplicate CDPA browser execution only after independent evidence proves:

- exact hop request ID reuse across restart/Resume;
- one accepted Send and no resend after uncertainty;
- exact project and project-conversation recovery;
- attachment upload and changed-file rejection;
- shared cross-process coordination for CDP 9222;
- borrowed/unrelated pages and Chromium remain open;
- dashboard projection preserves all required operational diagnostics;
- wheel/revision pin is reproducible in the worker environment.
