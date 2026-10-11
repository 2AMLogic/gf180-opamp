# enable-acquisition -- supply and external-bias enable acquisition (issue #191)

Evidence only. Neither `spec/target-spec.md` nor this directory defines or
grades a startup bound; the ramp rates, delays and windows below are
**diagnostic experiment conditions**, not consumer guarantees or supply/bias
slew requirements. A bias-source generator and consumer-loop startup are out of
scope (the ideal externally supplied 10 uA bias remains the block boundary).

The follower step bench (`step-response/`) and the overload bench
(`overdrive-recovery/`) both start from an established supply and bias. This
study instead starts with every source OFF and measures how the committed
DUT (`design/netlist/opamp_two_stage.spice`, CL = 2 pF, final Ibias = 10 uA,
unity-gain follower) acquires its powered follower level.

## Bench (`testbench/tb_enable.spice`)

* `Vdd`, `Vcm` stay plain DC sources (the klt-swept FINAL levels, VCM = VDD/2).
  Two 0..1 pwl envelopes (`Vrs`, `Vrb`) gate them: `v(vdd) = Vdd*rs`,
  `v(vinp) = Vcm*rs` (the input tracks the ramped rail at VDD/2, no unexplained
  powered-off drive), `I(ibias) = 10u*rb`.
* Initial state: both envelopes are 0 at t = 0, `.tran` without `uic`, so the
  t = 0 point is the trivial unpowered state. The extraction rejects a run whose
  first samples are not unpowered/unbiased; the `powered-start` control (the
  envelopes fixed on) proves it does.
* The schedule is six `.param` lines substituted per case by the driver
  (exactly one match each); every value is recorded in the fingerprint and the
  record. Traces retained per point: vdd, bias envelope, bias-mirror node, vinp,
  vout and the internal nodes n1, n2, nz, tail (`*.dat.gz`).

## Cases (3 scenarios x fast/slow ramp pair x 45 PVT points)

| Scenario | Sequence |
|---|---|
| `simultaneous` | rail and bias ramp together from t = 1 us |
| `supply-first` | rail ramps at 1 us; the bias is enabled 2 us after the rail ramp completes |
| `bias-cycle` | as supply-first (enable), then at fixed supply the bias is ramped to 0 A, held off 2 us, and re-enabled (re-enable) |

Ramps: fast = 100 ns, slow = 5 us (rail and bias). Observation window after each
enable completes: 4 us (> 200 tau at the ratified GBW floor).

## Measurement

* `v_ref` per point: a SEPARATE 45-point DC solution of the same bench with the
  envelopes fixed on (never read from the run being judged); invalid if more than
  20 mV from VDD/2.
* Acquisition time: from the instant the event's last ramp completes to the LAST
  exit of |vout - v_ref| from a +-1 % band (linear interpolation). Claimed only
  if the output stays in band to the window end, drifts <= 0.1 band across the
  last 200 ns, and the in-band tail is >= 10 % of the window; otherwise the event
  is **censored / not acquired** (inf), ranked worst, never a finite number.
* Any censored event is re-run over a 10x longer window before persistent
  failure is described; the record shows both. The follow-up keeps the same
  evidence as a primary case (decks, logs, `.dat.gz` traces, sanitised klt
  report under `corners/<rid>/long_<scenario>_<ramp>/`; its request and
  schedule in the netlist snapshot) and is re-derived from the committed
  traces with the primary points.
* Peaks: overshoot / undershoot about v_ref after the ramps complete; peak excess
  of vout over the instantaneous rail (`rail+`, 0 when vout stays at or below it).
* Invalid (never a result): powered start, unreached final rail or bias, bias not
  removed in the cycle, missing samples, non-finite or off-level reference.

## Controls (local single units, nominal point, simultaneous / fast)

`nominal`; `cl-x5` (CL = 10 pF must acquire later); `ibias-zero` (dead bias must
never be acquired, and must itself be a valid run); `powered-start` (must be rejected as invalid).

## Timing pilot (local, single nominal corner, `--pilot`)

Each case simulates in well under a second locally (1000 - 3300 rows). Halving
the sample interval to 1 ns / 2 ns moved the acquisition instants by < 0.5 ns
(fast 30.3 -> 30.2 ns, slow identical), so tstep = 5 ns (fast) / 10 ns (slow)
is kept for the fleet requests; the long-window follow-up reuses the same
sampling. Each case is its own 45-point request so no point's run covers more
than one schedule.

## Running

```
python3 sim/enable-acquisition/test_enable_acquisition.py      # offline tests, no simulator
python3 sim/enable-acquisition/run_enable_acquisition.py --smoke   # nominal, local, writes nothing
python3 sim/enable-acquisition/run_enable_acquisition.py --pilot   # timing pilot, writes nothing
python3 sim/enable-acquisition/run_enable_acquisition.py           # grids via klt ($KLT_SIM_BACKEND) + append-only record
```

Evidence: `records/<rid>.md`, `corners/<rid>/`, `netlist-snapshots/<rid>.spice`.
A failed batch submit writes no record and never falls back to a local grid.
