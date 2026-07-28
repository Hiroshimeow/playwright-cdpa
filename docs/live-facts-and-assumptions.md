# Live facts and remaining assumptions

Date: 2026-07-27

Environment: existing persistent Chromium on loopback CDP `127.0.0.1:9222`. Identifiers below are truncated. No Chromium shutdown, request interception, request mutation, Retry, Regenerate, Continue, or Stop click was used by Send acceptance.

## Freshly observed facts

### Composer/Stop/Send behavior

A read-only probe across existing ChatGPT pages observed:

- editable empty composers with neither Send nor Stop;
- active-response pages with Stop visible, Send absent, and an editable empty composer;
- no false attachment markers from the current attachment detector.

A disposable long-response fixture then filled a second prompt while Stop was visible:

```text
initial_after_fill_send_ready = false
initial_after_fill_stop_visible = true
polls_until_send_ready = 601 at 50 ms
stop_visible_when_send_ready = false
sent_once = true
```

Therefore immediate steering with Send+Stop simultaneously visible was **not** observed in this live run. The supported live behavior is the honest wait path: preserve the exact prompt, do not click Stop, wait until the real Send button becomes enabled, revalidate, then send once.

The implementation still supports immediate steering if a future frontend state exposes one enabled real Send button while Stop remains visible; automated tests cover that branch.

### Public send/get behavior

Fresh public `send` completed with exact response `API_V2_LIVE_OK` and exit 0. Reusing the same completed conversation completed with exact response `API_V2_SECOND_OK` and exit 0.

The same conversation accepted a later prompt after a manual/non-core long response. The core returned `STEERED_API_V2_OK` with exit 0. The code path never calls Stop from Send; the separate direct UI fixture above proved that this frontend version withheld Send until Stop disappeared.

### Shared ownership

A foreign deployment-wide claim was held for three seconds from a separate process and different local state root. The public existing-conversation Send waited, then completed once with response `SHARED_OWNER_SERIALIZED_OK`. The foreign owner log showed one claim and one release. No contender turn file existed before claim in automated coverage.

### Concurrent active `get`

The first fresh concurrent observer run exposed a real defect: `get` attached during transient `SENT`, attempted identity recovery before the active sender finished durable binding, and returned `identity_missing`. The sender still completed.

The implementation was corrected to wait through transient `PREPARING`/`SENT` before attach/reconciliation. The rerun began from observed `SENT`; sender and concurrent `get` both exited 0 with:

```text
response length = 2411
response SHA-256 prefix = 206c008627bd18ab
```

This proves both observers returned the same exact final response with zero additional Send.

### Exact page recovery

The one exact disposable conversation page was closed explicitly. Same-ID `get` on the completed request then opened/reused only the exact persisted conversation, returned the same response length/hash, and exited 0. The persistent browser was not closed.

### Client restart recovery

A restart fixture encountered a transient authenticated backend HTTP 429 after the irreversible boundary. The request remained `UNKNOWN` with disposition `get_required`; no resend occurred. A later same-ID `get` completed with exit 0 and response SHA-256 prefix `6b61ed2d7daf1f13`. The persisted accepted user message ID occurred exactly once in the conversation graph.

This is the intended external-failure behavior: retain the same durable identity and recover with `get`, never call `send` again.

## Automated facts

The suite covers:

- atomic wait-and-claim before local turn creation;
- one winning existing-conversation sender across competing state roots;
- ownership timeout with no local request record;
- immediate Send+Stop steering branch;
- Stop-only wait-until-Send branch;
- composer, attachment, page, owner, cancellation, and user-graph drift before the click boundary;
- no second Send after click-boundary timeout/crash/uncertainty;
- active and complete `get` paths;
- exact-page borrow/open and duplicate rejection;
- local-only `status` with no browser or coordination construction;
- strict legacy state decoding with no retired command or library aliases;
- secret redaction, forbidden operations, graph identity, restart, cancellation, packaging, and checksum/reference regressions.

## Remaining assumptions and limits

- The current public API is text-only. Attachment-bearing CDPA hops must not migrate until an exact immutable attachment contract is implemented and accepted.
- Immediate steering is frontend-dependent. It was not exposed by the 2026-07-27 live UI; the wait-until-Send path is the verified behavior.
- Cross-process locking uses POSIX `fcntl.flock`; Windows requires a different proven lock backend before native support.
- A remote service boundary is intentionally absent. Future loopback HTTP or MCP adapters must call the same Python API and must not duplicate state or browser logic.
- HTTP 429 remains an external condition. Same-ID `get` recovery was proven after the condition cleared; the core does not claim to eliminate service-side throttling.
