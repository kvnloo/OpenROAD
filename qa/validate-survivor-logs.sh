#!/usr/bin/env bash
# Fork-only validator. Never treat a fabricated .ok file as execution evidence.
set -euo pipefail
ROOT=$(git rev-parse --show-toplevel)
EVIDENCE="$ROOT/qa-evidence"
mkdir -p "$EVIDENCE"
exec > >(tee "$EVIDENCE/validation.log") 2>&1
printf 'candidate_commit=%s\n' "$(git rev-parse HEAD)" | tee "$EVIDENCE/provenance.txt"
printf 'candidate_source_blob=%s\n' "$(git hash-object src/rsz/src/Resizer.cc)" >> "$EVIDENCE/provenance.txt"
printf 'baseline_commit=%s\n' 9b7414eb17b4f33faba3d40b7031784a25416f54 >> "$EVIDENCE/provenance.txt"

python3 -m venv "$RUNNER_TEMP/openroad-tools"
"$RUNNER_TEMP/openroad-tools/bin/pip" install tclint==0.7.0
export PATH="$RUNNER_TEMP/openroad-tools/bin:$PATH"
command -v bazelisk >/dev/null || {
  curl -fL --retry 2 https://github.com/bazelbuild/bazelisk/releases/download/v1.27.0/bazelisk-linux-amd64 -o "$RUNNER_TEMP/bazelisk"
  chmod +x "$RUNNER_TEMP/bazelisk"
  export PATH="$RUNNER_TEMP:$PATH"
}
CASES=(remove_buffers_survivor_port_nets remove_buffers_survivor_hier)
FILES=()
for name in "${CASES[@]}"; do FILES+=("src/rsz/test/$name.tcl"); done
clang-format --version
tclfmt --version
clang-format --dry-run --Werror src/rsz/src/Resizer.cc
tclfmt --in-place "${FILES[@]}"
tclint "${FILES[@]}"
git diff --check
for file in "${FILES[@]}"; do cp "$file" "$EVIDENCE/"; done

# The normal repository config uses its anonymous, read-only remote cache.
# Never enable --config=ci: that config requires write credentials.
bazelisk build --jobs=2 --disk_cache= //:openroad
EXE="$ROOT/bazel-bin/openroad"
sha256sum "$EXE" > "$EVIDENCE/candidate-binary.sha256"
"$EXE" -version | tee "$EVIDENCE/candidate-version.txt"
for name in "${CASES[@]}"; do
  (cd src/rsz/test && "$EXE" -no_splash -no_init -threads 1 -exit "$name.tcl") > "$EVIDENCE/$name.ok" 2>&1
  cat "$EVIDENCE/$name.ok"
  (cd src/rsz/test && "$EXE" -no_splash -no_init -threads 1 -exit "$name.tcl") > "$EVIDENCE/$name.repeat.log" 2>&1
  diff -u "$EVIDENCE/$name.ok" "$EVIDENCE/$name.repeat.log"
  cp "$EVIDENCE/$name.ok" "src/rsz/test/$name.ok"
done
# Check required outcomes independently of the generated full-log snapshots.
grep -Fq 'b1 remains: 0' "$EVIDENCE/remove_buffers_survivor_port_nets.ok"
grep -Fq 'b1 remains: 1' "$EVIDENCE/remove_buffers_survivor_port_nets.ok"
grep -Fq 'dangling_selected remains: 0' "$EVIDENCE/remove_buffers_survivor_port_nets.ok"
grep -Fq 'dangling_auto remains: 0' "$EVIDENCE/remove_buffers_survivor_port_nets.ok"
grep -Fq 'u/b1 remains: 0' "$EVIDENCE/remove_buffers_survivor_hier.ok"
grep -Fq 'u/b1 remains: 1' "$EVIDENCE/remove_buffers_survivor_hier.ok"
grep -Eq 'assign[[:space:]]+o[[:space:]]*=[[:space:]]*i[[:space:]]*;' "$EVIDENCE/remove_buffers_survivor_hier.ok"

bazelisk test --jobs=2 --disk_cache= --test_output=errors --nocache_test_results \
  //src/rsz/test:remove_buffers_survivor_port_nets-tcl_test \
  //src/rsz/test:remove_buffers_survivor_hier-tcl_test \
  //src/rsz/test:remove_buffers1-tcl_test \
  //src/rsz/test:remove_buffers2-tcl_test \
  //src/rsz/test:remove_buffers3-tcl_test \
  //src/rsz/test:remove_buffers4_hier-tcl_test \
  //src/rsz/test:remove_buffers_dont_touch_rejected-tcl_test \
  //src/rsz/test:remove_buffers_disconnected_input_rejected-tcl_test
printf 'candidate_regressions=PASS\n' >> "$EVIDENCE/provenance.txt"
git diff -- src/rsz/test > "$EVIDENCE/test-changes.patch"

# Negative control: all production files except Resizer.cc already match the
# original base; restore that exact pre-fix file and require the reported bug.
git fetch --no-tags --depth=1 origin 9b7414eb17b4f33faba3d40b7031784a25416f54
git show 9b7414eb17b4f33faba3d40b7031784a25416f54:src/rsz/src/Resizer.cc > src/rsz/src/Resizer.cc
trap 'git restore --source=HEAD -- src/rsz/src/Resizer.cc' EXIT
bazelisk build --jobs=2 --disk_cache= //:openroad
sha256sum "$EXE" > "$EVIDENCE/baseline-binary.sha256"
for name in "${CASES[@]}"; do
  set +e
  (cd src/rsz/test && "$EXE" -no_splash -no_init -threads 1 -exit "$name.tcl") > "$EVIDENCE/$name.baseline.log" 2>&1
  code=$?
  set -e
  printf '%s_baseline_exit=%s\n' "$name" "$code" >> "$EVIDENCE/provenance.txt"
  cat "$EVIDENCE/$name.baseline.log"
  if [ "$name" = "remove_buffers_survivor_port_nets" ]; then
    # This is the original bug witness, which must fail before the fix.
    test "$code" -ne 0
    grep -Fq 'ODB-0369' "$EVIDENCE/$name.baseline.log"
  else
    # The hierarchical fixture is a compatibility control, not a bug witness.
    test "$code" -eq 0
    diff -u "$EVIDENCE/$name.ok" "$EVIDENCE/$name.baseline.log"
  fi
done
printf 'baseline_bug_witness=PASS\nhierarchy_compatibility=PASS\n' >> "$EVIDENCE/provenance.txt"
