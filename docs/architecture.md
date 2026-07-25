# Architecture and reliability contract

## Boundaries

| Module | Responsibility |
|---|---|
| `connection.py` | Loopback CDP validation, attach, Playwright detach |
| `frontend.py` | Authentication readiness and real composer/Send/Stop controls |
| `transport.py` | Passive observation and allowlisted frontend handoff reduction |
| `backend.py` | Authenticated read-only status and graph GETs |
| `models.py` | State machine, staged identity, result and conversation records |
| `storage.py` | Atomic state, revisions, record locks, conversation claims |
| `identity.py` | Namespace-aware identity merge and exact user-node binding |
| `graph.py` | Current-branch validation, tool-chain validation, final resolver |
| `monitor.py` | Stream polling and bounded graph convergence |
| `service.py` | Send, watch, recovery, wait-idle, cancellation orchestration |
| `api.py` | Public library exports |
| `cli.py` | Argument parsing, result rendering, stable exit-code mapping |

## Irreversible Send boundary

The Send sequence is deliberately ordered:

1. Persist `NEW`.
2. Claim the existing conversation when applicable.
3. Read the pre-Send graph and persist its node fingerprints and `current_node` anchor.
4. Insert and verify the prompt in the real composer.
5. Persist `PREPARING + CLICK_BOUNDARY_ENTERED` with `fsync`.
6. Click the real Send button inside `page.expect_response`.
7. Persist allowlisted request identity immediately after frontend response observation.
8. Reduce the short frontend response into transport identity.
9. Bind the exact user graph node and canonical graph identity.
10. Persist `DURABLE_HANDOFF + RUNNING`.
11. Close the helper page unless `--keep-helper-tab` is set.
12. Monitor the exact graph turn to convergence.

Once step 5 is durable, automatic resend is prohibited unless a separate future policy proves retry safety. Process restart does not reinterpret an uncertain Send as unsent.

## Identity namespaces

Live evidence showed that similarly named identifiers are not interchangeable. The persisted identity keeps them separate:

### Transport namespace

- `transport_turn_exchange_id`: observed in the frontend handoff response.
- `transport_request_id`: observed in the outgoing frontend payload when present.
- `stream_topic_id`: observed in frontend handoff options.
- `frontend_parent_message_id`: outgoing frontend parent value; fresh conversations may use a synthetic client root.

### Canonical graph namespace

- `turn_exchange_id`
- `request_id`
- `working_turn_id`
- `user_message_id`
- `parent_message_id`
- `pre_send_current_node`

Canonical graph fields are bound only from the exact user node and its current-branch ancestry. Transport IDs are never silently substituted for graph IDs.

## Exact final-response proof

`SUCCESS` requires all of the following:

1. Exact conversation identity.
2. Exact submitted user message identity.
3. At least one canonical graph correlation field.
4. The user node is on the graph's current branch.
5. Its graph parent matches the durable parent and, for reused conversations, the pre-Send graph anchor.
6. The submitted user node positively matches its persisted canonical graph identity. Within each graph segment, every present canonical turn/request field agrees.
7. A new graph segment is allowed only at a later user-role node whose immediate parent is a tool result; a later user node below an assistant is treated as a different human turn and rejected.
8. Tool calls have terminal tool results.
9. The selected final is the nearest eligible terminal `assistant -> all` text node below the exact user node.
10. Candidate and exact-chain fingerprints remain unchanged across the configured sample count and stability duration after stream completion.

History order, creation timestamps, latest-message heuristics, visible DOM text, and prior final responses are never success fallbacks.

Live tool execution showed that ChatGPT may append an internal user-role continuation after a tool result and assign that continuation a new `turn_exchange_id`, `request_id`, and `working_turn_id`. The resolver validates these as separate graph segments linked by exact ancestry. It never rewrites the persisted submitted-user identity or accepts a later user node whose parent is an assistant.

## Mutable graph handling

Message IDs are not treated as immutable completion markers. Fingerprints cover:

- node identity and parent/children;
- author and recipient;
- status;
- content;
- selected identity, model, reasoning, and tool metadata.

Logging/observation fingerprints are independent from final-candidate resolution. A node with the same ID is re-evaluated whenever its fingerprint changes.

## Persistence and ownership

- Turn and conversation state use strict versioned JSON schemas.
- Writes use a temporary file in the same directory, file `fsync`, `os.replace`, then parent-directory `fsync`.
- Turn and conversation records carry monotonically increasing revisions. A public request ID is create-only and can never overwrite an existing record.
- Record updates are serialized with `fcntl.flock`.
- A durable conversation record allows one active mutating request.
- Read-only watchers may run concurrently.
- Immediate competing sends fail with exit 21.
- `--wait-idle` watches the exact active request, then claims and revalidates the conversation before its own Send.

## Restart and uncertain outcomes

### Accepted request with transport identity

A new process loads the request, binds missing canonical graph identity from the exact persisted user message, and continues monitoring without Send.

### Existing-conversation click with no observed response

Recovery is allowed only when all of these hold:

- state is retry-prohibited after the click boundary;
- the durable conversation claim still names the exact request;
- a pre-Send current-node anchor exists;
- a complete pre-Send graph baseline exists;
- exactly one new current-branch user node appears below that anchor;
- that node carries canonical graph correlation.

No prompt-text fallback is used. Zero or multiple candidates remain `UNKNOWN` and block reuse.

### Fresh click with no conversation identity

The core cannot safely discover the conversation. It remains `UNKNOWN`; no automatic resend occurs.

## Authentication and secrets

Backend graph reads are GET-only. The authenticated browser session endpoint supplies an access token in memory. The token is used only in an in-memory Authorization header and is never returned by public results, serialized, or logged.

Serialization is allowlist-first and recursively redacts:

- generic token keys and `*_token` keys;
- cookies;
- authorization and bearer values;
- access/refresh/resume/session tokens;
- Sentinel, Turnstile, and proof material;
- JWT-like strings;
- passwords, secrets, and API keys.

Raw request headers, raw response headers, raw request bodies, raw response bodies, and session JSON are not persisted.

## Cancellation contract

- Before Send: the owned request becomes `CANCELLED` without browser activity. If a dedicated helper already filled the draft, the cancellation revision makes the sender's pre-click compare-and-swap fail; the sender closes its own helper without crossing the click boundary.
- Active turn: the core validates the exact branch before opening the exact conversation and clicking the real Stop control.
- `CANCELLED` is persisted only when the backend status explicitly reports cancellation.
- If the backend reaches a valid exact final, the request becomes `COMPLETE`, even if Stop was clicked.
- If neither cancellation nor an exact final can be proven, exit 23 is returned with `cancellation_unproven`; hard backend cancellation is not claimed.
- Cancelling an already terminal request is idempotent.

## Failure classes

External failures are separated from local invariant failures. Categories include browser offline, authentication required, frontend readiness, network/backend failure, schema drift, unsupported operation, timeout, ambiguous outcome, missing/conflicting identity, graph ambiguity/convergence, corrupt state, ownership, and unproven cancellation. Backend HTTP 429/5xx responses are classified as recoverable external outages; an explicit terminal turn status remains a terminal backend failure.
