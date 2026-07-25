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
