# Architecture contract

## Boundary

`playwright-api` is one in-process SDK around an existing persistent Chromium. It owns exact browser execution and durable request recovery only.

It does not own agent scheduling, task routing, HTTP transport, MCP exposure, authentication/profile management, or application-specific orchestration.

## Components

```text
caller / agent / CDPA adapter
        |
        v
ChatGPTClient
  |-- StateStore             durable request/result records
  |-- ProjectRegistry        caller key -> exact project identity
  |-- CoordinationStore      deployment-wide mutation ownership
  |-- BrowserSession         borrowed loopback CDP connection
  |-- frontend/transport     real UI mutation + accepted identity
  `-- backend/monitor        exact graph response proof
```

There is one request state machine. Projects and attachments add durable fields and boundaries to that state machine; they do not create a second worker or monitor.

## State versus coordination

### Durable state

`state_dir` contains request and project identity needed for restart recovery:

- request ID, target identity, revisions, state, provenance, timestamps;
- prompt hash/length, response hash/length;
- attachment canonical path, size, SHA-256, media type, and stage;
- exact accepted turn identity and graph fingerprints;
- helper-page ownership metadata;
- stable project caller-key mapping and unknown-create marker.

Prompt content, response content, cookies, tokens, authorization headers, and raw backend payloads are not persisted.

Processes recovering the same request must use the same `state_dir`.

### Coordination

`coordination_dir/deployment_id` contains only deployment-wide ownership records and locks. Every process that can mutate the same Chromium profile must share this namespace.

Different applications may use separate request state roots, but they must not use separate coordination namespaces for the same profile. Coordination prevents concurrent mutation; it cannot recover another application's request result.

Cross-process locks use `portalocker`. Package import does not require POSIX `fcntl`, and lock files release when the owning process/descriptor exits. Linux behavior is covered by the full suite; Windows import/contract behavior is covered, but native Windows runtime acceptance requires a real Windows run.

## Canonical target model

All browser paths are normalized once into `ChatTarget`:

```text
fresh                     /
conversation              /c/<conversation-id>
project root              /g/g-p-<project-id>/project
project conversation      /g/g-p-<project-id>/c/<conversation-id>
```

Project identity and conversation identity remain separate. The same model is used for parsing, navigation, comparison, coordination, recovery, and atomic Send validation.

Targets with credentials, query strings, fragments, non-default ports, unsupported hosts/schemes, malformed IDs, or ambiguous paths fail before browser mutation.

## Exactly-once Send boundary

The irreversible path is:

```text
validate input and immutable file snapshots
  -> create durable request
  -> claim exact target ownership when applicable
  -> prove exact page and safe composer
  -> persist PREPARING and baseline
  -> persist CLICK_BOUNDARY_ENTERED
  -> atomically recheck URL/composer/attachments and click real Send once
  -> persist frontend acceptance and exact user identity
  -> persist DURABLE_HANDOFF/RUNNING
  -> prove exact final assistant node
  -> COMPLETE
```

Once the click boundary is entered, retry is prohibited. A crash, timeout, schema drift, or ambiguous frontend outcome produces `UNKNOWN/get_required`; the caller uses the same request ID with `get`.

## Attachment boundary

Every attachment is snapshotted before local state or browser construction. Duplicate paths, unsupported media types, missing files, and changed files are rejected deterministically.

The mutation path is:

```text
SNAPSHOTTED
  -> persist ATTACHMENT_BOUNDARY_ENTERED + UPLOAD_STARTED
  -> use the real frontend file input/menu
  -> prove the exact expected filename multiset
  -> revalidate file size/hash
  -> VERIFIED
  -> revalidate files and chips inside the final Send boundary
```

After `UPLOAD_STARTED`, any uncertain outcome is non-retryable by `send`: the exact helper page is preserved for reconciliation and no second upload or Send is attempted automatically. Manual/foreign attachments are preserved and fail closed.

File content is never persisted. Local canonical paths exist only in private durable state and are omitted from public projections/logs.

## Project lifecycle boundary

`ProjectRegistry` binds a stable caller key to exact project metadata and identity.

`ensure_project(key, name, memory_scope)` behaves as follows:

1. Reuse an already proven local key binding.
2. List projects through the real `/projects` frontend.
3. Select by exact name/ID only; never by card order, date, or fuzzy text.
4. On zero exact matches, atomically persist an unknown-create marker before clicking Create.
5. On one exact match, bind and reuse it.
6. On multiple exact matches, fail closed.
7. If a prior create is unknown, reconcile exact identity and never click Create a second time blindly.
8. Persist the exact `g-p-*` identity as soon as the canonical project URL is proven.

Project knowledge-file administration is outside this SDK. Composer attachments remain request-scoped.

## Page ownership and cleanup

The SDK may borrow one uniquely proven exact page or create one owned helper page. It never selects a page by order, title, recency, or approximate URL.

Only a page created or durably owned by the SDK can be closed automatically. Borrowed pages, unrelated pages, duplicate ambiguous pages, and Chromium remain open. On manual text, attachments, or a choice prompt, the SDK durably preserves the exact owned page for operator reconciliation. This policy is internal and not caller-controlled. There is no public helper-tab policy option.

## Response proof

Frontend acceptance supplies transport identity. The SDK then binds the exact accepted user node in the conversation graph and monitors only the corresponding current branch. Completion requires bounded graph convergence and an exact assistant candidate correlated to the request.

`get` reconstructs the exact target from durable project/conversation identity. It never sends and never guesses the latest tab or latest conversation.

## Cancellation

Pre-click cancellation can complete locally. Post-click cancellation requires exact conversation ownership, exact page/branch identity, and a positively observed Stop/cancel result. Ownership drift or ambiguous cancellation becomes `cancellation_unproven`, not `cancelled`.

## Logging and redaction

The library uses standard-library logging conventions and does not install global handlers. Diagnostics are sanitized. Public `Result.to_dict()` truncates identity fields and exposes machine-readable error codes/categories without raw backend payloads, credentials, private headers, or local attachment paths.
