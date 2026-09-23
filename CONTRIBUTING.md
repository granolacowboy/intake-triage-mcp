# Contributing

This repository is a reference implementation for deterministic legal-intake tooling. Contributions are welcome when they improve correctness, safety, provenance, interoperability, or clarity.

## Invariants

Changes should preserve these properties unless a PR explicitly proposes and justifies a breaking design change:

- no LLM calls in the server;
- no network calls in the server;
- Pydantic validation with unexpected fields rejected;
- conflict-screen results carry provenance;
- `conflicts_status='not-run'` cannot be logged without a named human override and rationale;
- the triage log is append-only;
- bundled fixtures remain fictional and contain no client or personal data.

## Development

```bash
python -m pip install -r requirements-dev.txt
pytest -q --cov=server --cov-report=term-missing
docker build -t intake-triage-mcp:dev .
```

For tool changes, include unit tests and update the golden suite in `evals/` (and the reusable `intake-eval-harness` example when applicable). A new tool should have a clear schema, deterministic behavior, explicit side-effect semantics, and an MCP annotation consistent with its actual behavior.

## Release-sensitive changes

Changes to `Dockerfile`, `server.json`, or `.github/workflows/publish-mcp.yml` affect the published OCI/MCP artifact. Keep `server.json` and release tags version-aligned. Do not weaken the smoke test, public-pull check, vulnerability gate, signature, or provenance steps without documenting the tradeoff.

Security issues should be reported through the account-level security policy rather than a public issue.
