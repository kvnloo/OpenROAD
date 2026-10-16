# OpenROAD #11727 — numerical edge QA (2026-10-16)

Original contribution: rafaelmoresco. Review finding: gemini-code-assist; review follow-up: gudeh. This is downstream evidence, not an upstream review or merge claim.

Pinned upstream candidate: `15ddd71883df6818fadae51ede8676d8bca96978`; upstream baseline: `835f9daa31ab159a23cdad539c7f3109c63810d1`.

A standalone C++20 diagnostic compared the original float expression against a guarded variant. The guarded variant retains the original float arithmetic for finite, representable intermediate values; nonfinite arguments return neutral weight 1; overflowed or zero intermediates use double-precision interpolation.

Both GCC and Clang, at -O0/-O2/-O3 with -ffp-contract=off: 10/10 edge assertions and 100,000/100,000 bit-identical ordinary finite samples per configuration. This is isolated helper evidence, NOT native Bazel/GTest or full-flow placement/routing QoR. No source code or test runner has been published by this note.

Required before adoption: actual project clang-format, native GPL unit tests, then three-design routed A/B comparing identical upstream base and candidate source. Prior fork full-flow run 38727326065 concerns an older PR head only and cannot be promoted as current-head QoR evidence.
