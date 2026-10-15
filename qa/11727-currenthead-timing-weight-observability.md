# #11727 current-head weight-effect gate (fork QA)

Target: upstream base `835f9daa31ab159a23cdad539c7f3109c63810d1` versus rafaelmoresco's PR head `15ddd71883df6818fadae51ede8676d8bca96978`.

The previous native GCD/Nangate45 full-flow A/B produced an identical routed database and identical QoR on an **older** head. That does not show whether different GPL weighting executed. The next native gate must preserve: (1) two independently built exact-head executables, (2) complete routed flow on GCD/Nangate45, GCD/SKY130HD, AES/Nangate45, (3) first timing-driven pass net-slack and net-weight traces from `set_debug_level GPL timing 1`, turned off immediately after global placement, (4) comparative fingerprints only when the first-pass net+slack inputs match, (5) WNS, TNS, area, DRC, route DB SHA and runtimes. A different weight fingerprint with equal first-pass net/slack fingerprints establishes an execution-level weight difference **only**, not a QoR improvement. Every CI receipt must record binary hashes and exact source SHAs. No changes or reviews upstream are authorized.

Maintain attribution: percentile design and implementation by **rafaelmoresco**; semantics feedback by **gudeh**. Full-flow multi-design native execution is still pending. This note is diagnostic guidance, not a native-test receipt.
