#!/usr/bin/env python3
"""Local QA-only tests; fake binaries are NOT native QoR evidence."""
import importlib.util
from pathlib import Path
import os
import subprocess
import tempfile
import types
import unittest
from unittest.mock import patch

module_path = Path(__file__).with_name('11727-fullflow-qor.py')
spec = importlib.util.spec_from_file_location('lane5_fullflow', module_path)
mod = importlib.util.module_from_spec(spec)
spec.loader.exec_module(mod)

ORIGINAL_FLOW = '''set_thread_count [cpu_count]
# Global placement with placed IOs and routability-driven
global_placement -routability_driven -density $global_place_density \\
  -pad_left $global_place_pad -pad_right $global_place_pad
source $layer_rc_file
set_wire_rc -signal -layer $wire_rc_layer
'''


class FullFlowQA(unittest.TestCase):
    def test_instrument_activates_timing_only_on_second_placement(self):
        s = mod.instrument_flow(ORIGINAL_FLOW)
        self.assertIn('global_placement -routability_driven -timing_driven', s)
        self.assertIn('-timing_driven_nets_percentage 100', s)
        self.assertEqual(s.count('source $layer_rc_file'), 2)
        self.assertIn('LANE5_QOR route_drvs=[detailed_route_num_drvs]', s)
        self.assertIn('set_thread_count 2', s)
        self.assertEqual(ORIGINAL_FLOW.count('-timing_driven'), 0)

    def test_percentage_sweep_changes_real_placement_option(self):
        for pct in (10, 50, 100):
            with self.subTest(pct=pct):
                flow = mod.instrument_flow(ORIGINAL_FLOW, pct)
                self.assertIn(f'-timing_driven_nets_percentage {pct} ', flow)
                self.assertEqual(flow.count('-timing_driven_nets_percentage'), 1)
                self.assertEqual(flow.count('global_placement'), 1)
        # No values outside the Tcl option contract; even Python booleans are rejected.
        for bad in (0, 101, -10, 5.5, '10', True):
            with self.subTest(bad=bad):
                with self.assertRaisesRegex(ValueError, 'percentage'):
                    mod.instrument_flow(ORIGINAL_FLOW, bad)

    def test_fails_closed_if_source_changed(self):
        with self.assertRaisesRegex(ValueError, 'exactly one'):
            mod.instrument_flow(ORIGINAL_FLOW.replace('routability_driven', 'timing_driven'))
        with self.assertRaisesRegex(ValueError, 'RC setup'):
            mod.instrument_flow(ORIGINAL_FLOW.replace('source $layer_rc_file', ''))

    def test_metrics_validation(self):
        good = '\n'.join('LANE5_QOR %s=%s' % (k, v) for k, v in (
            ('wns_max', '-1.25'), ('tns_max', '-4'), ('design_area', '102.5'), ('route_drvs', '0')))
        self.assertEqual(mod.parse_metrics(good)['wns_max'], -1.25)
        for broken in (good.replace('route_drvs=0', 'route_drvs=NaN'), good + '\nLANE5_QOR wns_max=1',
                       good.replace('route_drvs=0', 'route_drvs=-1'),
                       good.replace('design_area=102.5', ''),
                       good.replace('route_drvs=0', 'route_drvs=2.2'),
                       good.replace('wns_max=-1.25', 'wns_max=1e30')):
            with self.subTest(log=broken):
                with self.assertRaises(ValueError):
                    mod.parse_metrics(broken)

    def test_nonzero_and_incomplete_are_not_comparable(self):
        with tempfile.TemporaryDirectory() as t:
            root = Path(t)
            exe = root / 'mock.sh'
            exe.write_text('#!/bin/sh\necho "LANE5_QOR wns_max=0"\nexit 1\n')
            exe.chmod(0o755)
            (root / 'test').mkdir()
            wrapper = root / 'tcl'
            wrapper.write_text('irrelevant')
            r = mod.run_one(exe, wrapper, root / 'test', root / 'out',
                            expected_route_db='gcd_nangate45_route-tcl.db')
            self.assertEqual(r['classification'], 'INCOMPLETE_FULL_FLOW')
            self.assertEqual(r['exit_code'], 1)
            pair = {'pdk/design': {'baseline': r, 'candidate': r}}
            self.assertEqual(mod.compare(pair)['pdk/design']['classification'], 'NOT_COMPARABLE')

    def test_per_flow_timeout_fails_closed_and_retains_log(self):
        with tempfile.TemporaryDirectory() as t:
            root = Path(t)
            binary = root / 'sleep.sh'
            binary.write_text('#!/bin/sh\necho "started"\nsleep 4\n')
            binary.chmod(0o755)
            (root / 'test').mkdir()
            wrapper = root / 'fixture.tcl'
            wrapper.write_text('ignored')
            r = mod.run_one(binary, wrapper, root / 'test', root / 'out', timeout_sec=1,
                            expected_route_db='gcd_nangate45_route-tcl.db')
            self.assertEqual(r['classification'], 'TIMEOUT_INCOMPLETE')
            self.assertEqual(r['exit_code'], 124)
            self.assertTrue(r['timed_out'])
            self.assertIn('started', (root / 'out/console.log').read_text())

    def test_real_flow_route_db_name_and_nonempty_receipt(self):
        # test/flow.tcl calls make_result_file(..._route.db), whose helpers.tcl
        # implementation inserts '-tcl' before the extension.
        with tempfile.TemporaryDirectory() as t:
            root = Path(t)
            exe = root / "mock-flow.py"
            exe.write_text('''#!/usr/bin/env python3
import os
from pathlib import Path
Path(os.environ["RESULTS_DIR"], "gcd_nangate45_route-tcl.db").write_bytes(b"db")
print("[INFO GPL-0100] Timing-driven iteration 1/2")
print("LANE5_QOR wns_max=-1.0")
print("LANE5_QOR tns_max=-1.0")
print("LANE5_QOR design_area=100")
print("LANE5_QOR route_drvs=0")
''')
            exe.chmod(0o755)
            (root / "test").mkdir()
            wrapper = root / "test.tcl"
            wrapper.write_text("# synthetic unit test only\n")
            record = mod.run_one(exe, wrapper, root / "test", root / "out", 3,
                                 expected_route_db="gcd_nangate45_route-tcl.db")
            self.assertEqual(record["classification"], "NATIVE_FULL_FLOW_PASS")
            self.assertEqual(record["route_db_count"], 1)
            self.assertEqual(record["route_db_bytes"], 2)
            self.assertIsNotNone(record["route_db_sha256"])

    def test_wrong_design_route_db_is_not_valid_evidence(self):
        # A successful routing run for AES must never validate a GCD QoR pair.
        with tempfile.TemporaryDirectory() as t:
            root = Path(t)
            (root / "test").mkdir()
            exe = root / "wrong-design.sh"
            exe.write_text('''#!/bin/sh
printf 'wrong design' > "$RESULTS_DIR/aes_nangate45_route-tcl.db"
echo '[INFO GPL-0100] Timing-driven iteration 1/2'
echo 'LANE5_QOR wns_max=-1'
echo 'LANE5_QOR tns_max=-2'
echo 'LANE5_QOR design_area=100'
echo 'LANE5_QOR route_drvs=0'
''')
            exe.chmod(0o755)
            wrapper = root / "gcd_nangate45.tcl"
            wrapper.write_text("# synthetic mismatch only\n")
            record = mod.run_one(exe, wrapper, root / "test", root / "out", 3,
                                 expected_route_db="gcd_nangate45_route-tcl.db")
            self.assertEqual(record["classification"], "INCOMPLETE_FULL_FLOW")
            self.assertIsNone(record["route_db_sha256"])

    def test_missing_or_empty_route_db_fails_closed(self):
        with tempfile.TemporaryDirectory() as t:
            root = Path(t)
            (root / "test").mkdir()
            wrapper = root / "test.tcl"
            wrapper.write_text("# synthetic unit test only\n")
            for case, cmd in (("wrong-name", 'touch "$RESULTS_DIR/gcd_nangate45_route.db"'),
                              ("empty-db", 'touch "$RESULTS_DIR/gcd_nangate45_route-tcl.db"')):
                with self.subTest(case=case):
                    exe = root / (case + ".sh")
                    exe.write_text('''#!/bin/sh
''' + cmd + "\n" +
                                   "echo '[INFO GPL-0100] Timing-driven iteration 1/2'\n" +
                                   "echo 'LANE5_QOR wns_max=-1'\n" +
                                   "echo 'LANE5_QOR tns_max=-1'\n" +
                                   "echo 'LANE5_QOR design_area=100'\n" +
                                   "echo 'LANE5_QOR route_drvs=0'\n")
                    exe.chmod(0o755)
                    record = mod.run_one(exe, wrapper, root / "test", root / case, 3,
                                         expected_route_db="gcd_nangate45_route-tcl.db")
                    self.assertEqual(record["classification"], "INCOMPLETE_FULL_FLOW")
                    self.assertIsNone(record["route_db_sha256"])

    def test_partial_matrix_is_not_full_qor_gate(self):
        with tempfile.TemporaryDirectory() as t:
            root = Path(t)
            repo = root / 'repo'
            test_dir = repo / 'test'
            test_dir.mkdir(parents=True)
            (test_dir / 'flow.tcl').write_text(ORIGINAL_FLOW)
            (test_dir / 'helpers.tcl').write_text('synthetic file contract for unit test')
            (repo / 'src/gpl/src').mkdir(parents=True)
            (repo / 'src/gpl/src/timingBase.cpp').write_text('mock')
            (repo / 'qa').mkdir()
            patch_file = repo / 'qa/11727-tied-percentile.patch'
            patch_file.write_text('test patch')
            entry = 'gcd_nangate45.tcl'
            (test_dir / entry).write_text('include -echo "flow.tcl"')
            binaries = {}
            for variant, v in [('baseline', '-2.0'), ('candidate', '-1.8')]:
                exe = root / variant
                exe.write_text('''#!/bin/sh
mkdir -p "$RESULTS_DIR"
printf 'synthetic' > "$RESULTS_DIR/gcd_nangate45_route-tcl.db"
echo '[INFO GPL-0100] Timing-driven iteration 1/2'
echo 'LANE5_QOR wns_max='"$1"
echo 'LANE5_QOR tns_max=-5'
echo 'LANE5_QOR design_area=50'
echo 'LANE5_QOR route_drvs=0'
''')
                # The command receives -no_init rather than a mock wns value.
                exe.write_text(exe.read_text().replace('"$1"', v))
                exe.chmod(0o755)
                binaries[variant] = exe
            output = root / 'partial'
            args = types.SimpleNamespace(repo=repo, baseline=binaries['baseline'],
                candidate=binaries['candidate'], candidate_patch_sha256=mod.sha256(patch_file),
                output=output, case=['nangate45/gcd'], timeout_sec=3,
                timing_nets_percentage=50)
            with (patch.object(mod, 'FLOW_GIT_BLOB', mod.git_blob(test_dir / 'flow.tcl')),
                  patch.object(mod, 'SOURCE_GIT_BLOB', mod.git_blob(repo / 'src/gpl/src/timingBase.cpp')),
                  patch.object(mod, 'FLOW_HELPERS_GIT_BLOB', mod.git_blob(test_dir / 'helpers.tcl')),
                  patch.object(mod, 'ENTRY_BLOBS', {entry: mod.git_blob(test_dir / entry)}),
                  patch.object(mod, 'EXPECTED_PATCH_SHA256', mod.sha256(patch_file))):
                self.assertEqual(mod.run(args), 0)
            report = __import__('json').loads((output / 'qor-summary.json').read_text())
            self.assertEqual(report['status'], 'PARTIAL_QOR_MATRIX_NATIVE_PAIRS')
            self.assertFalse(report['full_matrix'])
            self.assertEqual(report['timing_driven_nets_percentage'], 50)
            self.assertEqual(report['percentage_sweep'], 'SINGLE_SETTING_ONLY_NOT_A_SWEEP')
            self.assertEqual(len(report['comparisons']), 1)

    def test_full_runner_synthetic_variants_retain_provenance_gate(self):
        with tempfile.TemporaryDirectory() as t:
            root = Path(t)
            repo = root / 'repo'
            test_dir = repo / 'test'
            test_dir.mkdir(parents=True)
            src = test_dir / 'flow.tcl'
            src.write_text(ORIGINAL_FLOW)
            (test_dir / 'helpers.tcl').write_text('synthetic file contract for unit test')
            (repo / "src/gpl/src").mkdir(parents=True)
            (repo / "qa").mkdir()
            patch_file = repo / "qa/11727-tied-percentile.patch"
            patch_file.write_text("fake pinned candidate patch")
            gpl_source = repo / "src/gpl/src/timingBase.cpp"
            gpl_source.write_text("fake source for local QA only")
            for _, _, file in mod.DESIGNS:
                (test_dir / file).write_text('source "helpers.tcl"\ninclude -echo "flow.tcl"\n')
            binaries = {}
            for variant, wns in [('baseline', -0.2), ('candidate', -0.1)]:
                binary = root / variant
                binary.write_text('''#!/usr/bin/env python3
import os
from pathlib import Path
from sys import argv
entrypoint = Path(argv[-1]).stem
Path(os.environ['RESULTS_DIR'], f'{entrypoint}_route-tcl.db').write_text('fake')
print('[INFO GPL-0100] Timing-driven iteration 1/2')
print('LANE5_QOR wns_max=%s')
print('LANE5_QOR tns_max=-1.0')
print('LANE5_QOR design_area=100.0')
print('LANE5_QOR route_drvs=0')
''' % wns)
                binary.chmod(0o755)
                binaries[variant] = binary
            output = root / 'evidence'
            args = types.SimpleNamespace(repo=repo, baseline=binaries['baseline'],
                                         candidate=binaries['candidate'],
                                         candidate_patch_sha256=mod.EXPECTED_PATCH_SHA256,
                                         output=output, case=None, timeout_sec=10, timing_nets_percentage=10)
            entry_blobs = {file: mod.git_blob(test_dir / file) for _, _, file in mod.DESIGNS}
            with (patch.object(mod, 'FLOW_GIT_BLOB', mod.git_blob(src)),
                  patch.object(mod, 'SOURCE_GIT_BLOB', mod.git_blob(gpl_source)),
                  patch.object(mod, 'FLOW_HELPERS_GIT_BLOB', mod.git_blob(test_dir / 'helpers.tcl')),
                  patch.object(mod, 'ENTRY_BLOBS', entry_blobs),
                  patch.object(mod, 'EXPECTED_PATCH_SHA256', mod.sha256(patch_file))):
                args.candidate_patch_sha256 = mod.sha256(patch_file)
                self.assertEqual(mod.run(args), 0)
            report = __import__('json').loads((output / 'qor-summary.json').read_text())
            self.assertEqual(report['status'], 'COMPLETE_FOR_INDEPENDENT_QOR_REVIEW')
            self.assertEqual(report['timing_driven_nets_percentage'], 10)
            self.assertIn('TD 10%', report['test_config'])
            # These are synthetic outputs, not executable build receipts.
            self.assertEqual(report['binary_build_source_provenance'],
                             'REQUIRES_INDEPENDENT_BUILD_RECEIPT_CHECK')
            self.assertEqual(len(report['comparisons']), 3)
            self.assertEqual(report['reviewed_helpers_blob'], mod.git_blob(test_dir / 'helpers.tcl'))
            self.assertTrue(all(abs(v['delta_candidate_minus_baseline']['wns_max'] - 0.1) < 1e-9 for v in report['comparisons'].values()))
            self.assertEqual(src.read_text(), ORIGINAL_FLOW)
            self.assertEqual(len(list(output.rglob('console.log'))), 6)
            with (patch.object(mod, 'FLOW_GIT_BLOB', mod.git_blob(src)),
                  patch.object(mod, 'SOURCE_GIT_BLOB', mod.git_blob(gpl_source)),
                  patch.object(mod, 'FLOW_HELPERS_GIT_BLOB', mod.git_blob(test_dir / 'helpers.tcl')),
                  patch.object(mod, 'ENTRY_BLOBS', entry_blobs),
                  patch.object(mod, 'EXPECTED_PATCH_SHA256', mod.sha256(patch_file))):
                with self.assertRaisesRegex(ValueError, 'already exists'):
                    mod.run(args)


if __name__ == '__main__':
    unittest.main()
