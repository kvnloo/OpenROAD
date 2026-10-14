#!/usr/bin/env python3
"""Fork-only native Bazel validation; no fabricated outputs or upstream writes."""
import hashlib
import json
import os
from pathlib import Path
import shutil
import subprocess
import time
import xml.etree.ElementTree as ET

SOURCE = Path('src/gpl/src/timingBase.cpp')
TEST = Path('src/gpl/test/timing_base_test.cpp')
TARGET = '//src/gpl/test:timing_base_test'
EXPECTED = {
    'TimingNetWeight.TiedAnchorSaturatesMoreCriticalTail',
    'TimingNetWeight.TiedPercentilePreservesTailAcrossListSizes',
}


def verify_result(xml_text, returncode, count, expected_failures):
    root = ET.fromstring(xml_text)
    cases = list(root.iter('testcase'))
    names = [c.get('classname', '') + '.' + c.get('name', '') for c in cases]
    failures = {name for name, case in zip(names, cases)
                if case.find('failure') is not None or case.find('error') is not None}
    if len(cases) != count or len(set(names)) != count:
        raise RuntimeError(f'Expected {count} unique native cases; got {names}')
    if any(c.find('skipped') is not None or c.get('status') == 'notrun' for c in cases):
        raise RuntimeError('Skipped or disabled native test')
    if failures != expected_failures:
        raise RuntimeError(f'Unexpected failures: {failures}; expected {expected_failures}')
    wanted_exit = 3 if expected_failures else 0
    if returncode != wanted_exit:
        raise RuntimeError(f'Bazel exit {returncode}; expected {wanted_exit}')
    return {'tests': count, 'failures': sorted(failures), 'exit_code': returncode}


def main():
    root = Path(subprocess.check_output(['git', 'rev-parse', '--show-toplevel'], text=True).strip())
    os.chdir(root)
    evidence = root / 'qa-evidence'
    evidence.mkdir(exist_ok=True)
    baseline = SOURCE.read_bytes()
    pins = {str(SOURCE): 'a26775465b014d2ba787ed86502c2332ebf548b6',
            str(TEST): 'ba30cbe27091df92c50d24b1fb9c84ea8b956afd'}
    for name, expected in pins.items():
        actual = subprocess.check_output(['git', 'hash-object', name], text=True).strip()
        if actual != expected:
            raise RuntimeError(f'Source pin mismatch: {name}: {actual}')
    provenance = {'reviewed_pr': 11727, 'reviewed_head': '1c84603cfed44245e0ca517ebe89570d3e68a749',
                  'qa_commit': subprocess.check_output(['git', 'rev-parse', 'HEAD'], text=True).strip(),
                  'baseline_blobs': pins, 'target': TARGET, 'test_result_cache': False,
                  'full_flow_qor_executed': False, 'phases': {}}

    def save():
        (evidence / 'assessment.json').write_text(json.dumps(provenance, indent=2) + '\n')

    def phase(name, count, expected_failures):
        testdir = root / 'bazel-testlogs/src/gpl/test/timing_base_test'
        for filename in ('test.xml', 'test.log'):
            path = testdir / filename
            if path.exists():
                path.unlink()
        command = ['bazelisk', 'test', '--jobs=2', '--disk_cache=',
                   '--remote_upload_local_results=false', '--nocache_test_results',
                   '--test_output=all', f'--build_event_json_file={evidence / (name + ".bep.json")}', TARGET]
        started = time.time()
        with (evidence / (name + '.console.log')).open('w') as log:
            result = subprocess.run(command, stdout=log, stderr=subprocess.STDOUT, check=False)
        print((evidence / (name + '.console.log')).read_text(), flush=True)
        recorded = {'command': command, 'exit_code': result.returncode,
                    'elapsed_seconds': round(time.time() - started, 3)}
        provenance['phases'][name] = recorded
        save()
        xml = testdir / 'test.xml'
        if not xml.is_file():
            raise RuntimeError(f'{name}: no native test XML; build/infrastructure failure')
        for filename in ('test.xml', 'test.log'):
            shutil.copyfile(testdir / filename, evidence / (name + '.' + filename))
        recorded.update(verify_result(xml.read_text(), result.returncode, count, expected_failures))
        binary = root / 'bazel-bin/src/gpl/test/timing_base_test'
        recorded['binary_sha256'] = hashlib.sha256(binary.read_bytes()).hexdigest()
        save()

    save()
    phase('baseline-original', 8, set())
    subprocess.run(['git', 'apply', '--check', 'qa/11727-tied-percentile.patch'], check=True)
    subprocess.run(['git', 'apply', 'qa/11727-tied-percentile.patch'], check=True)
    subprocess.run(['clang-format', '-i', str(SOURCE), str(TEST)], check=True)
    subprocess.run(['clang-format', '--dry-run', '--Werror', str(SOURCE), str(TEST)], check=True)
    subprocess.run(['git', 'diff', '--check'], check=True)
    changed = subprocess.check_output(['git', 'diff', '--name-only'], text=True).splitlines()
    if set(changed) != {str(SOURCE), str(TEST)}:
        raise RuntimeError(f'Unexpected production changes: {changed}')
    (evidence / 'formatted-candidate.patch').write_bytes(subprocess.check_output(['git', 'diff', '--', str(SOURCE), str(TEST)]))
    for path in (SOURCE, TEST):
        shutil.copyfile(path, evidence / path.name)
    candidate = SOURCE.read_bytes()
    SOURCE.write_bytes(baseline)
    phase('baseline-extended', 13, EXPECTED)
    SOURCE.write_bytes(candidate)
    phase('candidate', 13, set())
    phase('candidate-repeat', 13, set())
    provenance['native_validation'] = 'PASS'
    provenance['clang_format'] = subprocess.check_output(['clang-format', '--version'], text=True).strip()
    save()
    print('Native validation passed; no full-flow QoR validation was run.', flush=True)


if __name__ == '__main__':
    main()
