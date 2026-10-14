# GPL #11727 timed-placement A/B

Fork-only QA at upstream head `1c84603cfed44245e0ca517ebe89570d3e68a749`. Compare native Nangate45 gcd timed-placement regressions on the unmodified PR and the candidate fix.

The test fixtures `simple01-td`, `simple01-td-net-percentage`, and `simple01-td-tune` cover default, 100%-selection, and multiple timing-overflow settings. Golden comparisons and actual timing-driven iteration logs are kept separately from measured QoR. This **does not** constitute full-flow, multi-design, or multi-technology QoR verification.

Credit: rafaelmoresco originated the percentile weighting design. gudeh identified that `-timing_driven_nets_percentage` and the 2% anchor combine multiplicatively. That user-contract concern remains unresolved; our tied-anchor candidate must not preempt it.

Remaining: real placement validation, resolve selection semantics with author, and independent full-flow multi-design QoR review. No upstream posting or merging in this experiment.
