# AGENTS.md

These rules apply to the entire repository.

- Work only on the explicitly authorized branch and preserve unrelated files.
- Use Python >=3.10, `uv`, Playwright, pytest, and the standard library unless a dependency is proven necessary.
- Use TDD for non-trivial behavior and keep transport, identity, graph resolution, persistence, locking, orchestration, and CLI code separate.
- Attach to persistent Chromium through CDP. Never call `browser.close()`.
- Use the real ChatGPT composer and Send/Stop controls. Never intercept, abort, mutate, fulfill, or replay frontend requests.
- Persist only allowlisted, recursively redacted state. Never print or store cookies, authorization values, access/resume/proof tokens, Sentinel, Turnstile, JWT-like values, or raw headers/bodies.
- Exact-turn proof is mandatory for success. Missing, conflicting, stale, or ambiguous identity fails closed and never permits an automatic duplicate Send.
- Do not modify the CDPA control-plane repository from this project.
