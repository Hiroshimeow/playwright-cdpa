# Live acceptance evidence

Date: 2026-07-29

Repository: `/home/ayumi/Workspace/git_project/playwright-gpt-internal`

Branch: `feat/playwright-api-sdk`

Environment:

- persistent Chrome 150 on loopback CDP `127.0.0.1:9222`;
- installed wheel in an isolated virtual environment outside the source tree;
- disposable state and coordination roots under `/tmp`;
- runtime identifiers below are truncated;
- no `browser.close()`, request interception, request replay, private upload API, Retry, Regenerate, or automatic Stop action.

## 1. Baseline and regression

Baseline before SDK productization:

```text
1,345 passed
5 intentional xfails
0 failed
```

Final DEV regression after Project, target, attachment, packaging, and upload-boundary corrections:

```text
1,401 passed
5 intentional xfails
0 failed
Ruff passed
compileall passed
git diff --check passed
```

## 2. Build and isolated installation

Commands:

```bash
uv build
uv venv --seed /tmp/playwright-api-wheel-turn2
uv pip install --python /tmp/playwright-api-wheel-turn2/bin/python \
  dist/playwright_api-0.1.0-py3-none-any.whl
```

Verified:

```text
wheel import outside source tree = pass
playwright_api.__version__ = 0.1.0
pip check = pass
wheel members = 27
sdist members = 27
py.typed present = true
tests/.plan/reference/cache/runtime state absent = true
```

## 3. Fresh text Send and same-ID recovery

A fresh root Send completed with:

```text
request = sdk-live-text-20260729-01
state = COMPLETE
response = SDK_TEXT_OK_20260729
conversation = 6a68d65d…778f
```

A later `get` using the same request ID returned the same exact result. A duplicate `send` with the same request ID was rejected before another Send could occur.

## 4. Ordinary conversation reuse

The completed ordinary conversation above was reused with a new request:

```text
request = sdk-live-ordinary-reuse-20260729-01
state = COMPLETE
response = SDK_REUSE_OK_20260729
same conversation = true
same-ID get = same response
duplicate request ID rejected = true
```

The SDK resolved the exact conversation identity rather than selecting a page by order.

## 5. Project creation, reuse, and exact reconciliation

A uniquely named disposable project was created through the real `/projects` frontend flow.

Verified:

```text
first ensure_project = created one project
second ensure_project = reused same project ID
project ID = g-p-6a68d9…7fd8
memory scope = default
```

A separate client with an empty local project registry then:

- found the exact project name in the Project Directory grid;
- navigated to the selected project;
- proved the stable `g-p-*` identity from the canonical route;
- proved the exact title and memory scope from Project Settings;
- bound a new stable caller key without clicking Create;
- reused the same project ID on the next call.

Zero matches remain eligible for one Create. Multiple exact matches fail closed. An uncertain post-Create outcome remains durable and prohibits a blind second Create.

## 6. Fresh project chat and same-request recovery

A fresh chat was started from the exact project-root target. The frontend accepted one Send but initially did not provide a conversation ID in the immediate response.

The request remained unresolved instead of resending. Same-ID recovery then matched the exact project conversation page, bound the accepted user node from the graph, and completed:

```text
request = sdk-live-project-chat-20260729-01
state = COMPLETE
response = SDK_PROJECT_OK_20260729
conversation = 6a68d942…a87c
second Send = none
```

Current ChatGPT project routes may append a readable slug after the stable project ID. The SDK normalizes that route to the stable `g-p-*` identity while preserving the project and conversation IDs separately.

## 7. Attachment boundary defect and correction

The first disposable attachment request failed with:

```text
failure = could not identify one exact ChatGPT file input
state = UNKNOWN
attachment stage = UPLOAD_STARTED
```

Investigation proved the old implementation persisted the upload boundary before file-input preflight. The frontend function raised because multiple file inputs existed before `set_input_files` was called.

Read-only evidence on the exact conversation showed:

```text
matching project conversation pages = 1
composer empty = true
attachment chips = []
pending attachments = false
old upload prompt occurrences in graph = 0
```

The correction moves the durable upload boundary to immediately before the exact `#upload-files` mutation. Preflight failures now remain pre-mutation, fail terminally, and release ownership. Post-mutation uncertainty remains fail closed.

The stale live owner was released only after the code-path and frontend/graph evidence proved no upload and no Send occurred. Its original UNKNOWN turn record was preserved as evidence.

## 8. Attachment Send acceptance

A new request used one disposable text file through the real frontend file input:

```text
request = sdk-live-attachment-20260729-03
state = COMPLETE
response = SDK_ATTACHMENT_OK_20260729_03
conversation = 6a68d942…a87c
attachment stage = VERIFIED
file content persisted = false
absolute path in public result = false
```

The SDK verified the exact attachment tile identity, waited until the tile was no longer pending, revalidated file size/hash and the exact attachment multiset at the irreversible Send boundary, then clicked Send once.

## 9. Ownership and browser/page cleanup

Independent TEST found that Project helpers closed their page after `BrowserSession` detached. The detached Playwright wrapper then appeared closed even though the Chromium target remained, leaking one project-root tab per lookup.

The correction closes Project find/create helper pages inside the active browser session, before `playwright.stop()` detaches.

A committed rerun then exposed a second exact-lookup boundary: the Project grid could become visible before the requested row finished hydrating. One transient zero match incorrectly triggered a disposable duplicate Create. Exact-name lookup now polls the exact row count for the existing bounded five-second readiness window; one match is selected, multiple matches fail closed, and only a stable zero result can permit Create. The disposable duplicate was removed through its exact Project Settings delete confirmation.

Final live verification used the original disposable project and proved:

```text
initial page targets = 14
find_project calls = 3
registry-loss ensure_project reconciliation = true
registry reuse = true
added targets = []
removed targets = []
final page targets = 14
project-root pages after completion = 0
```

The previously leaked disposable project-root tabs were closed explicitly. Project-conversation pages, unrelated tabs, and borrowed pages were preserved. Final coordination state had no active owners, and Chromium remained online.

## 10. Static and state scans

Verified:

```text
forbidden browser/request mutation AST scan = pass
HTTP/FastAPI/MCP server symbol scan = pass
secret/JWT scan = pass across 46 live state/output files
```

Durable attachment state contains path, size, digest, media type, and mutation stage, but not file content. Public result projection omits the absolute local path.

## Remaining unproven boundary

Linux import, locking, crash-release contracts, build, and live CDP behavior were exercised. Windows-oriented import and locking contracts are covered by automated tests, and unconditional `fcntl` imports are gone. Native Windows runtime acceptance was not performed and must not be claimed until the wheel is executed on a real Windows host.
