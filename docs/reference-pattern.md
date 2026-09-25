# Reusable MHSB MCP reference pattern

This repository is a concrete reference implementation, not a framework dependency. Reuse the **engineering contract** before reusing code.

The default strategy for a new public legal-platform MCP is:

1. copy the smallest useful pieces;
2. rename every domain concept explicitly;
3. delete behavior that does not apply;
4. re-establish invariants with repo-specific tests and evidence.

Prematurely extracting a shared framework would make safety behavior less visible and can couple unrelated vendor integrations.

## System boundary

Use models for language and judgment support. Use deterministic software for rules, records, gates, provenance, verification, and release evidence.

A public MCP in this family should make four boundaries obvious:

- **model behavior is nondeterministic**;
- **tool contracts and schema validation are deterministic**;
- **consequential writes are protected by explicit deterministic gates**;
- **human approval remains a first-class control where business or legal judgment is required**.

## Minimum repository contract

A serious repository should include, where applicable:

```
README.md
CHANGELOG.md
SECURITY.md
CONTRIBUTING.md
LICENSE
server.json
server.py / package entry point
requirements.txt / pyproject.toml
Dockerfile
tests/
evals/
docs/
  demo.md
  evidence/
.github/
  ISSUE_TEMPLATE/
  pull_request_template.md
  workflows/
```

Do not add files merely to satisfy a checklist. Each artifact should defend a concrete invariant.

## Deterministic server pattern

### Schemas first

- Validate every tool input at the process boundary.
- Reject unknown fields unless an extension surface is intentional.
- Bound strings, arrays, and nested objects that can affect memory, logs, prompts, or downstream APIs.
- Normalize canonical enums and identifiers before business logic.
- Return errors that are useful to both humans and tool-using models.

### Separate reads from writes

Read-only tools should be clearly identifiable in names, descriptions, and MCP annotations.

Write tools should:

- be few;
- declare the resource they mutate;
- accept idempotency material when the upstream API supports it;
- distinguish create, update, and destructive operations;
- return a stable record identifier plus provenance;
- never hide destructive behavior inside a broad convenience tool.

### Gate consequential actions

Encode policy in software, not only in prompts.

Examples include:

- conflicts completed before intake write;
- explicit matter or client identifier before document mutation;
- named-human approval before destructive actions;
- reason/rationale recorded for overrides;
- dry-run or preview before bulk changes.

A gate should be covered by a positive test and a negative test proving the unsafe path is refused.

## Provenance pattern

Every externally sourced or record-backed result should return enough metadata to answer:

- what source produced this value?
- which record or endpoint was used?
- when was it observed?
- which server revision produced the result?
- what transformation, if any, was applied?

For public fixtures, use fictional identifiers and datasets. Never commit client data, credentials, tenant configuration, or matter records.

## Evaluation contract

Use `intake-eval-harness` or an equivalent deterministic trace-aware harness.

A publishable evidence package should bind together:

- suite SHA-256;
- exact MCP server commit/tag/digest;
- exact harness commit;
- exact model identifier;
- UTC timestamp;
- execution configuration;
- Markdown report;
- JSON evidence;
- JUnit XML.

The suite should test the execution path, not just prose output. Prefer:

- required tools;
- forbidden tools;
- required-call argument subsets;
- tool-result assertions;
- tool ordering where order is safety-significant;
- maximum call counts;
- latency budgets only where operationally meaningful.

An LLM judge, if used, should be optional, provenance-rich, and subordinate to deterministic assertions wherever deterministic assertions are possible.

## Release contract

A release tag must match declared package/server version metadata.

Before tagging:

1. deterministic unit tests are green;
2. protocol smoke verification is green;
3. the exact commit is reviewed;
4. the public README does not claim artifacts that do not exist.

A release workflow should then produce or verify, as applicable:

- versioned image/package plus its immutable digest or artifact checksum;
- non-root runtime;
- protocol smoke test;
- SBOM;
- vulnerability findings;
- fail-closed critical-vulnerability policy;
- signature;
- attestation;
- anonymous/public artifact retrieval where publication is intended;
- registry publication;
- GitHub Release evidence bundle.

Do not hand-write a passing badge or release result.

## Central verification pattern

Where repository-local Actions cannot allocate runners, a central verifier may check out the target repository and execute its verification contract on a trusted self-hosted runner.

The target repo should link to the real verifier rather than retaining dead or permanently queued badges.

A central verifier is acceptable when it is explicit about:

- the repository and ref it checks out;
- the command it executes;
- the runner trust boundary;
- the absence of paid model calls;
- which checks remain release-only.

Native per-repository CI is preferable only when it is actually reliable and maintainable.

## Pull-request invariant checklist

Before merge, ask:

- Did this change broaden a write surface?
- Can the same operation be expressed as read/preview first?
- Are destructive actions explicit and separately gated?
- Are schemas stricter or looser than before?
- Can retries duplicate upstream state?
- Does new network behavior have timeout, retry, pagination, and rate-limit handling?
- Does any output now expose secrets, tenant data, or private matter data?
- Does the golden suite need a new required/forbidden tool assertion?
- Does release evidence still describe the artifact truthfully?
- Is a dependency major being treated as a compatibility project rather than a routine bump?

## Legal-platform MCP additions

For vendor-backed MCPs such as Lawmatics, Clio, or MyCase, add explicit coverage for:

- OAuth/API-key handling without logging secrets;
- scope and tenant boundaries;
- pagination;
- retry/backoff;
- rate-limit signaling;
- upstream error normalization;
- idempotency and duplicate prevention;
- record-version or last-updated checks where available;
- read/write permission separation;
- destructive-action approval;
- fixture/demo mode using fictional data;
- contract tests against captured/sanitized response shapes where permitted.

The goal is not an API wrapper. The goal is an inspectable operational control plane over the API.

## Proof-page pattern

A portfolio proof page should show one meaningful end-to-end scenario:

1. fictional input;
2. relevant read/tool call;
3. deterministic decision or validation;
4. attempted unsafe action;
5. refusal or approval gate;
6. trace-aware evaluation;
7. release/evidence provenance.

Prefer one defensible scenario over a broad feature tour.

## When to extract shared code

Extract a shared package only after at least two independent MCPs have demonstrated the same stable abstraction in production-like use.

Good candidates for later extraction may include:

- provenance envelopes;
- retry/rate-limit primitives;
- pagination helpers;
- idempotency helpers;
- evidence-manifest generation.

Keep vendor schemas, safety gates, permissions, and domain semantics local unless they are genuinely identical.
