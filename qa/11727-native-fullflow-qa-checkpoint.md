# OpenROAD #11727 — downstream native full-flow gate

Prepared 2026-10-15 for `kvnloo/OpenROAD`, fork-only. Reviewed upstream PR head: `1c84603cfed44245e0ca517ebe89570d3e68a749`; pinned QA parent: `a553b5eb7f0dedb590dfc12935170ae595e376d2`. Intended QA branch: `qa/openroad-11727-fullflow-native-20261015`. No upstream publication.

## What this adds

- `qa/11727-fullflow-qor.py`: runs independent baseline and tied-percentile candidate OpenROAD executables through the real `test/flow.tcl` with **explicit timing-driven GPL activation**. Captures final WNS, TNS, area, DRVs, runtime and routing DB with source and binary digests. Executes one paired public case at a time or all three (gcd/Nangate45, gcd/SKY130HD, AES/Nangate45), never declares a partial matrix complete, bounds each OpenROAD process at 20 minutes by default, and rejects missing/mutated candidate patch bytes and incomplete full-flow output.
- `qa/test-11727-fullflow-qor.py`: isolated parser/orchestration checks only; mocked executables do **not** demonstrate native QoR.
- `.github/workflows/openroad-11727-fullflow-native-qa.yml`: fork-only, read-only CI permissions; checkout pinned to the QA branch, exact source/patch preflight, two genuine CLI Bazel install builds from same source base, restore source bytes, run gcd/Nangate45 A/B first and save artifacts even on failure. No secrets, paid instances, upstream writes or repository-protection changes.

## Executed evidence

Local Python tests **9/9 passed** after the route DB receipt hardening described below, including timeout fail-closed and single-design status isolation. `python -m compileall -q qa` passed; YAML parsed and all five shell steps passed `bash -n`. Patch application is separately checked. No Bazel, native OpenROAD full flow or representative QoR completed in the current sandbox: outbound Git DNS failed and GitHub's file-content write action was blocked. The QA branch was created but files were **not** pushed. No executable build receipt exists for the new gate.

## Required independent validation

1. From the QA branch at the exact pinned parent, apply this patch; run `python3 qa/test-11727-fullflow-qor.py -v`; review YAML, then make a human DCO-signed downstream commit. A push to the branch should trigger the fork-only native build and one gcd/Nangate45 full-flow pair. Inspect full logs even on failed CI; do not treat workflow syntax or mock tests as native evidence.
2. Inspect the raw `qor-summary.json` and native build receipts, binary digests, final routed DBs and DRVs. A successful **partial** A/B matrix is only the first staging gate. Expand to gcd/SKY130HD and AES/Nangate45 across both PDKs and repeat to assess variance. Report regressions and infrastructure failures.
3. Check whether this edge-case candidate actually changed the net weights in each full-flow case; identical QoR does not prove the tied-percentile code path was reached. Reconcile gudeh's nested-percentage semantics request before asking for independent upstream review.

**Credit:** rafaelmoresco — original percentile design and implementation; gudeh — semantics and safety review; OpenROAD maintainers — native flow, PDKs and coding/testing contract. Lane 5's QA instrumentation is AI-assisted and requires independent human review.

## 2026-10-15 follow-up: routed-DB provenance correction

Verified against the reviewed upstream source: `test/flow.tcl` calls
`make_result_file ${design}_${platform}_route.db`; **`test/helpers.tcl` adds
`-tcl` before the extension**. The correct emitted artifact is therefore
`${design}_${platform}_route-tcl.db`, not `_route.db`. The harness retains
that exact glob and now additionally requires the routed database to be
nonempty, records its byte count and SHA-256, and pins `helpers.tcl` blob
`98ea5adbfcbdd604865ef1d9b875eacdadcfedde`. The CI preflight checks
reviewed flow, helpers, and all three fixture blobs *before* the expensive
native builds. Under the old runner, a synthetic empty route DB with otherwise
passing log sentinels was incorrectly accepted; the new test fails on baseline
and passes after the check. Python: 9/9 tests passed; no new native flow run.

The fork's separate precommit security scan rejects standalone use of a
vendor name that had been reused as the QA baseline/candidate variable name.
The corresponding QA variable-renaming helper is provided with this handoff;
**do not disable or weaken the repository security hook**. The existing
failed workflow is a scan failure, not a native placement regression.

A downstream GitHub content update was attempted again and blocked by tool
safety checks. These files remain an **unpublished patch**, not a committed
branch implementation; no fabricated DCO signoff was inserted.

## 2026-10-15 setting-sweep extension (local-only, unexecuted natively)

The reviewed maintainer highlighted that a 2% anchor is a fraction of the
`-timing_driven_nets_percentage` subset, not the full constrained-net universe.
A single 100% run therefore cannot answer whether 10% or 50% behaves as
intended in user settings. The new runner accepts an explicitly recorded
`--timing-nets-percentage` (integer 1..100), injects that same value for both
native variants, and marks each summary `SINGLE_SETTING_ONLY_NOT_A_SWEEP`.
The fork-only first gate now uses **10%**; the subsequent 50% and 100%
comparisons must be run separately with distinct output directories, with
multiple public designs/PDKs and repetitions. This changes instrumentation,
not the original contributor's algorithm. Only local synthetic checks have
run; native QoR is still unmeasured. Existing scan policy must pass before
independent acceptance. Credit: rafaelmoresco (algorithm), gudeh (nested
percentage observation), OpenROAD maintainers (public flow/fixtures).
