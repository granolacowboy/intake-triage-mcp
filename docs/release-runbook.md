# Release runbook

The public release workflow is intentionally **tag-only** and runs on a dedicated repository-scoped self-hosted runner labeled `triage-release`.

The central `[self-hosted, gcd]` runner belongs to `granolacowboy.dev` and is not part of the release trust boundary.

## Preconditions

Before creating a release tag:

1. Central portfolio verification for `intake-triage-mcp` is green for the exact server commit.
2. `server.json` declares the intended version.
3. The release commit has been reviewed and contains no client data, tenant configuration, or credentials.
4. A dedicated self-hosted runner is registered to this repository and exposes the `triage-release` label.
5. Docker, GitHub CLI, network access, and the runner service are healthy.

The workflow does not run on pull requests and does not support `workflow_dispatch`. Publication therefore requires an intentional `v*` tag.

## First release

For version `0.1.0`, verify the release commit and version before tagging:

```bash
git switch main
git pull --ff-only
git rev-parse HEAD
python - <<'PY'
import json
print(json.load(open("server.json"))["version"])
PY
```

The declared version must be `0.1.0`.

Create and push the annotated tag only after the exact commit has passed central verification:

```bash
git tag -a v0.1.0 -m "intake-triage-mcp v0.1.0"
git push origin v0.1.0
```

The workflow then performs unit tests, image build/push, non-root and MCP protocol smoke checks, CycloneDX SBOM generation, HIGH/CRITICAL vulnerability evidence, a fixable-CRITICAL failure gate, keyless Cosign signing/attestation, anonymous image-pull verification, MCP Registry publication, and GitHub Release evidence attachment.

## Evidence acceptance

Do not advertise the release as signed, attested, registry-published, or publicly pullable until the tag workflow completes successfully.

Retain or link the immutable release facts:

- Git commit
- release tag
- image digest
- SBOM SHA-256
- vulnerability report
- Cosign signature/attestation
- MCP Registry server name
- GitHub Release URL

If any release gate fails, fix the cause and create a new reviewed commit. Do not weaken the gate merely to obtain a green release.
