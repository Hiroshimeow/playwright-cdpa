# Live acceptance evidence

Date: 2026-07-27

All commands ran from `/home/ayumi/Workspace/git_project/playwright-api-internal` against loopback CDP 9222. The disposable conversation identifier is shown as `6a66383f…fc90`. Result/state roots were under `/tmp`. Public JSON identifiers were truncated by the CLI. No `browser.close()` was called.

## 1. Fresh Send followed by exact result

Command:

```bash
uv run playwright-api send 'Reply with exactly API_V2_LIVE_OK' \
  --fresh \
  --request-id live-fresh-20260727b \
  --state-dir /tmp/pgpt-api-v2-live-a \
  --coordination-dir /tmp/pgpt-api-v2-live-coordination \
  --deployment-id cdp-9222-api-v2-live \
  --timeout 180 --poll 0.5 --json
```

Result:

```text
exit = 0
state = COMPLETE
disposition = complete
response = API_V2_LIVE_OK
helper target persisted and marked closed after handoff
```

An earlier fresh attempt exited 10 with `frontend_not_ready` because the newly opened page was inspected before composer hydration. The implementation was corrected to wait boundedly for one safely editable empty composer before filling; the command above is the post-fix acceptance.

## 2. Reuse completed conversation

Command:

```bash
uv run playwright-api send 'Reply with exactly API_V2_SECOND_OK' \
  --conversation '6a66383f…fc90' \
  --request-id live-second-20260727 \
  --state-dir /tmp/pgpt-api-v2-live-a \
  --coordination-dir /tmp/pgpt-api-v2-live-coordination \
  --deployment-id cdp-9222-api-v2-live \
  --timeout 180 --poll 0.5 --json
```

Result:

```text
exit = 0
state = COMPLETE
response = API_V2_SECOND_OK
```

The closed helper from the fresh request was not reused by page order; the exact conversation URL was opened/resolved.

## 3. Active response and steering readiness

A disposable direct frontend fixture started a long response through the real composer/Send control, then filled the steering prompt while Stop was active. It used the same `observe_frontend`, `fill_composer`, and atomic real-Send primitive as the core.

Observed:

```text
exact_page_candidates = 1
initial_after_fill_send_ready = false
initial_after_fill_stop_visible = true
polls_until_send_ready = 601 at 50 ms
stop_visible_when_send_ready = false
sent_once = true
exit = 0
```

Conclusion: this live frontend did not expose enabled Send while Stop remained visible. The prompt was preserved for approximately 30 seconds and sent once only after Send became enabled and Stop disappeared. No Stop click occurred.

A public core continuation in the same disposable conversation returned:

```text
request_id = live-steer-20260727
exit = 0
state = COMPLETE
response = STEERED_API_V2_OK
```

Automated coverage separately proves the immediate Send+Stop branch sends once if that frontend state is observed.

## 4. Concurrent `get` observer

Initial run:

```text
observed start state = SENT
sender exit = 0, COMPLETE
get exit = 12, UNKNOWN/identity_missing
```

This was treated as a defect, not accepted evidence. Root cause: `get` reconciled transient `SENT` before the active sender completed durable user-identity binding.

After the correction, command shape:

```bash
# process 1
uv run playwright-api send '<120-line fixture>' \
  --conversation '6a66383f…fc90' \
  --request-id live-active-get-retry-20260727 \
  --state-dir /tmp/pgpt-api-v2-live-d \
  --coordination-dir /tmp/pgpt-api-v2-live-coordination \
  --deployment-id cdp-9222-api-v2-live \
  --timeout 300 --poll 0.25 --json

# process 2, launched after persisted state reached SENT
uv run playwright-api get live-active-get-retry-20260727 \
  --state-dir /tmp/pgpt-api-v2-live-d \
  --coordination-dir /tmp/pgpt-api-v2-live-coordination \
  --deployment-id cdp-9222-api-v2-live \
  --timeout 300 --poll 0.25 --json
```

Result:

```text
observed start state = SENT
sender exit = 0
get exit = 0
sender response length/hash = 2411 / 206c008627bd18ab
get response length/hash    = 2411 / 206c008627bd18ab
```

Both observers returned the same exact response. The `get` path contains no Send call.

## 5. Missing exact tab recovery

The one exact disposable conversation page was closed with `page.close()`; Chromium and unrelated tabs remained open.

Command:

```bash
uv run playwright-api get live-active-get-retry-20260727 \
  --state-dir /tmp/pgpt-api-v2-live-d \
  --coordination-dir /tmp/pgpt-api-v2-live-coordination \
  --deployment-id cdp-9222-api-v2-live \
  --timeout 180 --poll 0.25 --json
```

Result:

```text
exact pages closed before get = 1
browser closed = false
exit = 0
state = COMPLETE
response length/hash = 2411 / 206c008627bd18ab
```

The missing helper caused exact URL recovery. No Send was created.

## 6. Client restart and same-ID recovery

Fixture command shape:

```bash
uv run playwright-api send '<90-line fixture>' \
  --conversation '6a66383f…fc90' \
  --request-id live-restart-get-20260727 \
  --state-dir /tmp/pgpt-api-v2-live-e \
  --coordination-dir /tmp/pgpt-api-v2-live-coordination \
  --deployment-id cdp-9222-api-v2-live \
  --timeout 300 --poll 0.25 --json
```

The first process encountered authenticated backend HTTP 429 after crossing the irreversible boundary and exited 10 with:

```text
state = UNKNOWN
disposition = get_required
failure = backend GET failed with HTTP 429
```

No resend was issued. Later command:

```bash
uv run playwright-api get live-restart-get-20260727 \
  --state-dir /tmp/pgpt-api-v2-live-e \
  --coordination-dir /tmp/pgpt-api-v2-live-coordination \
  --deployment-id cdp-9222-api-v2-live \
  --timeout 300 --poll 0.5 --json
```

Result:

```text
exit = 0
state = COMPLETE
disposition = complete
response length/hash = 2060 / 6b61ed2d7daf1f13
accepted user message ID occurrences in exact graph = 1
```

This proves same-ID restart recovery and one submitted user node despite an external transient failure.

## 7. Different local state roots, one shared owner

A separate process held the exact conversation claim as `foreign-live-owner` for three seconds using the same absolute coordination root/deployment ID. The contender used a different local state root:

```bash
uv run playwright-api send 'Reply with exactly SHARED_OWNER_SERIALIZED_OK' \
  --conversation '6a66383f…fc90' \
  --request-id live-shared-owner-20260727 \
  --state-dir /tmp/pgpt-api-v2-live-b \
  --coordination-dir /tmp/pgpt-api-v2-live-coordination \
  --deployment-id cdp-9222-api-v2-live \
  --timeout 180 --poll 0.25 --json
```

Result:

```text
foreign log = FOREIGN_CLAIMED, FOREIGN_RELEASED
contender exit = 0
state = COMPLETE
response = SHARED_OWNER_SERIALIZED_OK
elapsed = 22 seconds
```

Automated acceptance additionally asserts that no contender local turn file exists before claim and only one contender can create a turn after release.

## 8. Secret and forbidden-operation evidence

Verification scans cover stdout/stderr, persisted local state, coordination files, and documentation/evidence for forbidden secret labels and raw credentials. Public JSON uses truncated identity summaries. The implementation contains no `browser.close()`, request routing/interception, abort, fulfill, conversation POST mutation, Retry, Regenerate, or automatic Stop path.

Final command results are recorded in the DEV handoff report after the full verification gate.
