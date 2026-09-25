# Changelog

All notable changes to this project are documented here.

Release tags must match server.json. The release workflow records the OCI digest and supply-chain evidence.

## Unreleased

### Added
- adversarial regression tests for hostile-looking input, oversized fields, invalid override metadata, and write I/O failures;
- explicit bounds on party names, checked parties, and follow-up field names;
- end-to-end safety demo and evaluation-evidence policy;
- issue forms for bugs and tool-behavior proposals.

### Changed
- conflicts override metadata is valid only when conflicts_status is not-run;
- agent evaluation is delegated to the reusable intake-eval-harness rather than maintaining a divergent local copy.

## 0.1.0

Initial deterministic legal-intake MCP reference implementation. This documents the intended first public release; a GitHub Release is not claimed until the corresponding tag and release workflow complete.
