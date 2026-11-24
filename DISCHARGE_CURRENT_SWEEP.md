# Constant-current discharge sweeps

New type: `experiment.type: discharge_current_sweep`.
The absolute current magnitudes in amperes are selected in
`sweep.discharge_current_a`; the charge configuration stays scalar and fixed.

```yaml
experiment:
  type: discharge_current_sweep
  name: discharge_load_comparison
  cycles: 1
sweep:
  discharge_current_a: [0.1, 0.2]
  repeats: 1
  initial_discharge: true
```

This is a fragment, not a complete profile. Complete illustrative profiles:
`configs/discharge_current_sweep.ch1.example.yaml` and the channel-2 counterpart.
They inherit existing trial settings but use placeholder specimen IDs and an
instrument serial placeholder. Review all DUT ratings, boundaries, wiring,
protection, instrument identity, rest settings and output names before any
hardware trial. There is no new hardware qualification from this patch.

## Preparation and ordering

The existing scalar `discharge.current` mapping (rated maximum discharge
fraction) selects the initial preparation current. It is NOT replaced by the
first swept value. Initial discharge and its following rest run once, then
conditions follow the supplied list order, with repeats nested within each
value. Every condition retains the complete configured charge/rest/discharge/
rest cycles. Each discharge therefore follows the same configured charging
procedure; equal physical state of charge is not independently established.
The recorded `initial_preparation` plan stores its own scalar configuration,
preview and execution settings. Preview and dual preflight include its limits.

To reuse the existing validated scalar discharge backend, each positive current
magnitude is converted internally into the equivalent rated-discharge fraction.
The original requested ampere list remains in the sweep configuration; condition
metadata uses the resolved ampere magnitude actually placed in its scalar plan.
Tiny floating-point differences from fraction conversion are possible. No new
instrument-control path or source-polarity convention is introduced.

All values must be finite and positive, unique, within supplied discharge
ratings, and valid under the existing configuration, acquisition and execution
limits. Other sweep axes cannot be combined. All conditions and initial
preparation are planned before reserving recording directories or connecting.
Timeout, validation, transport, recording or session-close failure stops the
sequence. Existing output directories are never overwritten or resumed.

## Preview and exports

```powershell
python -m smu --config configs/discharge_current_sweep.ch1.example.yaml --preview
python -m smu --config configs/discharge_current_sweep.ch1.example.yaml --config configs/discharge_current_sweep.ch2.example.yaml --preview
```

The existing explicit `--execute` authorization and `--analyze RUN_NAME`
interface are retained. Do not execute illustrative profiles unchanged.
The `discharge_current_a` condition label is included in the phase and cycle
CSV/JSON output, and `discharge_current_summary.csv` groups results by discharge
and charge current. On a charge-phase row this is the condition's later
planned discharge magnitude, not the instantaneous measured charge current.
Initial preparation is excluded from comparisons and has no swept-current label.
Delivered discharge energy remains `discharge_negative_energy_wh` (a positive
magnitude); signed charge retains its recorded sign. Recorded sample spans are
not exact output-enable durations, and recorded energy ratios are not an
independently established device efficiency.

Dual sweeps retain one shared instrument session, distinct specimens, an
initial-preparation barrier, independent channel progression, channel-separated
exports and stop-both failure behavior. Dual physical qualification remains a
separate task. Constant-power sweeps, consumption profiles, leakage testing and
new capacitance/ESR calculations are outside this patch.
