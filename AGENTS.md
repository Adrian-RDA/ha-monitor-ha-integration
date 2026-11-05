# Repository instructions

- Work only in this repository. Do not modify the Laravel application.
- Read `docs/PROTOCOL.md` before changing transport, credentials, or payloads. The implemented Laravel contract is authoritative.
- Use Home Assistant's async APIs and shared verified HTTP session. Never disable TLS verification or follow redirects with credentials.
- Keep tokens and pairing codes out of logs, diagnostics, entity attributes, and committed fixtures. Persist only the agent token in config entry data; never persist the pairing code.
- Keep buffers, requests, and retry rates bounded. Every new observation gets a UUID; retries preserve observation IDs and timestamps.
- Test against real Home Assistant APIs with a simulated backend. Run `.venv/bin/pytest`, `.venv/bin/ruff check .`, `.venv/bin/ruff format --check .`, and `.venv/bin/python scripts/validate_repository.py`.
- Keep English and German translations complete. Version changes must update the manifest and `const.py` together.
- Do not commit, publish, or change remote infrastructure unless requested.
