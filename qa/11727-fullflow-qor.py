#!/usr/bin/env python3
"""Fork-only, native full-flow A/B gate for OpenROAD #11727.

Requires two independently Bazel-built CLI binaries from the SAME reviewed
OpenROAD base head, with the candidate containing ONLY the pinned QA patch.
Runs real test/flow.tcl (through temporary wrappers; original files unchanged).
This tool does not build binaries and does not claim QoR improvement.
"""

import argparse
import hashlib
import json
import math
import os
from pathlib import Path
import re
import subprocess
import sys
import tempfile
import time

PR_HEAD = "1c84603cfed44245e0ca517ebe89570d3e68a749"
FLOW_GIT_BLOB = "0de2353120425ac47356e552e014bd058b30f5bb"
FLOW_HELPERS_GIT_BLOB = "98ea5adbfcbdd604865ef1d9b875eacdadcfedde"
SOURCE_GIT_BLOB = "a26775465b014d2ba787ed86502c2332ebf548b6"
EXPECTED_PATCH_SHA256 = "847cf4e8ca39cc0a7d7b4fc4472a44de4fa45868e61add951a22e8f4bcc03139"
ENTRY_BLOBS = {
    "gcd_nangate45.tcl": "8d4082c49dfd01570b81eff219d62159330c4de3",
    "gcd_sky130hd.tcl": "87efe1cc513ff895e0c7d49be1168877c21a517e",
    "aes_nangate45.tcl": "eed864f1e028b9801bb44c83204f720d844f4b15",
}
DESIGNS = (
    ("gcd", "nangate45", "gcd_nangate45.tcl"),
    ("gcd", "sky130hd", "gcd_sky130hd.tcl"),
    ("aes", "nangate45", "aes_nangate45.tcl"),
)
FLOW_NEEDLE = (
    "# Global placement with placed IOs and routability-driven\n"
    "global_placement -routability_driven -density $global_place_density \\\n"
)
STATS = ("wns_max", "tns_max", "design_area", "route_drvs")
STATS_RE = re.compile(r"^LANE5_QOR\s+(wns_max|tns_max|design_area|route_drvs)=([^\s]+)$", re.M)


def sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def git_blob(path: Path) -> str:
    return subprocess.check_output(["git", "hash-object", str(path)], text=True).strip()


def instrument_flow(source: str, timing_nets_percentage: int = 100) -> str:
    """Activate the same bounded timing-net selection in both executable variants."""
    if (isinstance(timing_nets_percentage, bool)
            or not isinstance(timing_nets_percentage, int)
            or not 1 <= timing_nets_percentage <= 100):
        raise ValueError("Timing-driven net percentage must be an integer from 1 to 100")
    if source.count(FLOW_NEEDLE) != 1:
        raise ValueError("Expected exactly one reviewed routability-driven placement")
    if source.count("source $layer_rc_file") != 1:
        raise ValueError("Unexpected flow RC setup; investigate source change")
    td = (
        "# Lane 5: same RC model and timing-driven options in both arms.\n"
        "source $layer_rc_file\n"
        "set_wire_rc -signal -layer $wire_rc_layer\n"
        "set_wire_rc -clock -layer $wire_rc_layer_clk\n"
        + FLOW_NEEDLE.replace(
            "-routability_driven -density",
            "-routability_driven -timing_driven "
            f"-timing_driven_nets_percentage {timing_nets_percentage} "
            "-timing_driven_net_weight_max 5 -density",
        )
    )
    result = source.replace(FLOW_NEEDLE, td, 1)
    # Bound compute and make A/B scheduling less sensitive to host core count.
    # Both arms run with identical native OpenROAD thread settings.
    threads = "set_thread_count [cpu_count]"
    if result.count(threads) != 1:
        raise ValueError("Unexpected full-flow threading setup")
    result = result.replace(threads, "set_thread_count 2", 1)
    # Deliberately after DRT and extraction: record end-to-end QoR rather than
    # inferring it from placement-only helper tests or intermediate snapshots.
    result += (
        '\nputs "LANE5_QOR wns_max=[sta::worst_slack -max]"\n'
        'puts "LANE5_QOR tns_max=[sta::total_negative_slack -max]"\n'
        'puts "LANE5_QOR design_area=[rsz::design_area]"\n'
        'puts "LANE5_QOR route_drvs=[detailed_route_num_drvs]"\n'
    )
    return result


def parse_metrics(log: str) -> dict:
    found = {}
    for key, value in STATS_RE.findall(log):
        if key in found:
            raise ValueError(f"Duplicate end-of-flow QoR sentinel: {key}")
        try:
            val = float(value)
        except ValueError as e:
            raise ValueError(f"Invalid {key}={value}") from e
        if not math.isfinite(val):
            raise ValueError(f"Non-finite {key}={value}")
        found[key] = val
    missing = set(STATS) - found.keys()
    if missing:
        raise ValueError("No executed full-flow QoR: missing " + ", ".join(sorted(missing)))
    # STA represents some unconstrained/no-path states using very large finite
    # sentinels rather than IEEE infinity. Never report those as genuine QoR.
    if any(abs(found[key]) > 1e12 for key in ("wns_max", "tns_max")):
        raise ValueError("Unbounded STA timing sentinel; check design constraints")
    if found["route_drvs"] < 0 or found["route_drvs"] != int(found["route_drvs"]):
        raise ValueError("Invalid native route DRC count")
    if found["design_area"] <= 0:
        raise ValueError("Invalid native design area")
    return found


def run_one(binary: Path, wrapper: Path, test_dir: Path, out: Path, timeout_sec: int = 1200,
            *, expected_route_db: str) -> dict:
    out.mkdir(parents=True, exist_ok=False)
    results = out / "results"
    results.mkdir()
    command = [str(binary), "-no_init", "-no_splash", "-exit", str(wrapper)]
    env = os.environ.copy()
    env["RESULTS_DIR"] = str(results)
    # Native integration tests must run from test/ for the included PDK files.
    start = time.monotonic()
    timed_out = False
    with (out / "console.log").open("wb") as f:
        try:
            proc = subprocess.run(command, cwd=test_dir, env=env,
                                  stdout=f, stderr=subprocess.STDOUT,
                                  check=False, timeout=timeout_sec)
            exit_code = proc.returncode
        except subprocess.TimeoutExpired:
            timed_out = True
            exit_code = 124
    elapsed = time.monotonic() - start
    raw = (out / "console.log").read_text(errors="replace")
    route_dbs = sorted(results.glob("*_route-tcl.db"))
    exact_route_db = len(route_dbs) == 1 and route_dbs[0].name == expected_route_db
    route_db_size = route_dbs[0].stat().st_size if exact_route_db else None
    record = {
        "command": command, "binary_sha256": sha256(binary),
        "elapsed_sec": round(elapsed, 3), "exit_code": exit_code,
        "timed_out": timed_out, "timeout_sec": timeout_sec,
        "console_sha256": sha256(out / "console.log"),
        "timing_driven_iterations": len(re.findall(r"\[INFO GPL-0100\] Timing-driven iteration", raw)),
        "route_db_count": len(route_dbs),
        "expected_route_db": expected_route_db,
        "route_db_matches_expected": exact_route_db,
        "route_db_bytes": route_db_size,
        "route_db_sha256": sha256(route_dbs[0]) if exact_route_db and route_db_size > 0 else None,
        "classification": "UNVERIFIED",
    }
    try:
        record["metrics"] = parse_metrics(raw)
    except ValueError as e:
        record["error"] = str(e)
    if (not timed_out and exit_code == 0 and exact_route_db
            and route_db_size > 0
            and record["timing_driven_iterations"] > 0 and "metrics" in record):
        record["classification"] = "NATIVE_FULL_FLOW_PASS"
    else:
        record["classification"] = "TIMEOUT_INCOMPLETE" if timed_out else "INCOMPLETE_FULL_FLOW"
    return record


def compare(records: dict) -> dict:
    results = {}
    for name, arms in records.items():
        a, b = arms["baseline"], arms["candidate"]
        if any(r["classification"] != "NATIVE_FULL_FLOW_PASS" for r in (a, b)):
            results[name] = {"classification": "NOT_COMPARABLE"}
            continue
        results[name] = {
            "classification": "COMPARABLE_PAIR",
            "delta_candidate_minus_baseline": {
                key: b["metrics"][key] - a["metrics"][key] for key in STATS
            },
            "runtime_delta_sec": b["elapsed_sec"] - a["elapsed_sec"],
        }
    return results


def run(args) -> int:
    repo = args.repo.resolve()
    test_dir = repo / "test"
    source = test_dir / "flow.tcl"
    if git_blob(source) != FLOW_GIT_BLOB:
        raise ValueError("Reviewed flow.tcl changed; re-validate and re-pin")
    # helpers.tcl inserts '-tcl' into make_result_file() outputs, including
    # the routed database. Pin this contract before classifying receipts.
    if git_blob(test_dir / "helpers.tcl") != FLOW_HELPERS_GIT_BLOB:
        raise ValueError("Reviewed helpers.tcl changed; re-validate route DB naming")
    if git_blob(repo / "src/gpl/src/timingBase.cpp") != SOURCE_GIT_BLOB:
        raise ValueError("Not the reviewed #11727 GPL source blob")
    qa_patch = repo / "qa/11727-tied-percentile.patch"
    if (args.candidate_patch_sha256 != EXPECTED_PATCH_SHA256
            or not qa_patch.is_file()
            or sha256(qa_patch) != EXPECTED_PATCH_SHA256):
        raise ValueError("Candidate patch digest does not match native-tested QA patch")
    if args.timeout_sec < 1:
        raise ValueError("Each full-flow timeout must be positive")
    all_cases = {f"{platform}/{design}": (design, platform, entry)
                 for design, platform, entry in DESIGNS}
    selected = list(dict.fromkeys(args.case or all_cases))
    invalid = set(selected) - set(all_cases)
    if invalid:
        raise ValueError(f"Unknown public flow cases: {sorted(invalid)}")
    # Two different executables must be supplied: a patched source file is
    # insufficient evidence that the executable was actually rebuilt.
    binaries = {"baseline": args.baseline.resolve(), "candidate": args.candidate.resolve()}
    for binary in binaries.values():
        if not binary.is_file() or not os.access(binary, os.X_OK):
            raise ValueError(f"Missing executable native OpenROAD: {binary}")
    hashes = {variant: sha256(binary) for variant, binary in binaries.items()}
    if hashes["baseline"] == hashes["candidate"]:
        raise ValueError("Identical baseline/candidate binaries; not an executable A/B")
    # Reject invalid settings before writing an empty evidence directory.
    script = instrument_flow(source.read_text(), args.timing_nets_percentage)
    if args.output.exists():
        raise ValueError("Evidence directory already exists; refusing stale output reuse")
    args.output.mkdir(parents=True)
    report = {
        "upstream_pr": 11727, "reviewed_pr_head": PR_HEAD,
        "reviewed_flow_blob": FLOW_GIT_BLOB,
        "reviewed_helpers_blob": FLOW_HELPERS_GIT_BLOB,
        "instrumented_flow_sha256": hashlib.sha256(script.encode()).hexdigest(),
        "binary_sha256": hashes,
        "candidate_patch_sha256": args.candidate_patch_sha256,
        "binary_build_source_provenance": "REQUIRES_INDEPENDENT_BUILD_RECEIPT_CHECK",
        "native_full_flow": False,
        "timing_driven_nets_percentage": args.timing_nets_percentage,
        "percentage_sweep": "SINGLE_SETTING_ONLY_NOT_A_SWEEP",
        "test_config": f"TD {args.timing_nets_percentage}% selected; weight_max=5; same flow",
        "selected_cases": selected, "all_cases": list(all_cases),
        "full_matrix": len(selected) == len(all_cases),
        "per_run_timeout_sec": args.timeout_sec,
        "results": {},
    }
    def save():
        (args.output / "qor-summary.json").write_text(json.dumps(report, indent=2) + "\n")
    save()
    with tempfile.TemporaryDirectory(prefix="openroad-11727-flow-") as tmp:
        path = Path(tmp)
        overlaid = path / "flow.tcl"
        overlaid.write_text(script)
        for case in selected:
            design, platform, entry = all_cases[case]
            original = test_dir / entry
            if git_blob(original) != ENTRY_BLOBS[entry]:
                raise ValueError(f"Reviewed flow entrypoint changed: {entry}")
            before = original.read_text()
            marker = 'include -echo "flow.tcl"'
            if before.count(marker) != 1:
                raise ValueError(f"Reviewed design entrypoint changed: {entry}")
            wrapper = path / entry
            wrapper.write_text(before.replace(marker, f'include -echo "{overlaid}"'))
            name = f"{platform}/{design}"
            report["results"][name] = {}
            for variant, binary in binaries.items():
                dest = args.output / f"{platform}-{design}-{variant}"
                report["results"][name][variant] = run_one(
                    binary, wrapper, test_dir, dest, args.timeout_sec,
                    expected_route_db=f"{design}_{platform}_route-tcl.db")
                save()
    report["comparisons"] = compare(report["results"])
    if not all(v["classification"] == "COMPARABLE_PAIR"
               for v in report["comparisons"].values()):
        report["status"] = "INCOMPLETE_NATIVE_EVIDENCE"
    elif report["full_matrix"]:
        report["status"] = "COMPLETE_FOR_INDEPENDENT_QOR_REVIEW"
    else:
        report["status"] = "PARTIAL_QOR_MATRIX_NATIVE_PAIRS"
    report["native_full_flow"] = report["status"] != "INCOMPLETE_NATIVE_EVIDENCE"
    report["native_full_flow_matrix"] = (report["status"] ==
                                         "COMPLETE_FOR_INDEPENDENT_QOR_REVIEW")
    save()
    return 1 if report["status"] == "INCOMPLETE_NATIVE_EVIDENCE" else 0


def main() -> int:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--repo", required=True, type=Path)
    p.add_argument("--baseline", required=True, type=Path, help="Bazel-built CLI executable: reviewed PR head")
    p.add_argument("--candidate", required=True, type=Path, help="Bazel-built CLI executable: same head + pinned QA patch")
    p.add_argument("--candidate-patch-sha256", required=True, help="Pin exact candidate patch bytes")
    p.add_argument("--output", required=True, type=Path, help="Fresh durable raw-evidence directory")
    p.add_argument("--timing-nets-percentage", type=int, default=100,
                   help="Percentage of constrained nets selected by GPL (1..100). "
                        "Re-run each design at 10, 50, and 100 for semantics/QoR sweep")
    p.add_argument("--case", action="append", choices=[f"{p}/{d}" for d, p, _ in DESIGNS],
                   help="One representative flow; repeat for more. Default: all three")
    p.add_argument("--timeout-sec", type=int, default=1200,
                   help="Maximum wall seconds per OpenROAD process, default 1200")
    try:
        return run(p.parse_args())
    except (ValueError, OSError, subprocess.CalledProcessError) as e:
        print(f"[FAIL-CLOSED] {e}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    sys.exit(main())
