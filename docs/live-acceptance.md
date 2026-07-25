# Live acceptance report

Date: 2026-07-25  
Target: persistent Chromium at `http://127.0.0.1:9222`  
Repository branch: `feat/stable-chatgpt-web-core`  
All identifiers below are truncated. No raw headers, cookies, tokens, request bodies, or response bodies were recorded in this report.

## Environment discovery

Read-only discovery established:

```text
CDP connected: yes
Persistent contexts: 1
Existing ChatGPT conversation pages at discovery: 7
Conversation graph GET: HTTP 200
Sample stream status: COMPLETE
Sample graph nodes: 74
Valid current_node: yes
Observed roles: assistant, system, tool, user
Observed graph identity fields: request_id, turn_exchange_id, working_turn_id
```

The transport response turn ID and graph turn ID differed for the same live request. The implementation was changed to persist separate transport and canonical graph namespaces before acceptance continued.

## Acceptance matrix

| # | Case | Evidence | Result |
|---:|---|---|---|
| 1 | Fresh conversation, exact `OK` | Request `52fe0be1...9116`; conversation `6a646117...7f7f`; transport turn `59e36cd0...b882`; graph turn `c9626da5...7a0a`; graph request `wfr_019f...51bc`; exit 0; response exactly `OK`; stderr 0 bytes | PASS |
| 2 | Reuse same conversation | Request `44adc517...bbff`; exit 0; exact response `SECOND_OK`; frontend parent, pre-Send graph anchor, and graph parent agreed | PASS |
| 3 | Attach watcher while running | Send and watcher both used request `c51ead00...0dd5`; watcher attached after `RUNNING:DURABLE_HANDOFF`; both exit 0; both returned identical 3,063-character response | PASS |
| 4 | `--wait-idle` ordering | Active request `1097d7c9...c6cd` completed; immediate competitor exited 21; next request `b01cf4b1...01e8` then exited 0 with exact `WAIT_IDLE_OK` | PASS |
| 5 | Helper closes after durable handoff | Successful default-send conversation had 0 matching helper pages after completion; monitoring still completed. `--keep-helper-tab` request `4d57b5f9...27fe` retained exactly 1 page and returned `KEEP_OK`; test cleanup returned the count to 0 | PASS |
| 6 | Kill/restart without duplicate Send | Process killed at `RUNNING:DURABLE_HANDOFF`; persisted state stayed RUNNING; watcher request `3e6acd1f...02ac` exited 0, response contained `RESTART_OK`, and the graph contained exactly 1 matching user node | PASS |
| 7 | Same-conversation concurrency | Immediate second sender exited 21 with category `ownership`; it created no request beyond the already-created active turn. Concurrent read-only watcher was allowed and returned the same request/response | PASS |
| 8 | Stop/cancel semantics | Pre-Send request became `CANCELLED`, exit 22; a stale PREPARING sender was proven unable to persist the click boundary after the cancellation revision. During active text and detected tool-call phases, Stop was clicked on an exact validated branch, but backend produced an exact `COMPLETE`; core returned COMPLETE rather than claiming cancellation. A separate race returned exit 23 `cancellation_unproven`; exact helper-page count remained 0. The completed tool request was rewatched after segmented-correlation hardening: COMPLETE, 2,704-character exact response, stderr 0 | PASS: honest semantics; hard cancellation not observed |
| 9 | Secret leakage | State/output scans found no bearer/JWT values, access/resume/proof tokens, Sentinel, Turnstile, authorization material, or cookies. All tested stderr files were 0 bytes except the intentional shell SIGKILL diagnostic outside core output | PASS |
| 10 | Graph fixtures | Mutable same-ID, stale history, current branch, regenerated sibling, missing/conflicting identity, transport-vs-graph namespace, delayed convergence, tool call/result, tool-parented internal subturns, later-human-turn rejection, terminal `IDLE/NOT_FOUND`, backend terminal failure, and ambiguous user binding are covered | PASS |

## Commands and exit codes

Representative commands:

```bash
uv run playwright-gpt send "Reply with exactly OK" \
  --fresh \
  --state-dir /tmp/pgc-final-ok-state \
  --timeout 180 \
  --poll 0.25 \
  --json
# exit 0
```

```bash
uv run playwright-gpt send "Reply with exactly SECOND_OK" \
  --conversation '<safe-selected-conversation>' \
  --state-dir /tmp/pgc-live-state \
  --json
# exit 0
```

```bash
uv run playwright-gpt watch '<persisted-request-id>' \
  --state-dir /tmp/pgc-watch-state \
  --json
# exit 0
```

```bash
uv run playwright-gpt send "Reply with exactly WAIT_IDLE_OK" \
  --conversation '<safe-selected-conversation>' \
  --wait-idle \
  --state-dir /tmp/pgc-wait-clean \
  --json
# exit 0
```

```bash
uv run playwright-gpt cancel '<active-request-id>' \
  --state-dir /tmp/pgc-cancel-state \
  --json
# observed exit 0 with exact COMPLETE, and exit 23 when cancellation remained unproven
```

## Restart evidence

The sending process was launched in its own process group, polled until the durable record was:

```text
RUNNING:DURABLE_HANDOFF
```

The process group was then killed with `SIGKILL`. The state remained unchanged. A new client process ran `watch` against the same request ID. It completed successfully and a read-only graph inspection found one, not two, nodes with the persisted exact user message ID.

## Uncertain-Send negative evidence

One existing-conversation Send crossed `CLICK_BOUNDARY_ENTERED` but no matching frontend response was observed within the send timeout. The graph also contained no unique post-baseline user node below the durable pre-Send anchor.

Observed result:

```text
request: e070a6ab...a9fd
state: UNKNOWN
provenance: RETRY_PROHIBITED
failure: ambiguous_outcome
```

A later structural reconciliation attempt found zero exact graph-delta candidates and refused to bind or resend. The durable conversation claim remained blocked. This is intentional fail-closed behavior, not a successful Send.

## Cancellation facts

### Before Send

```text
state: CANCELLED
exit: 22
browser action: none
```

### Active text turn

Stop was clicked after exact-branch validation. The backend reached a valid exact final response, so the request was recorded as `COMPLETE`.

### Tool phase

Tool activity was detected while:

```text
stream status: IS_STREAMING
node role: assistant
recipient: non-all
```

Stop was clicked, but the backend again reached a valid exact `COMPLETE`. The core did not claim that the tool/backend was hard-cancelled.

The final tool graph contained an internal user-role continuation directly below a tool result, with a new graph turn/request namespace. A fresh `watch` after segmented-correlation hardening returned COMPLETE with a 2,704-character exact response and zero stderr bytes. A later user node below an assistant remains rejected as a different human turn.

### Unproven race

Another active Stop request returned:

```text
exit: 23
state returned to cancel caller: UNKNOWN
failure: cancellation_unproven
```

The sender subsequently observed a valid exact final. The helper page was still closed before Playwright detached; matching page count was 0.

## Helper-page cleanup defect found during DEV

An initial cancellation implementation closed its helper page after leaving the Playwright session. Live page-count inspection found two retained test pages. Cleanup was moved into an inner `try/finally` while the session was still attached. The two known test-owned pages were closed, and a fresh active-cancel verification ended with zero matching pages.

## Automated validation

```bash
uv run pytest -ra
# 59 passed, 5 xfailed in 1.69s

uv build
# dist/playwright_gpt_core-0.1.0.tar.gz
# dist/playwright_gpt_core-0.1.0-py3-none-any.whl
```

The immutable prototype characterization tests are strict expected failures. They continue to demonstrate the five confirmed prototype defects without making the production suite fail.

A source AST test rejects actual calls to:

```text
browser.close()
page.route()
route.abort()
route.fulfill()
request.post()/put()/patch()/delete()
```

## Remaining externally blocked proof

A backend status explicitly representing hard cancellation was not observed in this session. The real Stop control was exercised during active text and tool execution, but the backend either produced an exact final or left cancellation unproven. The implementation therefore supports and reports cancellation honestly, but cannot claim live proof of hard backend cancellation.
