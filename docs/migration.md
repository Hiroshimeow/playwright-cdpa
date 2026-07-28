# Migration from the unreleased old name

The unreleased package/distribution names `playwright_gpt_core` / `playwright-gpt-core` were replaced by:

```text
distribution: playwright-api
import:       playwright_api
client:       ChatGPTClient
config:       ClientConfig
CLI:          playwright-api
```

No old-package import shim is included because no concrete external caller was found in the repository. This avoids two public package identities and duplicate deprecation machinery before the first release.

## Import changes

Before:

```python
from playwright_gpt_core import ChatGPTCore, CoreConfig
```

After:

```python
from playwright_api import ChatGPTClient, ClientConfig
```

## Send API changes

Before:

```python
await client.send(prompt, fresh=True, request_id=request_id)
await client.send(prompt, conversation=conversation_id, request_id=request_id)
```

After:

```python
from playwright_api import ChatTarget

await client.send(
    prompt,
    request_id=request_id,
    target=ChatTarget.fresh(),
)
await client.send(
    prompt,
    request_id=request_id,
    target=ChatTarget.conversation(conversation_id),
)
```

Project targets are now first-class:

```python
ChatTarget.project(project_id)
ChatTarget.project_conversation(project_id, conversation_id)
```

## CLI changes

Removed:

```text
--fresh
--conversation
```

Use one exact target:

```bash
playwright-api send "prompt" --target /
playwright-api send "prompt" --target /c/<conversation-id>
playwright-api send "prompt" --target /g/g-p-<project-id>/project
```

Attachments use repeated `--attach PATH` arguments.

## State locations

The installed SDK no longer defaults to a repository-relative state directory. Defaults are per-user:

```text
Linux:   ${XDG_STATE_HOME:-~/.local/state}/playwright-api
Windows: %LOCALAPPDATA%\playwright-api
```

Existing callers that need to recover an old durable request must explicitly point `ClientConfig.state_dir` at the old request state root. Durable turn schemas 2, 3, and 4 are decoded and migrated in memory to schema 5. State files are not copied automatically between directories.

Every process using the same Chromium profile must also set one shared absolute `coordination_dir` and stable `deployment_id`.

## Locking

Unconditional POSIX `fcntl` imports were removed. Cross-process locking now uses `portalocker`; package import does not fail solely because `fcntl` is unavailable.

Linux behavior remains covered by the regression suite. Native Windows runtime behavior must be verified on an actual Windows host before claiming full Windows acceptance.

## Projects and attachments

These are new SDK capabilities, not compatibility wrappers:

- `ProjectRef`, `ProjectMemoryScope`, `find_project`, `ensure_project`, and `open_project`;
- `AttachmentInput` snapshots with durable identity and real frontend upload;
- typed project-conversation target parsing and recovery;
- unknown-create/upload/Send boundaries that prohibit blind duplicate mutation.

## Removal criteria for any future compatibility shim

A compatibility shim should be added only when a concrete released caller cannot migrate atomically. It must have a named owner, observed caller, warning, and deletion date/criterion. No speculative shim is present in version 0.1.0.
