# Live facts and remaining assumptions

Date: 2026-07-29

Environment: persistent Chrome 150 on loopback CDP `127.0.0.1:9222`, using the built `playwright-api` wheel from an isolated virtual environment. Runtime identifiers are truncated.

## Proven live facts

### Text Send and recovery

A fresh root request completed with exact response `SDK_TEXT_OK_20260729`. Same-ID `get` returned the same result without another Send. Reusing that ordinary conversation completed with exact response `SDK_REUSE_OK_20260729`; the accepted conversation ID was unchanged and a duplicate request ID was rejected before browser mutation.

### Projects

The SDK created one uniquely named disposable project through the real `/projects` flow, reused the same project ID on a second `ensure_project`, and started a new chat inside that exact project.

The immediate project-chat Send outcome lacked a conversation ID. The SDK did not resend. Same-request recovery matched one exact project-conversation page, proved the accepted user identity from the graph, and completed with `SDK_PROJECT_OK_20260729`.

A client with no local project registry found the existing project by exact name, proved its stable ID, title, and `default` memory scope, bound a new stable key without clicking Create, and reused it again. Project Directory row order, creation date, fuzzy matching, and React class names were not used as identity.

Current project URLs can include a readable slug after the stable `g-p-*` prefix. Parsing normalizes the route to the stable project ID while preserving project and conversation identity separately.

### Attachments

The live frontend exposes one generic composer input as `#upload-files`, plus image-specific inputs that must not be selected for a generic file. Uploaded files appear as exact `role="group"` tiles with a filename label and a `Remove file …` control. A tile can exist while still pending, so filename presence alone is not upload completion.

The first attachment attempt exposed a boundary defect: durable `UPLOAD_STARTED` was written before file-input preflight. The frontend then rejected multiple candidate inputs before `set_input_files` ran. Read-only inspection proved no attachment chip, no pending upload, an empty composer, and no matching prompt in the conversation graph.

The boundary now occurs immediately before `set_input_files`. Therefore:

- selector/menu/file validation failures remain pre-mutation and release ownership;
- an exception after the callback remains upload uncertainty and prohibits blind upload or Send;
- exact attachment names, pending state, file digest, composer, target, owner, and Send control are revalidated at the click boundary.

A new disposable attachment request completed with exact response `SDK_ATTACHMENT_OK_20260729_03`. Durable stage was `VERIFIED`, file content was absent from durable state, and the public result did not expose the absolute path.

### Shared ownership and helper lifecycle

All live requests used one deployment-scoped coordination namespace. Final coordination state had no active owners.

Independent TEST exposed a Project helper leak: `_find_project` and `_create_project` attempted cleanup only after `BrowserSession` detached, so the Playwright wrapper appeared closed while the Chromium target remained. Cleanup now runs inside the active session before detach.

A committed rerun also proved that grid visibility alone was insufficient readiness: the requested exact row could hydrate later, and a transient zero match could permit a duplicate Create. Exact-name lookup now polls the exact match count for the existing bounded five-second window before returning zero; one match is selected and multiple matches fail closed. The disposable duplicate created by the failed validation run was removed through the exact frontend delete confirmation.

Final live verification ran three exact-name lookups, one registry-loss `ensure_project` reconciliation, and one registry reuse. The complete page target set remained unchanged at 16, no target was added or removed, and zero project-root helpers remained. Chromium stayed online, and project-conversation, unrelated, and borrowed pages were preserved.

### Packaging and portability

The wheel imported outside the source tree, reported version `0.1.0`, passed `pip check`, and included `py.typed`. Wheel and sdist excluded tests, `.plan`, reference prototypes, caches, and runtime state.

Cross-process locks use `portalocker`. Package import does not depend on POSIX `fcntl`. Linux behavior and crash-release contracts are covered by the regression suite. Windows-oriented import/contract tests pass, but native Windows runtime acceptance was not run.

### Regression and scans

Final DEV evidence:

```text
1,401 passed
5 intentional xfails
0 failed
Ruff passed
compileall passed
git diff --check passed
package allowlist passed
forbidden-operation scan passed
secret/JWT scan passed across 46 live state/output files
```

## Automated facts

The suite covers:

- public async/sync exports and running-event-loop misuse;
- exact request ID validation and duplicate rejection;
- per-user installed-package state defaults;
- loopback-only CDP configuration;
- cross-process coordination and crash release;
- strict target parsing for root, ordinary conversation, project root, and project conversation;
- current slugged project-route normalization;
- exact Project create/reconcile/fail-closed matching;
- immutable attachment snapshots and changed/duplicate/unsupported input rejection;
- upload preflight versus mutation boundary;
- partial, duplicate, pending, and manual attachment fail-closed behavior;
- exact target/composer/attachment/owner revalidation before Send;
- same-ID recovery with no second Send;
- cancellation and unresolved outcome semantics;
- public redaction, artifact contents, and forbidden operations.

## Remaining assumptions and limits

- Native Windows runtime behavior remains unproven until the wheel and cross-process locking are exercised on an actual Windows host.
- ChatGPT frontend roles, labels, and test IDs can change. Frontend drift fails closed and requires a new observed contract; the SDK does not fall back to card order or fuzzy project matching.
- Project-only memory is supported as a project setting, but ChatGPT Work mode is unavailable for that project type.
- Service-side throttling and network failures remain external conditions. After an irreversible boundary, recovery is same-ID `get`, never implicit Send retry.
- The package intentionally has no HTTP service, MCP server, daemon, scheduler, browser-profile manager, or generic multi-site abstraction.
- Different apps sharing one browser profile must use the same absolute coordination directory and stable deployment ID. The same logical request must use the same result state root; coordination is not a cross-repository result ledger.
- No retired command or library aliases are retained.
