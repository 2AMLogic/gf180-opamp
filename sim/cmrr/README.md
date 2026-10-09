# `sim/cmrr/` — common-mode rejection ratio

Measurement evidence for the **CMRR** row of `spec/target-spec.md` (issue #39;
supplies what DR-3 residual (e3) names as missing: a common-mode AC testbench
on the committed schematic). **It issues no pass or fail verdict and proposes
no bound.** The row's bound is not ratified. `spec/target-spec.md` and the
decision records are untouched; a proposed bound citing the record is a
separate decision-record issue for a human to ratify.

**Systematic only.** The simulated schematic has perfectly matched devices, so
this is the topology- and bias-limited CMRR. Real CMRR is usually set by
random mismatch, which needs Monte Carlo (DR-3 residual (e2)) and is out of
scope here.

## What is measured

- **Device under test**: `design/netlist/opamp_two_stage.spice` (the xschem
  export), instantiated as `opamp_two_stage`. The testbench declares no
  transistor. It uses the same DUT conversion and DUT guard as
  `sim/gain-gbw-pm/` (reused from that driver).
- **Conditions**: the ratified 45-point grid (5 MOS corners × −40/27/125 °C ×
  2.97/3.30/3.63 V). DC input common mode is VDD/2, `Ibias` = 10 µA (ideal),
  `CL` = 2 pF. Every corner uses `res_typical` + `mimcap_typical`, so RZ/CC
  passive spread is not swept.
- **Two excitations**, each one `klt sim` corner request over the 45 points:
  `dm` (vinp +0.5 V, vinn −0.5 V AC) and `cm` (vinp = vinn = 1 V AC).
- **Reported per point**: CMRR at "DC", at 1 kHz, 10 kHz, 100 kHz and 1 MHz,
  and at the differential unity-gain frequency f_u. Also reported: the
  worst-case corner of each figure, Ad and Acm at the plateau, and the
  curves.

## Bench: a DC servo that admits equal AC drive

The gain bench's `Cfb vinn 0` AC-grounds the inverting input, so it cannot
carry a common-mode signal. Its `Lfb vout vinn` would also inject
`v(vinn)/(jωLfb)` into the high-impedance output under CM drive. This bench
closes the DC loop with an ideal servo instead:

```
Esb vsb 0 vout 0 1        buffer: no load on vout
Rsv vsb vsv {rsv}         low-pass, tau = rsv*csv = 1e18 s
Csv vsv 0 {csv}
Esv vinn vnac vsv 0 1     v(vinn) = DC level + the vinn AC source
Vnac vnac 0 dc 0 ac {acn}
```

At DC this gives the unity-buffer operating point (vinn = vout = VCM), the
point `design/check_dc_op.py` verifies. At every swept frequency the loop is
open, with v(vinp) = acp and v(vinn) = acn. The driver rewrites only the
`.param` line, once per excitation.

## Extraction

- From the **actual** phasors of both runs, vd = v(vinp) − v(vinn) and
  vc = (v(vinp) + v(vinn))/2. The two equations
  `vout = Ad·vd + Acm·vc` are solved **exactly** per frequency. A residual
  differential leak in the CM run therefore cannot masquerade as common-mode
  gain. CMRR = 20 log10 |Ad/Acm|.
- **"DC"** is the mean over 0.1–1 Hz. It is valid only if CMRR and |Acm| are
  flat there to 0.1 dB; otherwise it is reported unavailable, with the 0.1 Hz
  value. AC analysis has no literal 0 Hz sample.
- **Spot values and the value at f_u** use linear interpolation in
  (log10 f, dB). f_u is the first descending 0 dB crossing of |Ad|, from the
  gain driver's extraction reused unchanged. That extraction also rejects a
  non-flat, wrong-polarity or re-crossing Ad.
- **Numerical floor**: an |Acm| below 10 × the residual demonstrated by a
  superposition check is clamped, and the figure is reported as a lower bound
  (`>=`). The check compares vout(both inputs) with vout(vinp only) +
  vout(vinn only) at the nominal point. Nothing is ever divided by zero, and
  no "infinite" CMRR is reported.
- **Near-cancellation flag**: if Acm's plateau phase changes sign across the
  grid, or a point sits more than 20 dB above the grid median, the record
  lists it. Such values are numerically resolved but are not design margin.

## Validation (blocking: any failure writes no record, exit 2)

| check | criterion |
|---|---|
| grid completeness | 45/45 points per excitation, no duplicates/extras, rawfile + log retained |
| data | finite, positive strictly increasing frequency, identical axes across excitations |
| differential excitation | vd = 1 V and vc = 0 within 1e-6 V at every frequency |
| common-mode excitation | vc = 1 V within 1e-6 V and residual \|vd/vc\| ≤ 1e-6 |
| operating point (every point and excitation) | returned; \|vout − VCM\| ≤ 100 mV; all excitations at the same DC vout within 1 µV |
| decomposition vs the gain bench | Ad + Acm/2 equals the committed gain-bench \|vout/vdiff\| (it drives vinp alone) within 0.05 dB over the whole sweep |
| local vs grid | the local nominal pair reproduces the grid's nominal point within 0.05 dB |
| servo isolation | tau = 1e16 and 1e20 s move CMRR ≤ 0.01 dB; tau = 1e-3 s (servo closes the loop at AC) must be rejected |
| unequal CM drive | acn = 0.99 must be rejected |
| negative control | control DUT with XM3 W 6u → 5.4u (−10 %) must lower the 0.1 Hz CMRR by ≥ 6 dB |

The opposite imbalance (+10 %) is also run and recorded as information,
without a criterion. It *raises* the CMRR: the systematic Acm is a signed sum,
so an imbalance can cancel part of it as well as add to it. That is why a
deterministic corner cannot stand in for the mismatch-limited figure. The
control DUT exists only in the control work directory and in
`netlist-snapshots/<rid>-controls.spice`. The production export is never
written.

## Running

```bash
python3 sim/cmrr/test_cmrr.py            # offline tests (+ 2 local nominal pairs)
python3 sim/cmrr/run_cmrr.py --smoke     # nominal point, local, no record
python3 sim/cmrr/run_cmrr.py --batch-runner-version-check warn \
        --batch-submit-retries 10        # full grid + record (append-only)
```

Cold-start prerequisites:

- `klt`, `ngspice` and the pinned gf180mcu PDK (`find_pdk` in
  `sim/harness.py`; `volare enable --pdk gf180mcu c6d73a35…`).
- Python 3 with `numpy` and a `matplotlib` that imports with it.
- The committed gain-bench dataset (`sim/gain-gbw-pm/corners/`), used for the
  decomposition cross-check.

The grid always goes through `klt sim`. That means the Spot batch fleet on a
dispatch worker (`$KLT_SIM_BACKEND=batch`), with `--batch-runner-version-check
warn` while the fleet runner is klt 0.5.0. The driver never launches an
ngspice grid and never falls back to a local grid. Only the nominal single
units (studies, controls, smoke) run locally. `--keep-work DIR` keeps the
grid reports and rawfiles of a run that failed validation, for diagnosis.
Never commit that directory.

Environment notes, learned on the dispatch worker:

- **Host `~/.spiceinit`.** Local single units run with an empty `HOME`, so a
  user `~/.spiceinit` is not loaded. One such file (`set wnflag=1`) moved the
  nominal open-loop gain by 0.24 dB against the fleet. DR-0004 keeps every
  `sim/` deck at ngspice defaults.
- **Local PDK selection.** Local units pass `models.pdk_root`. klt's own
  lookup prefers `~/.ciel` over `~/.volare` and so could pick a different
  open_pdks revision than the pinned one.
- **klt PDK provenance.** klt's `provenance.pdk` and `models_lib_sha256` in a
  batch report describe the submitting client's PDK, not the fleet's. The
  record says so and ties the fleet to the pinned revision through the
  cross-checks.
- **numpy/matplotlib mismatch.** A user-site numpy 2 can shadow a distro
  matplotlib built for numpy 1. The driver then stops before submitting
  anything. Run with `python3 -s` to skip the user site.

## Evidence layout (append-only)

```
records/<rid>.md                         claim, worst case, all points, method, cross-checks, studies, controls
records/<rid>-plots/*.png                CMRR of all 45 points; nominal |Ad|, |Acm|, CMRR
corners/<rid>/<point>.<dm|cm>.dat        actual vout, vinp, vinn phasors (re/im) per frequency
corners/<rid>/<point>.<dm|cm>.{log,cir}  ngspice log (with the printed operating point), klt deck
corners/<rid>/<point>.cmrr.dat           freq, |Ad|, |Acm|, CMRR (dB)
corners/<rid>/klt-report.<mode>.json     sanitised klt reports (job ids, runner versions)
corners/<rid>/controls/                  local studies and controls (.dat, .log, .cir)
netlist-snapshots/<rid>.spice            DUT + testbench + both requests + generated deck
netlist-snapshots/<rid>-controls.spice   the control DUTs and fixture parameters, separately
```

Every run mints a new record id. A record is never overwritten. Results must
be regenerated if the DUT changes (for example after #42); each record names
the DUT sha256 it measured.
