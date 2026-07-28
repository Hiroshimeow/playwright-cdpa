# Public API reference

All supported caller imports come from `playwright_api`. Internal modules are implementation details.

## Public exports

```python
from playwright_api import (
    AttachmentInput,
    ChatGPTClient,
    ChatTarget,
    ClientConfig,
    Disposition,
    Failure,
    FailureCategory,
    FailureCode,
    ProjectMemoryScope,
    ProjectRef,
    Result,
    SyncChatGPTClient,
    TargetKind,
    TurnIdentity,
    TurnState,
)
```

The package version comes from installed distribution metadata and is available as `playwright_api.__version__`. There is no second version constant.

## `ClientConfig`

```python
ClientConfig(
    cdp_endpoint="http://127.0.0.1:9222",
    state_dir=<per-user default>,
    coordination_dir=<per-user default>,
    deployment_id=None,
    timeout=1800.0,
    poll=1.0,
    send_timeout=90.0,
    identity_timeout=30.0,
    stable_samples=2,
    stable_seconds=0.5,
)
```

Rules:

- CDP must be HTTP/HTTPS loopback with no credentials, query, fragment, or path.
- `coordination_dir`, when explicit, must be absolute.
- `deployment_id` accepts 1-80 ASCII letters, digits, dot, underscore, or hyphen.
- When `deployment_id` is omitted, a deterministic value is derived from the normalized loopback endpoint.
- Call `.validated()` only when a validated immutable copy is needed directly. Clients validate automatically.

## `ChatTarget`

Constructors:

```python
ChatTarget.fresh()
ChatTarget.conversation("conversation-id")
ChatTarget.project("g-p-project-id")
ChatTarget.project_conversation("g-p-project-id", "conversation-id")
ChatTarget.parse("https://chatgpt.com/c/conversation-id")
```

Properties preserve project and conversation identity separately:

```python
target.kind
target.project_id
target.conversation_id
target.canonical_url
target.coordination_key
```

## `AttachmentInput`

Create an immutable snapshot before `send`:

```python
attachment = AttachmentInput.from_path("./report.pdf")
```

Fields:

```python
attachment.path       # canonical local Path
attachment.size       # bytes
attachment.sha256     # lowercase SHA-256
attachment.media_type # canonical supported media type
```

Supported extensions are `.txt`, `.md`, `.csv`, `.json`, `.pdf`, `.png`, `.jpg`, `.jpeg`, and `.webp`.

`to_public_dict()` omits the local path. The SDK revalidates size/hash before browser mutation and again at the Send boundary.

## `ChatGPTClient`

### `send`

```python
result = await client.send(
    prompt,
    request_id="caller-owned-id",
    target=ChatTarget.fresh(),
    attachments=(attachment,),
)
```

Signature:

```python
async def send(
    prompt: str,
    *,
    request_id: str | None = None,
    target: ChatTarget | None = None,
    attachments: Sequence[AttachmentInput] = (),
) -> Result
```

Defaults:

- `request_id=None`: generate one UUID for this new logical request.
- `target=None`: fresh root chat.
- `attachments=()`: text-only Send.

A caller-supplied request ID is an exact idempotency identity and must not be reused for a second Send. Duplicate IDs fail closed.

### `get`

```python
result = await client.get(request_id)
```

Attaches to durable state and returns/waits for the exact result. It never sends or uploads. `get` is the only continuation after `UNKNOWN/get_required`.

### `status`

```python
result = client.status(request_id)
```

Reads local durable metadata only. It does not connect to Chromium and does not prove a final response body unless that body is already returned in the current process result.

### `cancel`

```python
result = await client.cancel(request_id)
```

Requests cancellation for the exact durable turn. Post-click cancellation requires exact ownership and branch proof. `cancellation_unproven` is intentionally distinct from `cancelled`.

### Projects

```python
project = await client.find_project(name="Exact Name")
project = await client.find_project(project_id="g-p-project-id")

project = await client.ensure_project(
    key="stable-caller-key",
    name="Exact Name",
    memory_scope=ProjectMemoryScope.DEFAULT,
)

target = await client.open_project(project)
```

`find_project` returns `None`, one exact project, or fails closed on duplicate exact matches. `ensure_project` durably prevents blind duplicate Create after an uncertain frontend outcome.

## `SyncChatGPTClient`

The sync facade mirrors the async methods:

```python
client = SyncChatGPTClient(config)
client.send(...)
client.get(request_id)
client.status(request_id)
client.cancel(request_id)
client.find_project(...)
client.ensure_project(...)
client.open_project(project)
```

It contains no transport/state implementation and must not be used from a running event loop.

## `Result`

Fields:

```python
result.request_id
result.state       # TurnState
result.response    # exact response or None
result.identity    # TurnIdentity or None
result.failure     # Failure or None
result.success
result.disposition
result.to_dict()   # sanitized external projection
```

`TurnIdentity.safe_summary()` truncates identity values for public projection. Prompt and response bodies are caller results, not diagnostics.

## Errors

Public failures provide:

```python
failure.code         # FailureCode
failure.category     # FailureCategory
failure.disposition  # Disposition
failure.message      # sanitized diagnostic
failure.retryable
failure.external
```

Categories distinguish invalid input, ownership conflict/timeout, frontend or schema drift, network/rate-limit/backend failures, ambiguous outcomes, cancellation-unproven, corrupt state, and invariant defects.

## No `watch`

There is no public `watch` method. Durable retrieval is intentionally `status` for local projection plus `get` for exact active recovery. The SDK does not add a daemon, scheduler, or second monitoring state machine.
