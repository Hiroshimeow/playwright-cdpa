# Agent integration guide

## Integration rule

Treat `request_id` as the agent job's exact durable browser-execution identity. Generate it once at the application boundary and persist it with the job before calling the SDK.

```text
agent job ID / operation ID
        ==
playwright-api request_id
```

Do not create a second hidden retry ID inside an adapter.

## Minimal async adapter

```python
from pathlib import Path

from playwright_api import ChatGPTClient, ChatTarget, ClientConfig, Result


class ChatGPTExecutor:
    def __init__(self) -> None:
        self.client = ChatGPTClient(
            ClientConfig(
                state_dir=Path("/var/lib/my-agent/chatgpt-state"),
                coordination_dir=Path("/var/lib/shared/chatgpt-coordination"),
                deployment_id="profile-9222",
            )
        )

    async def execute(
        self,
        *,
        operation_id: str,
        prompt: str,
        target: ChatTarget,
    ) -> Result:
        result = await self.client.send(
            prompt,
            request_id=operation_id,
            target=target,
        )
        if result.disposition == "get_required":
            result = await self.client.get(operation_id)
        return result
```

The adapter delegates; it must not add browser logic, duplicate ownership, a retry daemon, or another request state machine.

## Restart recovery

Persist the application job ID and SDK request ID before invocation. After process restart:

```python
local = client.status(request_id)
if local.disposition == "get_required":
    result = await client.get(request_id)
else:
    result = local
```

Do not call `send` again when a durable record exists. Duplicate request IDs intentionally fail closed.

## Target selection

Construct the target before invoking the SDK:

```python
ChatTarget.fresh()
ChatTarget.conversation(conversation_id)
ChatTarget.project(project_id)
ChatTarget.project_conversation(project_id, conversation_id)
```

Do not pass arbitrary URLs through the agent. Parse and validate untrusted target text with `ChatTarget.parse` at the application boundary.

## Stable project-per-task pattern

A task-oriented agent can map one stable task key to one ChatGPT project:

```python
project = await client.ensure_project(
    key=task_id,
    name=f"task-{task_id}",
)
project_root = await client.open_project(project)
```

Use the returned `ProjectRef.project_id`; never rediscover by card position or approximate name. If exact matching becomes ambiguous, surface the conflict to the operator instead of selecting one project.

For each new chat inside the project, allocate a new logical request ID and use the project-root target. Once a result identity contains the accepted conversation ID, later turns use `ChatTarget.project_conversation(project_id, conversation_id)`.

## Attachments

Snapshot every file immediately before scheduling the operation:

```python
attachments = tuple(AttachmentInput.from_path(path) for path in input_paths)
```

Store the application-level file identity separately if needed. Do not copy file content into SDK state, logs, or prompts solely for retry. If a file changes after snapshot, create a new logical operation rather than mutating an existing request.

After any attachment-related `get_required`, preserve the same state directory and call `get`; never call `send` to re-upload.

## Result handling

Recommended decision table:

```python
if result.success:
    consume(result.response)
elif result.disposition == "get_required":
    schedule_same_id_get(result.request_id)
elif result.disposition == "ownership_timeout":
    wait_for_foreign_owner_release()
elif result.disposition == "invalid_input":
    reject_job(result.failure)
elif result.disposition == "external_failure":
    retry_only_if_policy_allows_and_no_durable_send_exists(result.failure)
elif result.disposition == "invariant_failure":
    halt_and_repair(result.failure)
elif result.disposition == "cancellation_unproven":
    mark_outcome_unresolved(result.failure)
```

Never derive resend permission from `failure.retryable` alone. `UNKNOWN/get_required` remains same-ID recovery regardless of the nested cause.

## Concurrency

All agents/apps using the same browser profile must share:

```python
coordination_dir=<same absolute directory>
deployment_id=<same stable value>
```

Processes that may recover the same request must also share `state_dir`. A distinct state directory with the same request ID is a distinct ledger and cannot recover the original operation.

## Operator interaction

Manual text, manual attachments, choice prompts, page replacement, or duplicate exact tabs are ownership conflicts, not opportunities to clear/overwrite the UI. Preserve the page and surface the diagnostic.

The SDK never clicks Stop to make Send available. Cancellation is explicit through `cancel(request_id)`.

## Version pinning

Agents should pin a wheel version or exact Git commit. Do not depend on an editable checkout or an unpinned branch in production/local automation.
