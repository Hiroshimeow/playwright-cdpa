# Architecture and reliability contract

## Boundaries

| Module | Responsibility |
|---|---|
| `connection.py` | Loopback CDP validation, attach/detach, exact Chromium page-target identity and closure |
| `frontend.py` | Authentication readiness and real composer/Send/Stop controls |
| `transport.py` | Passive observation and allowlisted frontend handoff reduction |
| `backend.py` | Authenticated read-only status and graph GETs |
| `models.py` | State machine, staged identity, local result records, and shared ownership records |
| `schema.py` | Exact, bounded, non-coercing identifier decoding |
| `storage.py` | Atomic JSON, revisions, and record locks |
| `coordination.py` | Deployment-wide conversation claims and mutation locks |
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

## Helper-page ownership

Immediately after creating a helper page, the core reads `Target.getTargetInfo` through a page-scoped CDP session and persists the exact Chromium target ID in turn-record schema v4. It also persists whether the original Send requested `keep-helper-tab` and whether the helper has been closed.

Lifecycle rules:

- A pre-click failure closes its owned helper while Playwright is still attached and releases the conversation claim.
- A post-click request without durable graph handoff retains the exact helper and its durable target ID.
- After `watch`, recovery, or cancellation proves an exact terminal turn, the core closes only the page whose target ID matches the durable record. It never closes by conversation URL, visible title, page ordering, or a single-page assumption.
- If the exact target no longer exists, such as after browser restart or prior cleanup, the record is marked closed without touching another page.
- `keep-helper-tab` is durable request policy; later recovery does not override it.
- Concurrent cleanup uses record revisions and is idempotent.
- Schema-v4 helper fields are decoded before any browser lifecycle decision. `helper_page_keep` must be exactly a JSON boolean; target ID must be null or 1–256 printable ASCII characters without spaces; close time must be null or a bounded timezone-aware ISO timestamp.
- All three schema-v4 helper fields are required. A keep policy or close timestamp without a target ID is corrupt state.
- Schema-v2/v3 records receive no helper ownership, even if untrusted extra helper fields are present; the core never invents a durable target during migration.
- Any malformed helper lifecycle record raises `CorruptStateError` before CDP connection, page closure, cleanup marking, or conversation ownership mutation.

A normal Playwright timeout/error before `CLICK_BOUNDARY_ENTERED` is classified as an external timeout/network failure. The exact claim is released because no Send can have occurred. Failures after the click boundary remain ambiguous and retain ownership.

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
9. Every assistant node has an explicit canonical recipient. The selected final is the nearest eligible terminal `assistant -> all` text node below the exact user node; tool-call assistants require an explicit non-`all` recipient.
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

Logging/observation fingerprints are independent from final-candidate resolution. A node with the same ID is re-evaluated whenever its fingerprint changes. Candidate stability is consecutive: a nonterminal status, absent graph, unresolvable exact candidate, or any change in exact raw allowlisted candidate/chain material resets both sample count and stability time. A later terminal observation starts a new convergence window. Fingerprints are SHA-256 digests over that raw in-memory material; redaction is deliberately not applied before hashing because it would make distinct credential-shaped responses collide. Raw graph material is never persisted or emitted by the fingerprint boundary.

## Persistence and ownership

- Repository-local turn results and deployment-wide conversation ownership are separate persistence planes.
- Turn and conversation state use strict versioned JSON schemas. Turn schema v4 validates the exact JSON type of enums, booleans, nonnegative integers, SHA-256 values, timezone-aware timestamps, identities, failures, response metadata, and helper ownership. Nested identity objects must contain exactly the supported current fields after explicit migration; unknown or missing fields are rejected and the raw record remains unchanged. It also validates target/identity, response/state, failure/state, and state/provenance combinations. Schema v2/v3 migration remains supported but malformed legacy values are rejected rather than coerced.
- All persisted identity and correlation fields are decoded as exact bounded strings. Objects, arrays, booleans, numbers, null where disallowed, whitespace-only values, and control characters are rejected rather than stringified.
- Writes use a temporary file in the same directory, file `fsync`, `os.replace`, then parent-directory `fsync`.
- Turn and conversation records carry monotonically increasing revisions. A public request ID is create-only and can never overwrite an existing record. Every turn load binds three identities before returning: requested ID, filename stem, and embedded `request_id`; any mismatch is preserved as corrupt state and no browser or ownership mutation follows.
- Record updates are serialized with `fcntl.flock`; state and coordination directories are private (`0700`) and files are `0600`.
- The shared coordination namespace defaults to the normalized loopback CDP endpoint. `localhost`, `127.0.0.1`, and `::1` aliases for the same scheme and port converge on one namespace. The coordination base is resolved to an absolute path during configuration validation. A relative `XDG_STATE_HOME` is not interpreted relative to process CWD; the core falls back to the absolute user-state directory. Embedders may provide an explicit deployment ID and an absolute coordination base.
- The coordination record contains no repository state path or turn payload. Clients with different local `state_dir` values still observe one active conversation owner.
- The shared mutation lock serializes claim/release transitions. The durable active owner then blocks competing sends across preflight, the real Send, identity binding, recovery, and monitoring without holding a long-lived process lock.
- Read-only watchers may run concurrently.
- Immediate competing sends fail with exit 21.
- `--wait-idle` polls the shared claim only. It never assumes that a foreign active request is readable in the current repository-local result store. A stale foreign claim remains fail-closed.

## Restart and uncertain outcomes

### Accepted request with transport identity

A new process loads the request, binds missing canonical graph identity from the exact persisted user message, and continues monitoring without Send.

### Existing-conversation click with no observed response

Recovery is allowed only when all of these hold:

- state is retry-prohibited after the click boundary;
- the durable conversation claim still names the exact request;
- a pre-Send current-node anchor exists;
- a complete pre-Send graph baseline exists and every persisted fingerprint is an exact lowercase SHA-256 digest;
- every baseline node remains present and its raw allowlisted projection matches the persisted digest; newly added non-baseline children are excluded from that comparison because a legitimate Send must append a child to the anchor;
- exactly one post-baseline user node is an immediate child of the durable anchor across the complete mapping;
- that same node is the only post-baseline user on the current branch; sibling, regenerated, off-current, or later current-chain user deltas remain ambiguous;
- that node carries canonical graph correlation.

No prompt-text fallback is used. A missing, reparented, content-changing, metadata-changing, or otherwise fingerprint-changing baseline fails before identity, state, helper lifecycle, or shared ownership is changed. Structural uniqueness is evaluated across the complete mapping, not inferred from `current_node`: zero or multiple anchor children, a unique child absent from the current branch, or multiple post-baseline current-chain users remain `UNKNOWN` and block reuse.

### Fresh click with no conversation identity

The core cannot safely discover the conversation. It remains `UNKNOWN`; no automatic resend occurs.

## Authentication and secrets

Backend graph reads are GET-only. The authenticated browser session endpoint supplies an access token in memory. The token is used only in an in-memory Authorization header and is never returned by public results, serialized, or logged.

Serialization is allowlist-first and recursively redacts:

- generic token keys and `*_token` keys;
- cookies;
- complete explicit Authorization and Proxy-Authorization header values for every scheme, plus standalone Bearer and Basic values;
- access/refresh/resume/session tokens;
- Sentinel, Turnstile, and proof material;
- JWT-like strings;
- passwords, passphrases, secrets, API keys, canonical secret-access-key labels, and encryption keys;
- credential-bearing URL userinfo, sensitive query parameters, fragments, and secret-bearing path segments. Secret labels are normalized across snake_case, kebab-case, camelCase, and compact forms. The shared predicate covers generic `*_token`, `*_secret`, `*_password`, `*_passwd`, `*_api_key`, `*_credential`, and `*_credentials` families plus bounded private/signing/encryption-key, secret-access-key, and passphrase forms, as well as known access/refresh/resume/session/proof/Sentinel/Turnstile labels. A credential marker joined to payload in one path segment causes the whole segment and following path context to be redacted; explicit documentation/resource suffixes and ordinary near-matches remain visible. Free-form secret assignments accept bounded unquoted keys and matching single- or double-quoted JSON/Python-style keys before `:` or `=`; quoted keys may contain provider separators `.`, `:`, and `/`, and only the unquoted key text is classified by the shared predicate. Signature, vendor signed-URL signature, OAuth `code_verifier`, and `client_assertion` families are normalized across snake_case, kebab-case, camelCase, and compact forms. Explicit Authorization and Proxy-Authorization headers are handled before assignment scanning: the complete line-bounded header value is redacted regardless of scheme, with only a bounded semicolon-delimited diagnostic suffix preserved. Unquoted values consume until an explicit comma, semicolon, ampersand, newline, independently recognized following assignment, or end of input. Whitespace alone is part of the secret value, which prevents multi-word passphrase tails from surviving. Quoted values use an escape-aware grammar: each backslash plus following character is consumed as content, so escaped quotes and arbitrary backslash runs cannot terminate the value early; the true matching quote remains the boundary around `<redacted>`. If no true closing quote exists, the secret assignment consumes the remainder of the diagnostic fail-closed. Query parameters already sanitized by the URL boundary are not expanded into following prose. Explicit `Cookie:` and `Set-Cookie:` headers use the RFC-style HTTP token alphabet for cookie names and are fully redacted before assignment parsing, including names with punctuation such as `+`, `$`, `!`, `^`, `|`, and `~`; multi-pair raw cookie blobs use the same name grammar and remain fully redacted. A single arbitrary `name=value` string without explicit cookie provenance is retained as bounded diagnostic context rather than guessed to be a cookie.

Free-form exception and diagnostic strings pass through the same bounded sanitizer. Unexpected exceptions expose only the exception type at the public boundary. Corrupt-state diagnostics identify the record and parser exception type without preserving or chaining raw parser text.

Raw request headers, raw response headers, raw request bodies, raw response bodies, and session JSON are not persisted.

## Cancellation contract

- Before Send: the owned request becomes `CANCELLED` without browser activity. If a dedicated helper already filled the draft, the cancellation revision makes the sender's pre-click compare-and-swap fail; the sender closes its own helper without crossing the click boundary.
- Active turn: the core validates the exact branch before opening the exact conversation and clicking the real Stop control.
- `CANCELLED` is persisted only when the backend status explicitly reports cancellation.
- If the backend reaches a valid exact final, the request becomes `COMPLETE`, even if Stop was clicked. Cancellation reuses the same mutable-node graph convergence tracker as normal monitoring. Terminal samples must be consecutive; an intervening RUNNING status, absent graph, or missing exact candidate resets convergence.
- If neither cancellation nor an exact final can be proven, exit 23 is returned with `cancellation_unproven`; hard backend cancellation is not claimed.
- Cancelling an already terminal request is idempotent.

## Failure classes

External failures are separated from local invariant failures. Categories include browser offline, authentication required, frontend readiness, network/backend failure, schema drift, unsupported operation, timeout, ambiguous outcome, missing/conflicting identity, graph ambiguity/convergence, corrupt state, ownership, and unproven cancellation. Backend HTTP 429/5xx responses are classified as recoverable external outages; an explicit terminal turn status remains a terminal backend failure.
