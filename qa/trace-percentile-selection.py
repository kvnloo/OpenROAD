#!/usr/bin/env python3
"""Supplemental #11727 GPL debug trace, not a golden regression or QoR gate.

Executes the same native regression fixture through Bazel with a temporary
GPL debug setting. An expected golden mismatch is preserved as evidence,
never mislabeled as a passing regression; the preceding unmodified A/B still
owns the native regression gate. Original Tcl and C++ bytes are restored.
"""
import importlib.util
import json
import os
from pathlib import Path
import re
import subprocess
import sys
import time

TEST = "simple01-td-net-percentage"
TCL = Path("src/gpl/test") / f"{TEST}.tcl"
TCL_BLOB = "0c74c1c50b1f8b99d1118e0e3151f9ce48012ded"
TARGET = f"//src/gpl/test:{TEST}-tcl_test"
RAMP = re.compile(r"Timing-driven: weight ramp (\S+) to (\S+), anchored on net (\d+) of (\d+)")
NET = re.compile(r"\bnet:(\S+)\s+slack:(\S+)\s+weight:(\S+)")


def overlay_tcl(contents: str) -> str:
    command = "global_placement -timing_driven"
    if contents.count(command) != 1 or "set_debug_level GPL timing" in contents:
        raise ValueError("Unexpected Tcl fixture: exactly one unmodified TD call required")
    return contents.replace(command, "set_debug_level GPL timing 1\n" + command, 1)


def parse_trace(log: str) -> list[dict]:
    """Parse actual GPL C++ debugPrint records (reported values may be rounded)."""
    events = []
    for line in log.splitlines():
        match = RAMP.search(line)
        if match:
            lo, hi = match.group(1, 2)
            events.append({
                "reported_min": lo,
                "reported_max": hi,
                "reported_tie": lo == hi,
                "anchor_one_based": int(match.group(3)),
                "selected_count": int(match.group(4)),
                "net_records": 0,
                "reported_below_anchor": 0,
                "below_anchor_above_unit_weight": 0,
            })
            continue
        match = NET.search(line)
        if match and events:
            event = events[-1]
            try:
                slack = float(match.group(2))
                weight = float(match.group(3))
                anchor = float(event["reported_min"])
            except ValueError as exc:
                raise ValueError(f"Non-numeric native GPL debug: {line}") from exc
            event["net_records"] += 1
            if slack < anchor:
                event["reported_below_anchor"] += 1
                if weight > 1.00001:
                    event["below_anchor_above_unit_weight"] += 1
    if not events or any(event["net_records"] == 0 for event in events):
        raise ValueError("Missing native GPL ramp or per-net trace; no activation claim")
    return events


def load_companion():
    script = Path(__file__).with_name("validate-percentile-placement.py")
    spec = importlib.util.spec_from_file_location("placement_qa", script)
    qa = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(qa)
    return qa


def probe(qa, arm: str, evidence: Path) -> dict:
    output = Path("bazel-testlogs/src/gpl/test") / f"{TEST}-tcl_test"
    original = TCL.read_bytes()
    instrumented = overlay_tcl(original.decode("utf-8"))
    for name in ("test.log", "test.xml"):
        (output / name).unlink(missing_ok=True)
        (evidence / f"trace-{arm}.{name}").unlink(missing_ok=True)
    command = ["bazelisk", "test", "--jobs=2", "--disk_cache=",
               "--remote_upload_local_results=false", "--nocache_test_results",
               "--test_output=all", TARGET]
    began = time.monotonic()
    try:
        TCL.write_text(instrumented, encoding="utf-8")
        with (evidence / f"trace-{arm}.console.log").open("w") as log:
            result = subprocess.run(command, stdout=log, stderr=subprocess.STDOUT,
                                    check=False)
    finally:
        TCL.write_bytes(original)
    record = {
        "command": command,
        "exit_code": result.returncode,
        "elapsed_sec": round(time.monotonic() - began, 3),
        "instrumented_tcl_sha256": qa.sha256(instrumented.encode()),
        "native_golden_regression": False,
        "classification": "TRACE_INVALID",
    }
    for name in ("test.log", "test.xml"):
        path = output / name
        if path.exists():
            raw = path.read_bytes()
            (evidence / f"trace-{arm}.{name}").write_bytes(raw)
            record[f"{name}_sha256"] = qa.sha256(raw)
    logpath = evidence / f"trace-{arm}.test.log"
    xmlpath = evidence / f"trace-{arm}.test.xml"
    if not logpath.is_file() or not xmlpath.is_file():
        record["classification"] = "MISSING_FRESH_NATIVE_OUTPUT"
        return record
    log = logpath.read_text(errors="replace")
    try:
        case = qa.parse_xml(xmlpath)
        record["native_xml"] = case
        record["metrics"] = qa.extract_metrics(log)
        record["ramps"] = parse_trace(log)
    except (ValueError, OSError) as exc:
        record["error"] = str(exc)
        return record
    golden_mismatch = "Log does not match golden file" in log
    record["golden_mismatch"] = golden_mismatch
    # The temporary debug output intentionally invalidates the original .ok;
    # this must not be represented as a passing regression.
    if case["skipped"] or (result.returncode != 0 and not golden_mismatch):
        record["classification"] = "UNEXPECTED_NATIVE_FAILURE"
    elif result.returncode == 0 and case["failure"]:
        record["classification"] = "UNEXPECTED_XML_FAILURE"
    elif result.returncode != 0 and not case["failure"]:
        record["classification"] = "UNEXPECTED_NONZERO_WITHOUT_XML_FAILURE"
    else:
        record["classification"] = "NATIVE_TRACE_CAPTURED_DIAGNOSTIC_ONLY"
    return record


def main() -> int:
    qa = load_companion()
    os.chdir(qa.git("rev-parse", "--show-toplevel"))
    evidence = Path("qa-evidence/percentile-placement")
    evidence.mkdir(parents=True, exist_ok=True)
    if qa.git("hash-object", str(qa.SOURCE)) != qa.BASE_SOURCE_BLOB:
        raise RuntimeError("Source is not the reviewed #11727 source blob")
    if qa.git("hash-object", str(qa.TEST_SOURCE)) != qa.BASE_TEST_BLOB:
        raise RuntimeError("Unit tests are not the reviewed #11727 blob")
    if qa.git("hash-object", str(TCL)) != TCL_BLOB:
        raise RuntimeError("Original native Tcl fixture changed")
    if subprocess.run(["git", "merge-base", "--is-ancestor", qa.BASE_HEAD,
                       "HEAD"], check=False).returncode != 0:
        raise RuntimeError("Reviewed upstream PR is not an ancestor")
    if qa.sha256(qa.PATCH.read_bytes()) != qa.EXPECTED_PATCH_SHA256:
        raise RuntimeError("Unexpected QA patch bytes")
    original_source = qa.SOURCE.read_bytes()
    original_test = qa.TEST_SOURCE.read_bytes()
    report = {
        "reviewed_upstream_head": qa.BASE_HEAD,
        "qa_head": qa.git("rev-parse", "HEAD"),
        "one_fixture_only": "gcd/Nangate45, 100% timing nets",
        "full_flow_qor": False,
        "diagnostic_only": True,
        "results": {},
    }

    def save():
        (evidence / "selection-trace.json").write_text(
            json.dumps(report, indent=2) + "\n")

    try:
        report["results"]["baseline"] = probe(qa, "baseline", evidence)
        save()
        if report["results"]["baseline"]["classification"] != "NATIVE_TRACE_CAPTURED_DIAGNOSTIC_ONLY":
            report["status"] = "BASELINE_TRACE_INCOMPLETE"
            save()
            return 1
        subprocess.run(["git", "apply", "--check", str(qa.PATCH)], check=True)
        try:
            subprocess.run(["git", "apply", str(qa.PATCH)], check=True)
            subprocess.run(["git", "diff", "--check"], check=True)
            report["results"]["candidate"] = probe(qa, "candidate", evidence)
            save()
        finally:
            qa.SOURCE.write_bytes(original_source)
            qa.TEST_SOURCE.write_bytes(original_test)
        good = report["results"]["candidate"]["classification"] == "NATIVE_TRACE_CAPTURED_DIAGNOSTIC_ONLY"
        report["status"] = "BOTH_TRACES_CAPTURED" if good else "CANDIDATE_TRACE_INCOMPLETE"
        report["potential_tied_tail_events"] = {
            arm: sum(1 for event in report["results"][arm].get("ramps", [])
                     if event["reported_tie"] and event["reported_below_anchor"] > 0)
            for arm in ("baseline", "candidate")
        }
        save()
        print(json.dumps({"status": report["status"],
                          "potential_tied_tail_events": report["potential_tied_tail_events"]},
                         indent=2))
        return 0 if good else 1
    finally:
        qa.SOURCE.write_bytes(original_source)
        qa.TEST_SOURCE.write_bytes(original_test)
        if qa.git("hash-object", str(TCL)) != TCL_BLOB:
            raise RuntimeError("Native Tcl fixture was not restored")


if __name__ == "__main__":
    sys.exit(main())
