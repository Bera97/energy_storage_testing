# Termination-current-factor sweep

This adds `experiment.type: termination_current_factor_sweep` to the existing
single public CLI and the existing single/dual sweep execution paths.

```yaml
experiment:
  type: termination_current_factor_sweep
  name: threshold_factor_comparison
  cycles: 1
charge:
  # Keep the other required charge fields from your reviewed profile.
  termination:
    mode: after_current_threshold
    current_reference: configured_charge_current
    current_factor: [0.1, 0.25, 0.5]
    hold_time_s: 15
sweep:
  repeats: 1
  initial_discharge: true
```

The fragment above is not a complete profile. Full illustrative profiles are
`configs/termination_factor_sweep.ch1.example.yaml` and `.ch2.example.yaml`.
They inherit the existing CV-hold example's ratings, which are NOT independently
verified here. They use a placeholder instrument serial. Review and replace DUT
ratings, identity, instrument address, protection settings, currents and output
names before considering execution. No new physical hardware qualification is
claimed, including dual-channel operation.

Threshold current = factor * configured CC current. Existing scalar validation
requires a positive finite factor and a threshold strictly below CC current;
with this reference, 0 < factor < 1. Only the factor is swept. Hold duration and
CC current remain fixed; duration/current lists are not accepted simultaneously.
The confirmed-CV gate, strict current comparison, fixed timer, timeout limits,
recording/session checks, stop-both dual behavior and shutdown paths are reused.
All conditions are validated before any stage starts. The initial discharge
and rest run once per channel before its first charging condition; repeats are
nested within the supplied factor order, not randomized. There is no automatic
retry or resume.

Offline preview, from an activated environment:

```powershell
python -m smu --config configs/termination_factor_sweep.ch1.example.yaml --preview
python -m smu --config configs/termination_factor_sweep.ch1.example.yaml --config configs/termination_factor_sweep.ch2.example.yaml --preview
```

The existing `--execute` remains a separate explicit hardware authorization;
this patch's preparation and test commands do not invoke it on real hardware.
Use `--analyze RUN_NAME` on a completed recorded sweep as before. Exports include
`termination_current_factor`, `termination_current_a`, fixed hold duration, and
hold mode in phase/cycle CSVs, JSON, and `termination_factor_summary.csv`.
Grouped results are separated by factor/current/hold policy and, for dual runs,
channel/specimen. Initial preparation is excluded from comparisons. Recorded
energy ratios remain descriptive and are not established device efficiency.

Review the patch and run the offline tests before committing or considering a
physical qualification run. No commit, push, merge, branch deletion or hardware
operation is performed by the patch-preparation script.

## Single-channel hardware validation: 2026-11-24
Recorded run: `20261124_133324_termination_factor_trial_ch1`.
- Instrument: Keysight B2902B, serial MY61391173, channel 1.
- Specimen: 9&10, two 40 F LICs in parallel.
- Configured charge current: 0.2 A.
- Factors tested: 0.25 and 0.5; thresholds: 0.05 A and 0.10 A.
- Fixed threshold-triggered hold: 5 s; one repeat per factor.
- Factor 0.25: confirmed CV at 514.2 s, threshold/timer start
  at 563.0 s, completion at 568.0 s.
- Factor 0.5: confirmed CV at 498.4 s, threshold/timer start
  at 518.8 s, completion at 523.8 s.
- Both holds recorded 5.0 s and stopped with cv_hold_elapsed.
- Whole-sweep validation passed: 3 stages, 5 active-phase recordings,
  no validation errors.
- Every stage verified the serial, shutdown, and session closure.
- Both charge recordings finalized without fault indices or
  transport, cleanup, or finalization errors.
- Compliance warnings were retained in the analysis.
- Offline checks before finalization: 157 focused tests passed;
  full suite: 1526 tests and 344 subtests passed.
This establishes a successful single-channel hardware trial for those
two factors and the 5 s hold only. It does not establish factor 0.1,
a 15 s factor-sweep hold, repeatability, or dual-channel qualification.
Recorded sample timing does not establish exact output-enable timing.
Raw recordings and analysis outputs are retained locally, not added
to this source commit.
