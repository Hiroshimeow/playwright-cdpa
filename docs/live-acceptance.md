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
# 93 passed, 5 xfailed

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

## DEV turn 3 ownership and helper-lifecycle remediation

TEST returned the implementation for two defects. Both were corrected on the same feature branch.

### Pre-click Playwright failure

Regression coverage invokes real Playwright `TimeoutError` and `Error` exception classes from `page.goto()` before the click boundary. Verified outcomes:

```text
state: FAILED
timeout category: timeout, external, retryable
generic Playwright category: network, external, retryable
conversation active_request_id: null
helper close timestamp: present
next request can claim the same conversation: yes
```

The claim-release decision now depends on `click_entered == false`, not on which exception hierarchy was raised. After the irreversible boundary, the claim remains fail-closed.

### Durable exact helper identity

Turn-record schema v4 persists:

```text
helper_page_target_id
helper_page_keep
helper_page_closed_at
```

Cleanup uses page-scoped CDP `Target.getTargetInfo`. URL matching is not used. Protocol-fake integration covers:

```text
frontend accepted
backend GET HTTP 429 during graph identity binding
state UNKNOWN / RETRY_PROHIBITED
owned helper remains open
watch exact recovery succeeds
owned target closes
unrelated target remains open
helper close timestamp persists
conversation claim releases
```

### Fresh live proof after the correction

```text
prompt: Reply with exactly DEV3_OK
exit: 0
response: DEV3_OK
state: COMPLETE
provenance: DURABLE_HANDOFF
turn schema: 4
helper target persisted: yes
helper close timestamp persisted: yes
open pages matching persisted target after completion: 0
stderr: 0 bytes
```

A separate live two-page target test confirmed that exact-target cleanup closed the owned page and did not close the unrelated page.

Post-remediation compatibility runs passed on Python 3.11.15, 3.12.13, and 3.14.0. Python 3.10 was not installed on the host and was not downloaded or installed during this task.

## DEV turn 4 strict helper-state remediation

TEST found that schema-v4 helper fields were being coerced with `str(...)` and `bool(...)`. That could turn `"false"` into an enabled keep policy, convert arrays into false, accept numeric target IDs, or treat a numeric close marker as proof that cleanup already happened.

The decoder now rejects malformed state before service orchestration:

```text
helper_page_keep: exact JSON boolean only
helper_page_target_id: null or 1–256 printable ASCII characters without spaces
helper_page_closed_at: null or bounded timezone-aware ISO timestamp
closed timestamp without target ID: rejected
keep=true without target ID: rejected
missing schema-v4 helper field: rejected
schema-v2/v3 injected helper fields: ignored; no ownership is inferred
```

StateStore regressions cover strings, integers, null, arrays, objects, empty/oversized/control-character target IDs, invalid/naive timestamps, missing fields, and cross-field violations. Service regressions cover both `watch` and `cancel` and prove:

```text
raised error: CorruptStateError
BrowserSession constructed: no
page close attempted: no
turn file changed: no
conversation claim changed: no
```

CLI-level evidence used a malformed terminal record with an active conversation claim:

```text
command: playwright-gpt watch <corrupt-request> --json
exit: 20
failure category: corrupt_state
raw turn record unchanged: yes
conversation claim unchanged: yes
stderr: 0 bytes
```

A valid existing schema-v4 live record was rewatched after the strict decoder change:

```text
exit: 0
response: DEV3_OK
schema: 4
helper_page_keep decoded type: bool
helper_page_target_id decoded type: str
helper_page_closed_at decoded type: str
stderr: 0 bytes
```

Post-change compatibility passed on Python 3.11.15, 3.12.13, and 3.14.0. Python 3.10 remains unavailable on the host.

## Replacement DEV turn 1 remediation and live evidence

Date: 2026-07-26

Temporary evidence root: `/tmp/pgc-dev1b-20260726`

Coordination namespace: explicit `shared-cdp-9222`

### Review findings closed

1. Conversation mutation ownership is now deployment-wide and independent of repository-local result `state_dir`.
2. Persisted, transport, and graph identity fields are exact bounded strings; assistant recipients are mandatory and unambiguous.
3. Cancellation uses the same mutable-node candidate/chain convergence tracker as normal monitoring.
4. Free-form diagnostics, URLs, parser failures, and unexpected exceptions are sanitized before persistence or output.

### Live cross-store ownership matrix

| Case | Local result store | Result |
|---|---|---|
| Fresh exact Send | `state-fresh` | exit 0, `COMPLETE`, exact `DEV1B_OK`, stderr 0 |
| Reuse same conversation | `state-reuse` | exit 0, `COMPLETE`, exact `DEV1B_REUSE_OK`, stderr 0 |
| Long owner | `state-owner` | exit 0, response ended `DEV1B_LONG_DONE` |
| Immediate contender during owner | `state-contender` | exit 21, `ownership`, zero turn files, stderr 0 |
| Foreign owner for wait-idle | `state-wait-owner` | exit 0, response ended `DEV1B_WAIT_OWNER_DONE` |
| Cross-store `--wait-idle` | `state-waiter` | exit 0, exact `DEV1B_WAIT_OK`, stderr 0 |

The coordination plane had one conversation record throughout. It contained no local result-store path, had mode `0600` under a `0700` deployment directory, and ended with `active_request_id: null`. After narrowing the process lock to atomic claim/release transitions while retaining the durable active owner, the live contention/wait-idle sequence was rerun: the contender again exited 21 with zero turn files, the owner ended `DEV1_CURRENT_OWNER_DONE`, and the cross-store waiter returned exact `DEV1_CURRENT_WAIT_OK`; combined stderr remained zero bytes.

### Fresh graph-materialization race

The first fresh run crossed the real frontend acceptance boundary and obtained durable conversation identity while the new graph endpoint still returned HTTP 404. It returned `UNKNOWN` with a backend failure. Three seconds later, `watch` on the same request completed with exact `DEV1_OK`, proving no resend was required.

A focused regression was added before the fix. Backend snapshot now represents a not-yet-materialized graph as `None`; identity binding continues bounded polling instead of treating the transient 404 as a terminal Send failure. A fresh post-fix run completed directly with exact `DEV1B_OK`.

### Helper and secret evidence

The post-fix fresh record had a non-null helper close timestamp. Read-only target enumeration found zero open pages matching the persisted helper target ID. No persistent Chromium close was called.

A scan of the current live stdout, stderr, local state, and shared coordination trees found no bearer/JWT values, credential assignments, cookies, authorization material, Sentinel, Turnstile, or proof material. The coordination payload keys were exactly:

```text
active_request_id
conversation_id
last_terminal_request_id
revision
schema_version
updated_at
```

### Automated evidence

```bash
uv run pytest -ra
# 131 passed, 5 xfailed

uv run --python 3.11 pytest -q
uv run --python 3.12 pytest -q
uv run --python 3.14 pytest -q
# all passed with the same five immutable prototype characterization xfails

uv build
# dist/playwright_gpt_core-0.1.0.tar.gz
# dist/playwright_gpt_core-0.1.0-py3-none-any.whl
```

Python 3.10 remains unavailable on the host. The externally blocked hard-cancellation proof remains unchanged: Stop is exercised honestly, but no backend status explicitly proving hard cancellation has been observed.

## Replacement DEV turn 2 review remediation and live evidence

Date: 2026-07-26

Temporary evidence root: `/tmp/pgc-dev2-20260726`

### Review findings closed

1. The default coordination base is absolute and frozen during validation. Relative `XDG_STATE_HOME` no longer creates repository-CWD-local coordination islands.
2. Candidate convergence is consecutive. Normal monitor and cancellation reset on nonterminal status, absent graph, or an unresolvable exact candidate.
3. Diagnostic secret-label normalization covers camelCase, snake_case, and kebab-case in text and URLs, including the exact REVIEW cases `accessToken`, `session_token`, `api_key`, and `proof_material`.
4. Turn and conversation state decoding no longer coerces failure flags, enums, integers, hashes, timestamps, response metadata, or cross-field state/provenance combinations.

### Cross-CWD coordination proof

Two clients were constructed under different working directories with the same HOME and `XDG_STATE_HOME=relative-xdg-state`:

```text
first_root  /tmp/pgc-dev2-20260726/home/.local/state/playwright-gpt-core/coordination/cdp-ccd7d89f…7662
second_root /tmp/pgc-dev2-20260726/home/.local/state/playwright-gpt-core/coordination/cdp-ccd7d89f…7662
same_absolute True
second claim: ownership conflict
```

### Existing-record live compatibility

```text
request: ce6fbaad…f0c1
command: watch with the original explicit shared coordination namespace
exit: 0
state: COMPLETE
response: DEV1_CURRENT_WAIT_OK
stderr: 0 bytes
```

### Fresh real-frontend Send

```text
request: 2a2630e2…5680
exit: 0
state: COMPLETE
response: DEV2_OK
send provenance: DURABLE_HANDOFF
turn schema: 4
helper close timestamp persisted: yes
open pages matching persisted helper target: 0
stderr: 0 bytes
secret scan: PASS
```

### Automated evidence

```text
uv run pytest -ra
177 passed, 5 xfailed
```

The five xfails remain immutable prototype characterization tests. Hard backend cancellation was not newly observed; this remains an explicit external limitation rather than an acceptance claim.

## Replacement DEV turn 3 review remediation and live evidence

Date: 2026-07-26

Temporary evidence root: `/tmp/pgc-dev3-20260726`

### Review findings closed

1. Exact-turn convergence no longer hashes redacted content. The implementation hashes exact allowlisted raw in-memory node and chain material, returns only SHA-256 digests, and therefore cannot accept two raw-different credential-shaped responses as stable.
2. Turn persistence is bound to its public identity. Requested ID, filename stem, and embedded request ID must all match before any record is returned.
3. The diagnostic sanitizer now applies one normalized secret grammar to structured keys, free-form assignments, query keys, and URL path markers, including generic secret/password suffixes and compound marker-plus-payload segments.

### Exact existing-record compatibility

```text
CDP HTTP: 200
request: 2a2630e2…5680
exit: 0
state: COMPLETE
response: DEV2_OK
stderr: 0 bytes
runtime source: target repository src/playwright_gpt_core
```

### Fresh real-frontend Send

```text
prompt: Reply with exactly DEV3_OK
request: 3c2de378…2dd0
exit: 0
state: COMPLETE
response: DEV3_OK
send provenance: DURABLE_HANDOFF
turn schema: 4
helper close timestamp persisted: yes
open pages matching persisted helper target: 0
stderr: 0 bytes
```

### Poisoned request identity proof

A file named for `requested-id` was populated with a valid schema-v4 record embedding a different request ID. Public CLI `get requested-id` returned:

```text
exit: 20
category: corrupt_state
message: turn record identity mismatch
public request_id: -
stderr: 0 bytes
raw poisoned file: unchanged
```

Automated service regressions prove the same record is rejected by `watch`, `recover`, and `cancel` before browser construction or coordination mutation. Discovery also fails through the checked load path.

### Secret and convergence proof

Parameterized regressions cover raw-different response pairs shaped as proof-token assignments, authorization values, and credential-bearing URLs. Two terminal samples are insufficient after the raw mutation; a third identical raw sample is required in both monitor and cancellation paths.

The live evidence state and diagnostic proof removed generic secret/password assignments and a compound credential-path payload. The sanitized state remained valid JSON. Ordinary near-match path resource names are covered separately to avoid needless diagnostic destruction.

### Automated evidence

```text
uv run pytest -ra
210 passed, 5 xfailed
```

The five xfails remain immutable prototype characterization tests. Hard backend cancellation was not newly observed and remains explicitly incomplete rather than claimed.

## Replacement DEV turn 4 — credential and private-key boundary remediation

Date: 2026-07-26

Temporary evidence root: `/tmp/pgc-dev4-20260726`

### TEST turn 3 blocker closed

The normalized secret predicate now covers:

```text
credential / credentials
qualified *_credential / *_credentials
private_key / privateKey / compact privatekey
qualified *_private_key
signing_key / signingKey / compact signingkey
qualified *_signing_key
```

The extension is shared rather than duplicated. It therefore applies to mapping keys, free-form assignments, URL query keys, compound path markers, nested diagnostic strings, `Failure`, durable state, and CLI rendering.

### Bounded near-match behavior

Tested public diagnostic path contexts remain visible:

```text
credentials-guide
credentialed-user
private-key-format
signing-key-docs
public-key-resource
```

Compound marker-plus-payload paths are redacted with following path context. An explicit public-context allowlist is bounded to documentation/resource terms rather than weakening secret-label recognition.

### Multi-parameter query correction

A focused regression found that the free-form assignment scanner could consume the remainder of a sanitized URL query because `&` was not a value delimiter. The scanner now stops unquoted values at `&`. Secret query parameters remain redacted independently while a nonsecret parameter such as `mode=inspect` remains visible.

### Exact persisted rewatch

```text
CDP /json/version HTTP: 200
request: 3c2de378…2dd0
exit: 0
state: COMPLETE
response: DEV3_OK
stderr: 0 bytes
```

No new Send was required because the defect was wholly local to redaction/state/output boundaries.

### Adversarial boundary proof

A local corpus used generated credential, private-key, and signing-key sentinel values without writing those raw values into the report. Observed:

```text
diagnostic values removed: all true
structured JSON values removed: all true
Failure values removed: all true
state-file values removed: all true
nonsecret query context preserved: true
state remains valid JSON: true
CLI exit: 20
CLI stderr: 0 bytes
CLI values removed: true
CLI redaction marker present: true
evidence-tree secret scan: pass
```

### Automated evidence

```text
uv run pytest -ra
244 passed, 5 xfailed
```

The five xfails remain immutable prototype characterization tests. Existing cancellation and external-schema limitations are unchanged.

## Replacement DEV turn 5 — publication identifier minimization

Date: 2026-07-26

### Documentation contract correction

The committed report previously contained five full durable request identifiers and two occurrences of one full coordination namespace digest. They are now represented consistently as truncated prefix/suffix forms, for example:

```text
request: 3c2de378…2dd0
coordination namespace: cdp-ccd7d89f…7662
```

No product runtime or persisted state was changed.

### Regression gate

`tests/test_documentation.py` scans the public Markdown surfaces:

```text
README.md
reference/README.md
docs/**/*.md
```

It rejects:

- full UUID-shaped values;
- contiguous hexadecimal identifiers from 24 through 64 characters.

The sole full-length exception is an exact checksum already declared in `reference/SHA256SUMS`, which preserves the immutable prototype-integrity proof without allowing runtime identifiers by context or label heuristics.

Initial result before remediation:

```text
7 findings
- 5 durable request identifiers
- 2 coordination digest occurrences
```

Final result:

```text
0 findings
1 documentation test passed
```

### Exact persisted rewatch

```text
CDP /json/version HTTP: 200
request: 3c2de378…2dd0
exit: 0
state: COMPLETE
response: DEV3_OK
stderr: 0 bytes
```

### Automated evidence

```text
Python 3.11.15: 245 passed, 5 xfailed
Python 3.12.13: 245 passed, 5 xfailed
Python 3.14.0:  245 passed, 5 xfailed
```

The five xfails remain immutable prototype characterization tests. Existing runtime limitations are unchanged.

## Replacement DEV turn 6 — version-agnostic publication gate

Date: 2026-07-26

### Gate correction

The dashed identifier detector previously constrained UUID version and variant nibbles. That was inappropriate for publication minimization because the contract concerns full identifier shape, not semantic UUID validity.

The detector now rejects every complete hexadecimal dashed shape with group lengths:

```text
8-4-4-4-12
```

No version or variant nibble is privileged.

### Direct corpus

The focused tests prove rejection of:

```text
version-4-shaped dashed identifier
version-6-shaped dashed identifier
version-7-shaped dashed identifier
nil-shaped dashed identifier
uppercase dashed identifier
32-character contiguous hexadecimal identifier
```

Negative controls prove acceptance of:

```text
Unicode-ellipsis prefix/suffix form
ASCII-ellipsis prefix/suffix form
truncated coordination namespace
short request prefix label
```

The exact checksum declared in `reference/SHA256SUMS` is allowed. A checksum of the same length with one changed character is rejected.

### TDD evidence

Before the detector correction, the added corpus failed exactly three cases:

```text
version-6-shaped
version-7-shaped
nil-shaped
```

After switching to the version-agnostic shape expression:

```text
tests/test_documentation.py: 12 passed
```

### Exact persisted rewatch

```text
CDP /json/version HTTP: 200
request: 3c2de378…2dd0
exit: 0
state: COMPLETE
response: DEV3_OK
stderr: 0 bytes
```

### Automated evidence

```text
Python 3.11.15: 256 passed, 5 xfailed
Python 3.12.13: 256 passed, 5 xfailed
Python 3.14.0:  256 passed, 5 xfailed
```

The five xfails remain immutable prototype characterization tests. Product runtime and existing limitations are unchanged.

## Replacement DEV turn 7 — baseline, nested-state, and secret remediation

Date: 2026-07-26

### Uncertain structural recovery

Persisted baseline fingerprints are now operational proof rather than an ID-only set. The decoder requires lowercase SHA-256 digests. Before binding a graph-delta user, recovery recomputes every baseline node from exact allowlisted raw material.

The projection retains parent, original baseline children, author, recipient, status, content, and selected metadata. It excludes only children added outside the original baseline, because a valid Send necessarily appends a new user child to the pre-Send anchor.

Focused results:

```text
unchanged baseline plus one new user child: accepted
same-ID baseline content change: rejected
missing baseline node: rejected
missing durable anchor fingerprint: rejected
malformed fingerprint: corrupt_state
identity after rejection: unbound
state after rejection: UNKNOWN
shared owner and revision after rejection: unchanged
```

### Exact nested identity schema

The current persisted identity object requires every supported field and rejects every unsupported field after explicit legacy migration. Direct store, CLI, and service regressions prove a poisoned nested identity:

```text
returns corrupt_state
keeps the original file byte-for-byte
constructs no BrowserSession
changes no helper lifecycle
changes no shared ownership record
```

### Cloud and encryption secret grammar

The shared normalized predicate now covers:

```text
canonical secret-access-key labels
encryption-key labels
passphrase labels
qualified snake, kebab, camel, and compact forms
```

The same predicate is exercised by assignments, mappings, query keys, compound paths, nested diagnostics, `Failure`, atomic state writes, and CLI JSON. Explicit guide/help/format paths remain diagnostic context.

A generated adversarial corpus reported:

```text
diagnostic values removed: true
structured values removed: true
Failure values removed: true
state values removed: true
state remains valid JSON: true
nested identity poison rejected: true
nested identity poison byte-preserved: true
baseline drift rejected: true
identity remained unbound: true
state remained UNKNOWN: true
shared owner remained unchanged: true
evidence-tree secret scan: pass
```

### Exact persisted rewatch

```text
CDP /json/version HTTP: 200
request: 3c2de378…2dd0
exit: 0
state: COMPLETE
response: DEV3_OK
stderr: 0 bytes
```

No new frontend Send was issued.

### Automated evidence

```text
Python 3.11.15: 295 passed, 5 xfailed
Python 3.12.13: 295 passed, 5 xfailed
Python 3.14.0:  295 passed, 5 xfailed
```

The five xfails remain immutable prototype characterization tests. Existing hard-cancellation and external-schema limitations are unchanged.

## Replacement DEV turn 8 — sibling ambiguity and multi-word passphrases

Date: 2026-07-26

### Complete-mapping structural uniqueness

An uncertain existing-conversation click has no persisted prompt body and may have no transport correlation. `current_node` alone is therefore not proof of which later user node came from the click.

Recovery now requires both:

```text
exactly one post-baseline user child of the durable anchor across the complete mapping
that same node is the only post-baseline user on the current branch
```

Public `watch` and `recover` regressions cover:

```text
two sibling user children below the anchor
one current and one off-current sibling
multiple post-baseline users on the current chain
one unique user control
```

Ambiguous results preserve:

```text
state: UNKNOWN
send provenance: RETRY_PROHIBITED
response: absent
user/turn identity: unbound
helper lifecycle: unchanged
shared owner: exact request
shared owner revision: unchanged
```

The unique control reaches exact `COMPLETE`, returns only its own final response, and releases ownership.

### Multi-word passphrase boundary

Unquoted values classified by the shared secret predicate now consume through the first safe boundary:

```text
comma
semicolon
ampersand
newline
following assignment boundary
end of input
```

Whitespace alone is not a boundary. Quoted values stop at their closing quote. URL query values that are already `<redacted>` remain terminal and do not consume following prose.

Coverage includes:

```text
passphrase=multi word value
passphrase: multi word value
qualifiedPassphrase=multi word value
quoted passphrase
semicolon/comma/newline context
ampersand query context
following assignment context
nested diagnostics
Failure serialization
durable state
CLI JSON stdout/stderr
cookie-header and multi-cookie preservation gates
```

### Independent adversarial evidence

```text
sibling watch fail-closed: true
sibling identity unbound: true
sibling owner unchanged: true
multi-word passphrase removed: true
safe context preserved: true
state valid JSON: true
CLI exit 20 and stderr empty: true
evidence-tree secret scan: pass
```

### Exact persisted rewatch

```text
CDP /json/version HTTP: 200
request: 3c2de378…2dd0
exit: 0
state: COMPLETE
response: DEV3_OK
stderr: 0 bytes
```

No new frontend Send was issued.

### Automated evidence

```text
Python 3.11.15: 312 passed, 5 xfailed
Python 3.12.13: 312 passed, 5 xfailed
Python 3.14.0:  312 passed, 5 xfailed
```

The five xfails remain immutable prototype characterization tests. Existing hard-cancellation and external-schema limitations are unchanged.

## Replacement DEV turn 9 — quoted diagnostic keys and explicit cookie headers

Date: 2026-07-26

### Quoted-key assignment boundary

The free-form sanitizer now recognizes the following bounded key forms before `:` or `=`:

```text
passphrase=...
"passphrase": ...
'passphrase': ...
"aws_secret_access_key"=...
```

Single and double quotes must match. The unquoted key text is classified by the existing normalized secret predicate. Quoted values preserve their matching quote boundary around `<redacted>`, while bounded nonsecret sibling fields remain visible.

Coverage includes:

```text
quoted JSON passphrase with multiple words
quoted Python-style passphrase with multiple words
quoted cloud-secret key
qualified quoted passphrase key
nested safe JSON
Failure serialization
durable state
CLI JSON stdout/stderr
```

### Explicit cookie provenance

`Cookie:` and `Set-Cookie:` headers containing a cookie pair are detected before URL and assignment sanitization and redact the complete diagnostic. Detection is bounded by an explicit header label plus `name=value`, and works when the header occurs inside surrounding diagnostic prose.

The policy is:

```text
Cookie: one pair                 -> fully redacted
Set-Cookie: one pair + flags     -> fully redacted
raw two-or-more cookie pairs     -> fully redacted
one arbitrary name=value string  -> retained unless cookie provenance is explicit
```

The last case avoids classifying every ordinary assignment as a cookie while keeping explicit cookie-bearing surfaces fail-closed.

### Independent adversarial evidence

```text
quoted JSON secret removed: true
quoted Python-style secret removed: true
quoted cloud secret removed: true
Set-Cookie secret removed: true
Failure/state/CLI secret removed: true
safe sibling context preserved: true
single raw pair policy verified: true
state and CLI valid JSON: true
evidence-tree secret scan: pass
```

Raw canary values are not recorded in this report.

### Exact persisted rewatch

```text
CDP /json/version HTTP: 200
request: 3c2de378…2dd0
exit: 0
state: COMPLETE
response: DEV3_OK
stderr: 0 bytes
```

No new frontend Send was issued.

### Automated evidence

```text
Python 3.11.15: 325 passed, 5 xfailed
Python 3.12.13: 325 passed, 5 xfailed
Python 3.14.0:  325 passed, 5 xfailed
```

The five xfails remain immutable prototype characterization tests. Existing hard-cancellation and external-schema limitations are unchanged.

## Replacement DEV turn 10 — escaped quoted values and HTTP-token cookie names

Date: 2026-07-26

### Escape-aware quoted values

Quoted secret values now match:

```text
double quote, then zero or more escaped-character or nonquote/nonbackslash units, then true double quote
single quote, then zero or more escaped-character or nonquote/nonbackslash units, then true single quote
```

A backslash consumes the following character as content. Therefore an escaped quote cannot terminate the secret value early. The replacement still preserves the original matching quote characters around `<redacted>`.

Coverage includes:

```text
valid JSON value containing an escaped double quote
zero through four literal backslashes before an embedded quote
escaped quote followed by additional secret words
Python-style single-quoted value containing escaped single quotes
safe sibling assignment after the true closing quote
unterminated single/double quote and trailing-backslash fail-closed cases
nested diagnostic
Failure serialization
durable state
CLI JSON stdout/stderr
```

The JSON cases are generated by `json.dumps()` and re-parsed before and after sanitization. Sanitized JSON resolves to the expected bounded object with the secret value replaced and the nonsecret sibling unchanged.

### HTTP-token cookie-name grammar

Explicit cookie headers and raw multi-pair cookie detection now use the HTTP token alphabet:

```text
letters, digits, and ! # $ % & ' * + - . ^ _ ` | ~
```

Independent tests additionally prove `http.cookies.SimpleCookie` accepts the tested names containing:

```text
+
$
!
^
|
~
```

Every corresponding `Set-Cookie:` diagnostic becomes exactly `<redacted>` before assignment processing. The documented raw single `name=value` case without cookie provenance remains visible.

### Independent adversarial evidence

```text
escaped JSON secret removed: true
escaped Python-style secret removed: true
token-punctuation cookie names removed: true
Failure/state/CLI secret removed: true
safe sibling context preserved: true
single raw-pair policy preserved: true
state and CLI valid JSON: true
evidence-tree secret scan: pass
```

Raw canary values are not recorded in this report.

### Exact persisted rewatch

```text
CDP /json/version HTTP: 200
request: 3c2de378…2dd0
exit: 0
state: COMPLETE
response: DEV3_OK
stderr: 0 bytes
```

No new frontend Send was issued.

### Automated evidence

```text
Python 3.11.15: 347 passed, 5 xfailed
Python 3.12.13: 347 passed, 5 xfailed
Python 3.14.0:  347 passed, 5 xfailed
```

The five xfails remain immutable prototype characterization tests. Existing hard-cancellation and external-schema limitations are unchanged.
