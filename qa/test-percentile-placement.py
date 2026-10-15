#!/usr/bin/env python3
"""Unit checks for fork-only evidence parser, independent of OpenROAD availability."""
import importlib.util
from pathlib import Path
import tempfile
import os
from unittest.mock import patch
import subprocess
import unittest

module_path = Path(__file__).with_name('validate-percentile-placement.py')
spec = importlib.util.spec_from_file_location('placement_qa', module_path)
mod = importlib.util.module_from_spec(spec)
spec.loader.exec_module(mod)

class EvidenceTests(unittest.TestCase):
    def test_metrics_from_native_style_log(self):
        sample = '''[INFO GPL-0100] Timing-driven iteration 1/2, virtual: false.
[INFO GPL-0100] Timing-driven iteration 2/2, virtual: false.
[INFO GPL-1001] Global placement finished at iteration 402
[INFO GPL-1014] Final placement area: 601.79 (+0.13%)
[INFO GPL-1018] Final HPWL (um): 2617.69
worst slack max 1.37
No differences found.
'''
        value = mod.extract_metrics(sample)
        self.assertEqual(value['final_iteration'], 402)
        self.assertEqual(value['timing_driven_iterations'], 2)
        self.assertEqual(value['hpwl_um'], 2617.69)
        self.assertEqual(value['placed_area_um2'], 601.79)
        self.assertEqual(value['worst_slack'], 1.37)

    def test_missing_metric_must_fail(self):
        with self.assertRaisesRegex(ValueError, 'hpwl_um'):
            mod.extract_metrics('worst slack max 1.3')

    def test_missing_timing_driven_must_fail(self):
        log = '''[INFO GPL-1001] Global placement finished at iteration 402
[INFO GPL-1014] Final placement area: 601.79
[INFO GPL-1018] Final HPWL (um): 2617.69
worst slack max 1.37'''
        with self.assertRaisesRegex(ValueError, 'timing-driven'):
            mod.extract_metrics(log)

    def test_three_native_golden_metric_shapes(self):
        # Numeric values and iteration counts from pinned #11727 .ok fixtures;
        # not a substitute for actually running these Tcl regressions.
        cases = (
            ("simple01-td", 2587.54, 601.79, 1.38, 370, 2),
            ("simple01-td-net-percentage", 2617.69, 601.79, 1.37, 402, 2),
            ("simple01-td-tune", 2528.32, 600.99, 1.38, 368, 7),
        )
        for name, hpwl, area, wns, steps, td_count in cases:
            with self.subTest(name=name):
                events = "\n".join(
                    f"[INFO GPL-0100] Timing-driven iteration {i}/{td_count}, virtual: false."
                    for i in range(1, td_count + 1))
                text = (events + "\n" +
                        f"[INFO GPL-1001] Global placement finished at iteration {steps}\n" +
                        f"[INFO GPL-1014] Final placement area: {area:.2f} (+0.0%)\n" +
                        f"[INFO GPL-1018] Final HPWL (um): {hpwl:.2f}\n" +
                        f"worst slack max {wns:.2f}\n")
                got = mod.extract_metrics(text)
                self.assertEqual(got, {
                    "hpwl_um": hpwl,
                    "placed_area_um2": area,
                    "worst_slack": wns,
                    "final_iteration": steps,
                    "timing_driven_iterations": td_count,
                })

    def test_empty_xml_never_counts_as_pass(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "test.xml"
            path.write_text("<testsuite tests=\"0\"></testsuite>")
            with self.assertRaisesRegex(ValueError, "Expected one"):
                mod.parse_xml(path)

    def test_xml_single_valid_case(self):
        with tempfile.TemporaryDirectory() as tmp:
            x = Path(tmp) / 'test.xml'
            x.write_text('<testsuites><testsuite tests="1"><testcase name="a"/></testsuite></testsuites>')
            self.assertEqual(mod.parse_xml(x), {'testcase': 'a', 'skipped': False, 'failure': False})

    def test_xml_failure_detected(self):
        with tempfile.TemporaryDirectory() as tmp:
            x = Path(tmp) / 'test.xml'
            x.write_text('<testsuite><testcase name="a"><failure message="golden diff"/></testcase></testsuite>')
            self.assertTrue(mod.parse_xml(x)['failure'])

    def test_xml_multiple_cases_rejected(self):
        with tempfile.TemporaryDirectory() as tmp:
            x = Path(tmp) / 'test.xml'
            x.write_text('<testsuite><testcase name="a"/><testcase name="b"/></testsuite>')
            with self.assertRaisesRegex(ValueError, 'Expected one'):
                mod.parse_xml(x)


    def test_run_arm_fresh_native_logs_three_targets(self):
        fake = """[INFO GPL-0100] Timing-driven iteration 1/2, virtual: false.
[INFO GPL-1001] Global placement finished at iteration 400
[INFO GPL-1014] Final placement area: 601.79
[INFO GPL-1018] Final HPWL (um): 2617.69
worst slack max 1.37
"""
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            old = Path.cwd()
            os.chdir(root)
            try:
                evidence = root / 'evidence'
                evidence.mkdir()
                def create_native_results(cmd, stdout, stderr, check):
                    test = cmd[-1].split(':')[1].removesuffix('-tcl_test')
                    output = Path('bazel-testlogs/src/gpl/test') / f'{test}-tcl_test'
                    output.mkdir(parents=True, exist_ok=True)
                    (output / 'test.xml').write_text('<testsuites><testsuite><testcase name="native-test"/></testsuite></testsuites>')
                    (output / 'test.log').write_text(fake)
                    return subprocess.CompletedProcess(cmd, 0)
                with patch.object(mod.subprocess, 'run', side_effect=create_native_results):
                    results = mod.run_arm('baseline', evidence)
                self.assertEqual(set(results), set(mod.TESTS))
                self.assertTrue(all(r['classification'] == 'NATIVE_REGRESSION_PASS' for r in results.values()))
                self.assertTrue(all('test.log_sha256' in r and 'test.xml_sha256' in r for r in results.values()))
            finally:
                os.chdir(old)

    def test_stale_native_results_never_counted(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            old = Path.cwd()
            os.chdir(root)
            try:
                evidence = root / 'evidence'
                evidence.mkdir()
                for test in mod.TESTS:
                    output = Path('bazel-testlogs/src/gpl/test') / f'{test}-tcl_test'
                    output.mkdir(parents=True)
                    (output / 'test.xml').write_text('<testsuite><testcase name="old"/></testsuite>')
                    (output / 'test.log').write_text('stale')
                with patch.object(mod.subprocess, 'run', side_effect=lambda cmd, **kwargs: subprocess.CompletedProcess(cmd, 1)):
                    results = mod.run_arm('baseline', evidence)
                self.assertTrue(all(r['classification'] == 'INFRASTRUCTURE_OR_BUILD_FAILURE' for r in results.values()))
                self.assertFalse(any(evidence.glob('*.test.xml')))
            finally:
                os.chdir(old)

    def test_golden_diff_exit_is_a_failure_even_with_metrics(self):
        fake = """[INFO GPL-0100] Timing-driven iteration 1/2, virtual: false.
[INFO GPL-1001] Global placement finished at iteration 400
[INFO GPL-1014] Final placement area: 600
[INFO GPL-1018] Final HPWL (um): 2600
worst slack max 1.0
"""
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            old = Path.cwd()
            os.chdir(root)
            try:
                evidence = root / 'evidence'
                evidence.mkdir()
                def fail(cmd, **kwargs):
                    test = cmd[-1].split(':')[1].removesuffix('-tcl_test')
                    output = Path('bazel-testlogs/src/gpl/test') / f'{test}-tcl_test'
                    output.mkdir(parents=True, exist_ok=True)
                    (output / 'test.xml').write_text('<testsuite><testcase name="golden"><failure message="DEF mismatch"/></testcase></testsuite>')
                    (output / 'test.log').write_text(fake)
                    return subprocess.CompletedProcess(cmd, 3)
                with patch.object(mod.subprocess, 'run', side_effect=fail):
                    results = mod.run_arm('candidate', evidence)
                self.assertTrue(all(r['classification'] == 'NATIVE_REGRESSION_FAILURE' for r in results.values()))
                self.assertTrue(all(r['metrics']['hpwl_um'] == 2600 for r in results.values()))
            finally:
                os.chdir(old)

    def test_prior_evidence_cannot_rescue_missing_native_outputs(self):
        native_log = """[INFO GPL-0100] Timing-driven iteration 1/2, virtual: false.
[INFO GPL-1001] Global placement finished at iteration 400
[INFO GPL-1014] Final placement area: 601.79
[INFO GPL-1018] Final HPWL (um): 2617.69
worst slack max 1.37
"""
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            old = Path.cwd()
            os.chdir(root)
            try:
                evidence = root / 'evidence'
                evidence.mkdir()
                for test in mod.TESTS:
                    stem = f'candidate-{test}'
                    (evidence / f'{stem}.test.xml').write_text(
                        '<testsuite><testcase name="stale-pass"/></testsuite>')
                    (evidence / f'{stem}.test.log').write_text(native_log)
                with patch.object(mod.subprocess, 'run', side_effect=lambda cmd, **kwargs: subprocess.CompletedProcess(cmd, 0)):
                    results = mod.run_arm('candidate', evidence)
                self.assertTrue(all(
                    r['classification'] == 'INFRASTRUCTURE_OR_BUILD_FAILURE'
                    for r in results.values()))
                self.assertFalse(any(evidence.glob('*.test.xml')))
                self.assertFalse(any(evidence.glob('*.test.log')))
            finally:
                os.chdir(old)

if __name__ == '__main__':
    unittest.main()
