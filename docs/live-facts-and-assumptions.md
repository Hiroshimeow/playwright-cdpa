# Live-tested facts and remaining assumptions

All facts below were observed against the existing persistent Chromium on CDP `127.0.0.1:9222` on July 25, 2026. Identifiers are truncated.

## Live-tested facts

- Chromium was reachable through CDP and exposed one persistent context with existing user-owned ChatGPT pages.
- `/api/auth/session` was available in the page and browser request contexts.
- Read-only authenticated GETs to the conversation graph and `stream_status` endpoints worked without copied cookie headers or copied auxiliary frontend headers.
- A sampled graph had a valid `current_node`, parent-linked current branch, and user/assistant/tool/system nodes.
- Graph metadata exposed `turn_exchange_id`, `request_id`, and `working_turn_id`.
- The final visible response was represented by the nearest current-branch `assistant`, recipient `all`, terminal text node.
- Frontend response `turn_exchange_id` and graph `turn_exchange_id` were different values for the same live request. They are separate namespaces.
- A fresh Send used a synthetic outgoing frontend parent while the materialized graph user node had a different graph parent.
- A reused conversation's pre-Send graph anchor, outgoing parent, and materialized graph parent agreed in the tested case.
- Default helper pages were closed after durable handoff while backend monitoring still completed.
- `--keep-helper-tab` retained exactly one helper page in the tested case.
- Concurrent read-only watchers returned the same request and response.
- A process killed after `DURABLE_HANDOFF` was recoverable from state, with exactly one matching user graph node.
- A competing immediate sender was rejected before creating another turn record.
- `--wait-idle` completed the active exact request before sending the next request.
- Stop was visible and clickable during both a long text turn and an observed tool-call phase.
- A completed tool flow contained a second internal user-role node directly below a tool result. That internal node and its descendants used a new graph turn/request namespace. Rewatch succeeded only after correlation validation was segmented at this exact tool-parented boundary.
- In the tested Stop cases, the backend ultimately reported a valid exact `COMPLETE`, not cancellation. The core therefore returned `COMPLETE` or, in a race, `cancellation_unproven`; it did not claim hard cancellation.

## Externally unstable contracts

These are current observations, not stable public APIs:

- ChatGPT DOM selectors.
- `/backend-api/f/conversation` response shape.
- Conversation graph and `stream_status` endpoint paths and schemas.
- Session endpoint token field names.
- Stream status vocabulary.
- Tool-call and tool-result graph representation.
- Meaning and lifetime of transport topic/turn identifiers.

Schema absence, drift, or ambiguity must continue to fail closed.

## Deliberate limitations

- The package uses undocumented ChatGPT Web behavior and requires continuous compatibility testing.
- Cross-process locking currently depends on POSIX `fcntl`; Windows support needs a separate proven lock primitive.
- A fresh uncertain click that yields no conversation identity cannot be automatically recovered or retried.
- An existing uncertain click with no frontend response can be reconciled only from one exact graph delta below the durable pre-Send anchor. When the graph remains unchanged, the request stays `UNKNOWN` and the conversation claim remains blocked.
- Prompt bodies are not persisted. Restart reconciliation therefore relies on durable identity and graph structure, not prompt text.
- Response bodies are not persisted. `watch` can re-read the exact graph response; `get` returns metadata only.
- A Stop click does not prove backend or tool cancellation.

## DEV turn 3 remediation facts

- Page-scoped `Target.getTargetInfo` returned a stable Chromium target ID in the existing CDP 9222 session.
- A live exact-target cleanup created two temporary pages, closed only the persisted owned target, left the unrelated target open, and recorded a durable close timestamp. Both temporary pages were removed before detaching.
- A fresh post-fix Send returned exact `DEV3_OK`, exit 0, stderr 0 bytes. Its schema-v4 record contained a helper target ID, `DURABLE_HANDOFF`, and a non-null helper close timestamp. A subsequent CDP enumeration found zero open pages matching that persisted target ID.
- Regression tests using real Playwright `TimeoutError` and `Error` classes prove that pre-click failures become external timeout/network failures, close their helper, release the claim, and allow a new request to claim the same conversation.
- A protocol-fake integration proves frontend acceptance followed by backend HTTP 429 leaves one exact helper open, then `watch` recovery closes that target only, leaves an unrelated page open, completes the turn, and releases ownership.


## DEV turn 4 strict-state facts

- Schema-v4 helper lifecycle values are no longer coerced. Malformed JSON types and invalid cross-field combinations are rejected as corrupt state.
- `watch` and `cancel` regression harnesses prove corrupt helper state is rejected before `BrowserSession` construction, page closure, durable close marking, or conversation claim mutation.
- A CLI corrupt-record proof returned exit 20 with category `corrupt_state`; the raw turn file and active conversation record were byte/field unchanged and stderr was empty.
- Schema-v2/v3 records cannot gain helper ownership from injected newer fields. Migration resets helper target to null, keep policy to false, and close marker to null.
- An existing valid schema-v4 request still rewatched successfully with exact response `DEV3_OK` after strict decoding was enabled.

## Replacement DEV turn 1 facts — July 26, 2026

- A fresh Send using an explicit shared coordination base and deployment ID completed with exact response `DEV1B_OK`, exit 0, and zero stderr bytes.
- A second process reused that conversation from a different repository-local `state_dir`; it completed with exact response `DEV1B_REUSE_OK`. The two local stores each contained one turn while the deployment had one shared conversation ownership record.
- During a deliberately long turn, a contender using a third local `state_dir` and the same coordination namespace exited 21 with category `ownership`, created zero local turn files, and did not send. The owner completed with `DEV1B_LONG_DONE` and released the shared claim.
- A `--wait-idle` sender using another local `state_dir` observed a foreign active claim, waited without reading the foreign turn store, then completed with exact response `DEV1B_WAIT_OK`. The shared claim was null afterward.
- The shared coordination record contained only `active_request_id`, `conversation_id`, `last_terminal_request_id`, `revision`, `schema_version`, and `updated_at`; it contained no local state path. Directory mode was `0700` and record mode was `0600`.
- The first fresh proof exposed a materialization race: the frontend accepted the Send while the new conversation graph still returned HTTP 404. The request remained `UNKNOWN` and exact `watch` later recovered `DEV1_OK` without duplicate Send. The backend snapshot contract was then corrected to represent graph 404 as an absent graph and allow bounded identity polling. A fresh post-fix Send completed directly with `DEV1B_OK`.
- The post-fix fresh helper record had a durable close timestamp, and a read-only CDP target enumeration found zero pages matching its persisted helper target ID.
- A scan of live stdout, stderr, local state, and shared coordination files found no bearer/JWT values, credential assignments, cookies, authorization material, Sentinel, Turnstile, or proof material.
- Strict schema regressions reject non-string persisted/transport/graph identifiers and missing or malformed assistant recipients. Cancellation regression evidence proves mutable same-message-ID terminal snapshots must converge before `COMPLETE` is accepted.

## Replacement DEV turn 2 review-remediation facts — July 26, 2026

- With `XDG_STATE_HOME=relative-xdg-state`, two clients started from different working directories resolved the same absolute fallback coordination root under the test HOME. A claim created in the first CWD caused an `ownership` conflict in the second; no CWD-local split occurred.
- Normal monitoring and cancellation now require consecutive exact terminal candidates. Regression sequences `COMPLETE → RUNNING → COMPLETE` and `COMPLETE → missing exact candidate → COMPLETE` no longer satisfy a two-sample convergence window; the following matching terminal sample is required.
- Credential-label normalization now covers snake_case, kebab-case, and camelCase in free-form assignments, structured keys, URL query keys, and URL path contexts. Exact regressions for `accessToken`, `session_token`, `api_key`, and `proof_material` contain no secret value after sanitization.
- Persisted turn and conversation records reject coercive booleans, integers, enums, hashes, timestamps, failure flags, response metadata, and invalid cross-field state/provenance combinations as `corrupt_state` before browser access.
- The pre-existing schema-v4 request `ce6fbaad…f0c1` rewatched on CDP 9222 with exit 0, exact response `DEV1_CURRENT_WAIT_OK`, and zero stderr bytes.
- A new real frontend Send completed with request `2a2630e2…5680`, exact response `DEV2_OK`, schema 4, `DURABLE_HANDOFF`, a persisted helper-close timestamp, zero matching open helper targets, and zero stderr bytes.
- The DEV-turn-2 evidence tree passed the credential/JWT assignment scan. Hard backend cancellation remains externally unproven; the implementation continues to report only exact COMPLETE, explicit CANCELLED, or cancellation-unproven outcomes.

## Replacement DEV turn 3 review-remediation facts — July 26, 2026

- Convergence fingerprints now hash exact allowlisted raw graph material in memory and expose only SHA-256 digests. Regression pairs whose token-shaped text would sanitize to the same output now produce different node and chain fingerprints; normal monitoring and cancellation require a fresh pair of identical raw terminal samples.
- `StateStore.load()` now requires equality among the requested ID, turn filename stem, and embedded `request_id`. Poisoned records fail as `corrupt_state` before `get`, `watch`, `recover`, `cancel`, browser construction, helper cleanup, state save, or coordination mutation. Discovery through `find_by_conversation()` uses the same checked load path.
- Free-form sanitization now routes generic normalized assignments through the shared secret predicate, including `*_secret`, `*_password`, kebab/camel/compact equivalents, and existing token/API-key families. Credential marker plus payload in one URL path segment redacts that segment and following path context while tested ordinary near-matches remain visible.
- Existing request `2a2630e2…5680` rewatched on CDP 9222 with exit 0, exact response `DEV2_OK`, and zero stderr bytes after these changes.
- A fresh real frontend Send completed as request `3c2de378…2dd0`, exit 0, exact response `DEV3_OK`, schema 4, `DURABLE_HANDOFF`, a persisted helper-close timestamp, zero open pages matching the helper target, and zero stderr bytes.
- A live poisoned-file CLI proof returned exit 20 and category `corrupt_state`; the raw file remained unchanged. Diagnostic and state-file proofs removed generic secret assignments and compound credential path payloads while preserving valid JSON.
- Hard backend cancellation remains externally unproven; Stop behavior continues to report only explicit cancellation, exact converged completion, or cancellation-unproven.

## Replacement DEV turn 4 secret-grammar facts — July 26, 2026

- The shared normalized secret predicate now classifies exact and qualified `credential`/`credentials` labels plus bounded private/signing-key forms across snake_case, kebab-case, camelCase, and compact variants.
- The same predicate is exercised by structured mappings, free-form assignments, URL query keys, URL path marker-plus-payload handling, nested diagnostics, `Failure`, atomic state writes, and CLI JSON.
- Path sanitization preserves explicit documentation/resource contexts such as credential guides, private-key formats, signing-key docs, and public-key resources while redacting compound credential/private/signing-key payload segments.
- Unquoted free-form assignment matching now treats `&` as a delimiter. Multiple query parameters remain independently sanitized, and a nonsecret parameter following secret parameters remains available as diagnostic context.
- Current full-suite result is `244 passed, 5 xfailed`. The five xfails remain immutable prototype characterizations.
- Existing exact request `3c2de378…2dd0` rewatched on CDP 9222 with exit 0, exact response `DEV3_OK`, and zero stderr bytes after this change.
- A local adversarial corpus proved credential, private-key, and signing-key values absent from diagnostics, structured JSON, nested diagnostics, `Failure`, state files, CLI stdout, and the entire evidence tree. State remained valid JSON; CLI returned the expected invariant exit 20 with zero stderr bytes.

## Replacement DEV turn 5 publication-minimization facts — July 26, 2026

- Five full 32-hex durable request identifiers and one full coordination namespace digest were removed from the committed live-acceptance report and replaced with consistent truncated forms.
- A new documentation regression scans root/docs/reference public Markdown for UUIDs and contiguous 24–64 character hexadecimal identifiers. The only full-length exception is an exact checksum already declared in `reference/SHA256SUMS`.
- The documentation gate initially reproduced seven findings and now passes with zero findings.
- Full suites pass on Python 3.11, 3.12, and 3.14 with `245 passed, 5 xfailed`.
- Existing exact request `3c2de378…2dd0` rewatched through CDP 9222 with exit 0, exact response `DEV3_OK`, and zero stderr bytes after the documentation-only change.

## Replacement DEV turn 6 version-agnostic identifier-gate facts — July 26, 2026

- The documentation UUID detector now checks the complete dashed `8-4-4-4-12` hexadecimal shape without restricting version or variant nibbles.
- Direct regressions cover version-4-shaped, version-6-shaped, version-7-shaped, nil-shaped, uppercase dashed identifiers, and 32-character contiguous hexadecimal identifiers.
- Negative controls confirm truncated prefix/suffix forms remain allowed.
- The exact immutable prototype checksum remains the only allowed full-length hexadecimal value; a one-character-altered checksum is rejected.
- The documentation-focused suite now contains 12 passing tests, and the full suite passes with `256 passed, 5 xfailed` on Python 3.11, 3.12, and 3.14.
- Existing exact request `3c2de378…2dd0` rewatched through CDP 9222 with exit 0, exact response `DEV3_OK`, and zero stderr bytes after this test-only correction.

## Replacement DEV turn 7 recovery and secret-boundary facts — July 26, 2026

- Existing-conversation uncertain recovery now consumes the complete persisted baseline fingerprint map. Each value must be an exact lowercase SHA-256 digest.
- Recovery compares a raw allowlisted projection of every baseline node. Newly appended non-baseline children are filtered from the projection so a legitimate Send can add its user node, while content, metadata, status, parent, original-child, or missing-node drift fails closed.
- Changed and missing baseline regressions leave the request `UNKNOWN`, keep `RETRY_PROHIBITED`, bind no user identity, preserve helper state, and retain the same shared owner revision.
- Persisted identity objects require the exact supported field set after explicit schema migration. Unknown or missing nested fields fail as `corrupt_state`; direct store, CLI, and `watch`/`recover`/`cancel` proofs preserve raw bytes and avoid browser or ownership mutation.
- The shared secret predicate now covers canonical secret-access-key, encryption-key, and passphrase families across snake_case, kebab-case, camelCase, and compact forms. The same boundary is tested for diagnostics, mappings, query/path contexts, `Failure`, durable state, and CLI JSON, while explicit documentation/help/format path contexts remain visible.
- An adversarial local corpus confirmed secret values absent from diagnostics, structured JSON, failures, state, CLI-visible surfaces, and the evidence tree. State remained valid JSON.
- Full suites pass on Python 3.11, 3.12, and 3.14 with `295 passed, 5 xfailed`.
- Existing exact request `3c2de378…2dd0` rewatched through CDP 9222 with exit 0, exact response `DEV3_OK`, and zero stderr bytes without a new Send.

## Replacement DEV turn 8 sibling-ambiguity and passphrase facts — July 26, 2026

- Uncertain structural recovery now enumerates immediate post-baseline user children of the durable anchor across the complete mapping. It requires exactly one total anchor child and requires that same node to be the only post-baseline user on the current branch.
- Public `watch` and `recover` regressions cover two sibling anchor children and multiple post-baseline users on one current chain. Ambiguous cases remain `UNKNOWN`, keep `RETRY_PROHIBITED`, bind no identity, preserve helper state, and retain the same shared owner revision. A public single-unique-user control completes and releases ownership.
- Unquoted secret assignment values no longer stop at whitespace. Multi-word passphrases are redacted through comma, semicolon, ampersand, newline, a following assignment boundary, or end of input; quoted values retain their quoted boundary.
- URL query values already reduced to redaction markers remain bounded, so safe prose after a sanitized URL is preserved. Cookie headers and multi-pair cookie blobs remain fully redacted, while semicolon-delimited nonsecret diagnostic context after one passphrase remains visible.
- An adversarial harness confirmed sibling recovery failed closed and retained ownership. A generated multi-word passphrase canary was absent from diagnostics, nested JSON, `Failure`, durable state, CLI stdout/stderr, and the evidence tree; state and CLI output remained valid JSON and safe context survived.
- Full suites pass on Python 3.11, 3.12, and 3.14 with `312 passed, 5 xfailed`.
- Existing exact request `3c2de378…2dd0` rewatched through CDP 9222 with exit 0, exact response `DEV3_OK`, and zero stderr bytes without a new Send.

## Replacement DEV turn 9 quoted-key and cookie-header facts — July 26, 2026

- The free-form assignment grammar now accepts bounded keys with no quote or with matching single/double quotes. The unquoted key text is passed to the existing normalized secret predicate; quoted values retain matching quote boundaries and safe sibling fields remain visible.
- Explicit `Cookie:` and `Set-Cookie:` headers containing a cookie pair are detected before URL or assignment transformations and redact the complete diagnostic, including embedded header text. Multi-pair raw cookie blobs remain fail-closed.
- A single arbitrary `name=value` string without explicit cookie provenance is intentionally not classified as a cookie because arbitrary diagnostic assignments are indistinguishable from cookie pairs. The fail-closed cookie boundary is explicit header provenance or multiple cookie pairs.
- Generated quoted JSON, Python-style, cloud-secret, and `Set-Cookie` canaries were absent from direct diagnostics, nested JSON, `Failure`, durable state, CLI stdout/stderr, and the evidence tree. Safe nonsecret sibling fields remained visible and state/CLI output remained valid JSON.
- Full suites pass on Python 3.11, 3.12, and 3.14 with `325 passed, 5 xfailed`.
- Existing exact request `3c2de378…2dd0` rewatched through CDP 9222 with exit 0, exact response `DEV3_OK`, and zero stderr bytes without a new Send.

## Replacement DEV turn 10 escape-aware value and cookie-token facts — July 26, 2026

- Quoted secret values now use an escape-aware grammar for both quote styles. A backslash plus following character is consumed as content, so escaped quotes do not terminate the value; the true closing quote remains the redaction boundary.
- Regression coverage generates valid JSON with zero through four literal backslashes before an embedded quote, plus a Python-style single-quoted value containing escaped single quotes. Unterminated single- and double-quoted secrets, including a trailing backslash, fail closed through the diagnostic remainder. Complete secret tails are removed and bounded nonsecret sibling fields remain intact for valid quoted values.
- Explicit `Cookie:` and `Set-Cookie:` recognition now uses the HTTP token alphabet for cookie names. Names containing `+`, `$`, `!`, `^`, `|`, and `~`, all accepted by `http.cookies.SimpleCookie` in the test environment, are fully redacted. Raw multi-pair cookie detection uses the same token alphabet.
- Generated escaped-quote and token-punctuation cookie canaries were absent from direct diagnostics, nested JSON, `Failure`, durable state, CLI stdout/stderr, and the evidence tree. State and CLI output remained valid JSON.
- Full suites pass on Python 3.11, 3.12, and 3.14 with `347 passed, 5 xfailed`.
- Existing exact request `3c2de378…2dd0` rewatched through CDP 9222 with exit 0, exact response `DEV3_OK`, and zero stderr bytes without a new Send.

## Replacement DEV turn 11 authorization and proof-boundary facts — July 26, 2026

- Historical turn-11 behavior redacted one physical authorization line and preserved a recognized semicolon suffix. Turn 13 removed the suffix exception, and turn 15 further supersedes the physical-line boundary by consuming folded continuation lines.
- The shared normalized secret predicate now covers signature, vendor signed-URL signature, OAuth `code_verifier`, and `client_assertion` families across snake_case, kebab-case, camelCase, and compact forms.
- Matching quoted assignment keys may contain `.`, `:`, and `/`; provider-qualified forms such as `"aws.secret_access_key"` are classified by the same predicate while safe sibling fields remain visible.
- Direct diagnostics, structured mappings, URL queries, nested JSON, `Failure`, atomic state writes, and CLI JSON are covered. Full suites pass with `389 passed, 5 xfailed`; no new frontend Send was needed because the remediation is local to redaction and output boundaries.

## Replacement DEV turn 12 structured proxy-authorization facts — July 26, 2026

- Structured mappings and matching quoted JSON/Python-style assignments now classify `Proxy-Authorization`, `proxy_authorization`, camelCase, and compact key forms as the same whole-value secret identity.
- Digest nonce/response, AWS4 Credential/Signature, Basic, and arbitrary proxy authorization schemes are removed before `Failure`, durable state, or CLI JSON output; bounded nonsecret sibling fields remain visible and JSON remains valid.
- The correction is limited to the shared secret-label predicate and cross-surface regressions. Transport, identity, graph, ownership, persistence schema, helper lifecycle, and CLI architecture are unchanged.

## Replacement DEV turn 13 header-boundary facts — July 26, 2026

- The turn 11 same-line semicolon suffix exception is superseded. Explicit `Authorization` and `Proxy-Authorization` provenance now redacts through newline or end of input; `retry`, `failed`, `failure`, `error`, `status`, `reason`, `request`, and `operation` parameters cannot terminate the secret boundary.
- Structured mappings and matching quoted JSON/Python-style assignments now classify exact and qualified authorization, proxy-authorization, cookie, cookies, and set-cookie final components across separator, snake_case, kebab-case, camelCase, and bounded compact header forms.
- Near matches such as `authorization_status`, documentation/schema labels, `reauthorization`, `cookie_policy`, `set_cookie_docs`, and `cookies_count` remain visible. The correction is local to redaction/output boundaries; transport, identity, graph, ownership, persistence schema, helper lifecycle, and CLI architecture are unchanged.

## Replacement DEV turn 14 compact-header facts — July 26, 2026

- Lowercase compact multi-level header keys such as `requestheadersauthorization`, `requestheadersproxyauthorization`, and `networkrequestheaderssetcookie` now use an allowlisted component decomposition instead of requiring one exact qualifier token.
- A compact compound prefix must be fully composed of approved request, response, network, direction, proxy, HTTP, and header components and must include `header` or `headers`. Existing one-level compact qualifiers remain supported.
- Negative controls including `reauthorization`, `authorizationstatus`, `marketingcookie`, `setcookiedocs`, `cookiescount`, and compact prefixes containing unapproved components remain visible. The change is limited to the shared redaction classifier and its public output/state boundaries.

## Replacement DEV turn 15 canonical header-boundary facts — July 26, 2026

- Explicit `Authorization` and `Proxy-Authorization` provenance now consumes the first physical line plus every immediately following SP/HTAB-prefixed continuation line. CRLF+SP, LF+TAB, multiple continuations, arbitrary parameter labels, and end-of-input continuations are removed while the next non-continuation line remains visible.
- Serialized quoted assignment keys now use an escape-aware parser with the same 256-character decoded bound and canonical normalization as real mapping keys. Bracket notation, escaped slash, Unicode escapes, padded keys, and long qualified keys converge; malformed/control-bearing/mismatched or over-bound assignment keys fail closed.
- Separated and camel-normalized authorization/cookie qualifiers now require a complete approved context prefix rather than an unrestricted final-component suffix. Required request/response/header contexts remain protected, while ordinary fields such as `marketing_cookie`, `customer_cookies`, `legal_authorization`, and `feature_authorization` remain visible.
- The correction is limited to the shared redaction classifier and output/state boundaries. Transport, identity, graph, monitor, state machine, ownership, locking, helper lifecycle, cancellation, backend access, and CLI architecture are unchanged.

## Replacement DEV turn 16 unmatched canonical-key facts — July 26, 2026

- Turn 16 introduced canonical decoding for a missing-close prefix, but its first-delimiter assumption was incomplete when `:` or `=` also appeared inside a qualified key. Turn 17 supersedes that delimiter rule by evaluating every bounded candidate position.
- An assignment-like unmatched key is preserved only when decoding succeeds and the canonical identity is provably nonsecret. Invalid escapes, control-bearing identities, over-bound prefixes, or canonical secret identities fail closed to `<redacted>` for the complete diagnostic.
- Direct diagnostics, nested JSON, `Failure`, atomic state writes, CLI JSON stdout/stderr, valid outer JSON, and invariant exit `20` are covered. The correction is local to redaction and output/state boundaries; all accepted transport, identity, graph, monitor, ownership, recovery, helper, cancellation, backend, and CLI architecture remains unchanged.

## Replacement DEV turn 17 unmatched delimiter-disambiguation facts — July 26, 2026

- Missing-close quoted assignments now evaluate all raw `:` and `=` positions on the physical line from longest to shortest. Each candidate prefix uses the same JSON/Python escape decoder and secret classifier as matched quoted keys, so internal separators cannot hide a later Unicode-escaped Authorization, Proxy-Authorization, or Set-Cookie component.
- Candidate analysis is bounded by the raw expansion ceiling implied by the 256-character decoded key limit. Any secret-equivalent, undecodable, control-bearing, or over-bound candidate fails closed for the complete diagnostic; preservation requires every plausible candidate to decode successfully as nonsecret.
- An 84-case matrix covers seven approved qualifier shapes, three escaped secret suffixes, colon/equal internal separators, and both quote styles. Representative nested, `Failure`, state, and CLI tests verify valid JSON, exit `20`, empty stderr, and zero canary leakage while a canonical nonsecret internal-separator control remains visible.
- The correction is local to redaction and public output/state boundaries. Transport, identity, graph, monitor, state machine, ownership, locking, recovery, helper lifecycle, cancellation, backend access, and CLI architecture are unchanged.

## Replacement DEV turn 18 canonical quoted-value facts — July 26, 2026

- Valid JSON object/list diagnostics now use a token-preserving quoted-value scanner. The outer JSON is first validated, existing quoted-key redaction is retained, each non-key string token is decoded with the standard JSON string grammar, and only changed value spans are re-encoded in place.
- Unicode-escaped `:` and `=` delimiters, escaped label characters, escaped slash, escaped quote, and backslash sequences therefore converge with ordinary Authorization, Proxy-Authorization, Cookie, and Set-Cookie text. Safe key order, whitespace, sibling fields, and ordinary values such as `legal_authorization: allowed`, `marketing_cookie: enabled`, and `authorization_status: denied` remain visible.
- Missing-close quoted lines with no raw delimiter now decode the bounded candidate content and apply the same secret-boundary predicate. Fully escaped Authorization, qualified Authorization, and Set-Cookie delimiters fail closed, while opening quotes that are provably assignment values retain the established unterminated-value handling.
- Direct diagnostics, nested serialization, `Failure`, atomic state, CLI JSON stdout/stderr, valid outer JSON, invariant exit `20`, and the existing malformed/over-bound/key-format regressions are covered. The correction changes only redaction and public persistence/output boundaries.

## Replacement DEV turn 19 recursive structured-diagnostic facts — July 26, 2026

- Turn 18's one-pass value scanner is superseded by a bounded JSON-grammar traversal. It distinguishes actual object keys from object values, root-list elements, and nested-array elements, so array strings are never routed through malformed-key classification.
- Real object keys retain canonical bounded decoding and secret classification. A secret key replaces only its complete associated JSON value; nonsecret object/list values are traversed recursively while original key order, whitespace, punctuation, and safe siblings remain intact.
- String values are sanitized across nested JSON-in-JSON and additional legal JSON escape layers. Each layer first attempts structured object/list handling, then the accepted unstructured boundary, then one recognized JSON escape decode; unchanged safe controls preserve their original representation. Processing stops at a fixed point or the shared depth limit of 12, and over-depth or structurally inconsistent input fails closed.
- Direct diagnostics, nested serialization, `Failure`, durable state, CLI JSON stdout/stderr, root lists, nested arrays, six nested/double-escaped Authorization/Proxy-Authorization/Set-Cookie cases, four negative controls, and an explicit depth-budget case are covered. The correction remains confined to redaction and public persistence/output boundaries.
