#!/usr/bin/env python3
"""Fork-only A/B of real OpenROAD GPL Tcl regressions (not multi-design QoR).

Run at the pinned PR head with Bazelisk, the same dependencies in both arms.
The candidate may legitimately diverge from a checked-in .ok/.defok golden;
that is a FAILED regression requiring diagnosis, not an automatic QoR win.
"""

import hashlib
import json
import os
from pathlib import Path
import re
import subprocess
import sys
import time
import xml.etree.ElementTree as ET

BASE_HEAD = "1c84603cfed44245e0ca517ebe89570d3e68a749"
BASE_SOURCE_BLOB = "a26775465b014d2ba787ed86502c2332ebf548b6"
BASE_TEST_BLOB = "ba30cbe27091df92c50d24b1fb9c84ea8b956afd"
EXPECTED_PATCH_SHA256 = "847cf4e8ca39cc0a7d7b4fc4472a44de4fa45868e61add951a22e8f4bcc03139"
SOURCE = Path("src/gpl/src/timingBase.cpp")
TEST_SOURCE = Path("src/gpl/test/timing_base_test.cpp")
PATCH = Path("qa/11727-tied-percentile.patch")
TESTS = ("simple01-td", "simple01-td-net-percentage", "simple01-td-tune")
METRICS = {
    "hpwl_um": r"Final HPWL \(um\):\s*([-+]?\d+(?:\.\d+)?(?:[eE][-+]?\d+)?)",
    "placed_area_um2": r"Final placement area:\s*([-+]?\d+(?:\.\d+)?(?:[eE][-+]?\d+)?)",
    "worst_slack": r"^worst slack max\s+([-+]?\d+(?:\.\d+)?(?:[eE][-+]?\d+)?)\s*$",
    "final_iteration": r"Global placement finished at iteration\s+(\d+)",
}


def sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def git(*args: str) -> str:
    return subprocess.check_output(["git", *args], text=True).strip()


def extract_metrics(log: str) -> dict:
    result = {}
    for name, expression in METRICS.items():
        matches = re.findall(expression, log, flags=re.MULTILINE)
        if not matches:
            raise ValueError(f"Missing required native log metric: {name}")
        result[name] = int(matches[-1]) if name == "final_iteration" else float(matches[-1])
    result["timing_driven_iterations"] = len(re.findall(
        r"\[INFO GPL-0100\] Timing-driven iteration\s+\d+\/\d+", log
    ))
    if result["timing_driven_iterations"] < 1:
        raise ValueError("No timing-driven iteration in native test output")
    return result


def parse_xml(xml_path: Path) -> dict:
    root = ET.parse(xml_path).getroot()
    cases = list(root.iter("testcase"))
    if len(cases) != 1:
        raise ValueError(f"Expected one Tcl regression testcase, got {len(cases)}")
    case = cases[0]
    skipped = case.find("skipped") is not None or case.get("status") == "notrun"
    failure = any(case.find(tag) is not None for tag in ("failure", "error"))
    return {"testcase": case.get("name"), "skipped": skipped, "failure": failure}


def run_arm(arm: str, evidence: Path) -> dict:
    all_results = {}
    for test in TESTS:
        target = f"//src/gpl/test:{test}-tcl_test"
        stem = f"{arm}-{test}"
        output = Path("bazel-testlogs/src/gpl/test") / f"{test}-tcl_test"
        # Remove old generated XML/log; prevent stale results from being reported.
        for name in ("test.xml", "test.log"):
            (output / name).unlink(missing_ok=True)
        cmd = ["bazelisk", "test", "--jobs=2", "--disk_cache=",
               "--remote_upload_local_results=false", "--nocache_test_results",
               "--test_output=all", target]
        started = time.monotonic()
        with (evidence / f"{stem}.console.log").open("w") as fh:
            proc = subprocess.run(cmd, stdout=fh, stderr=subprocess.STDOUT, check=False)
        result = {"target": target, "command": cmd, "exit_code": proc.returncode,
                  "elapsed_sec": round(time.monotonic() - started, 3)}
        all_results[test] = result
        for name in ("test.log", "test.xml"):
            path = output / name
            if path.is_file():
                data = path.read_bytes()
                (evidence / f"{stem}.{name}").write_bytes(data)
                result[name + "_sha256"] = sha256(data)
        log = evidence / f"{stem}.test.log"
        xml = evidence / f"{stem}.test.xml"
        if not log.exists() or not xml.exists():
            result["classification"] = "INFRASTRUCTURE_OR_BUILD_FAILURE"
            continue
        try:
            result["gtest"] = parse_xml(xml)
            result["metrics"] = extract_metrics(log.read_text(errors="replace"))
        except (ValueError, ET.ParseError) as err:
            result["classification"] = "INVALID_NATIVE_EVIDENCE"
            result["error"] = str(err)
            continue
        # Exit status is authoritative for golden/log/DEF success. Preserve
        # metrics for failed tests, but never call them passing regressions.
        result["classification"] = ("NATIVE_REGRESSION_PASS"
                                    if proc.returncode == 0
                                    and not result["gtest"]["failure"]
                                    and not result["gtest"]["skipped"]
                                    else "NATIVE_REGRESSION_FAILURE")
    return all_results


def main() -> int:
    repo = Path(git("rev-parse", "--show-toplevel"))
    os.chdir(repo)
    evidence = repo / "qa-evidence" / "percentile-placement"
    evidence.mkdir(parents=True, exist_ok=True)
    if git("hash-object", str(SOURCE)) != BASE_SOURCE_BLOB:
        raise RuntimeError("Not the reviewed GPL source blob; rebase the experiment")
    if git("hash-object", str(TEST_SOURCE)) != BASE_TEST_BLOB:
        raise RuntimeError("Not the reviewed GPL test blob; rebase the experiment")
    if subprocess.run(["git", "merge-base", "--is-ancestor", BASE_HEAD, "HEAD"], check=False).returncode != 0:
        raise RuntimeError("Reviewed PR head is not in this branch history")
    if not PATCH.is_file():
        raise RuntimeError("Missing pinned candidate patch")
    if sha256(PATCH.read_bytes()) != EXPECTED_PATCH_SHA256:
        raise RuntimeError("Candidate patch differs from previously native-tested bytes")
    baseline = SOURCE.read_bytes()
    original_test = TEST_SOURCE.read_bytes()
    record = {"upstream_pr": 11727, "reviewed_head": BASE_HEAD,
              "qa_head": git("rev-parse", "HEAD"), "source_blob": BASE_SOURCE_BLOB,
              "candidate_patch_sha256": sha256(PATCH.read_bytes()),
              "candidate_source_sha256": None,
              "design": "gcd/Nangate45 (same fixture in three variants)",
              "full_flow_qor": False, "results": {}}

    def save():
        (evidence / "assessment.json").write_text(json.dumps(record, indent=2) + "\n")

    save()
    record["results"]["baseline"] = run_arm("baseline", evidence)
    save()
    # Even when baseline fails, retain logs; do not interpret an infra failure
    # as a comparative QoR improvement.
    if any(x["classification"] != "NATIVE_REGRESSION_PASS"
           for x in record["results"]["baseline"].values()):
        record["status"] = "BASELINE_NOT_GREEN"
        save()
        return 1
    subprocess.run(["git", "apply", "--check", str(PATCH)], check=True)
    try:
        subprocess.run(["git", "apply", str(PATCH)], check=True)
        subprocess.run(["git", "diff", "--check"], check=True)
        changed = set(git("diff", "--name-only").splitlines())
        if changed != {str(SOURCE), str(TEST_SOURCE)}:
            raise RuntimeError(f"Candidate modified unexpected paths: {sorted(changed)}")
        record["candidate_source_sha256"] = sha256(SOURCE.read_bytes())
        record["results"]["candidate"] = run_arm("candidate", evidence)
        save()
    finally:
        SOURCE.write_bytes(baseline)
        TEST_SOURCE.write_bytes(original_test)
    record["status"] = ("BOTH_ARMS_PASS_GOLDEN"
                        if all(x["classification"] == "NATIVE_REGRESSION_PASS"
                               for x in record["results"]["candidate"].values())
                        else "CANDIDATE_REQUIRES_GOLDEN_TRIAGE")
    record["deltas"] = {}
    for test in TESTS:
        b = record["results"]["baseline"][test].get("metrics")
        c = record["results"]["candidate"][test].get("metrics")
        if b and c:
            record["deltas"][test] = {
                k: {"baseline": b[k], "candidate": c[k], "delta": c[k] - b[k]}
                for k in METRICS
            }
    save()
    print(json.dumps({"status": record["status"], "deltas": record["deltas"]}, indent=2))
    return 0 if record["status"] == "BOTH_ARMS_PASS_GOLDEN" else 1


if __name__ == "__main__":
    sys.exit(main())
