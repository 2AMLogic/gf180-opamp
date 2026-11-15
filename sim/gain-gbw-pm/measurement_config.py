"""Effective measurement configuration of the gain/GBW/PM experiment (issue #85).

Stdlib only, so the offline report generator can load it without a simulator
or numpy. The driver (`run_gain_gbw_pm.py`) imports its grid and analysis
constants FROM here, so the fingerprint cannot drift from what is run.

The fingerprint covers what changes the measured numbers: the canonical bench
content (bias, load, stimulus, feedback isolation), the `.ac` analysis, the
corner/temperature/supply axes (and the VCM-tracks-VDD/2 rule), and the model
library and passive-section choices. It deliberately excludes record IDs,
workspace paths, backend / retry / scheduling options, and the extraction code
(an extraction-only fix does not need a new simulation). The DUT is covered by
its own hash (issue #75), not here.

Extension contract, shared by every experiment (issue #89, unified in issue
#325 -- this module is itself an example; `sim/slew-swing-power/measurement_config.py`
shows a multi-bench one): give each experiment a stdlib
`measurement_config.py` exposing `EXPERIMENT`, `FINGERPRINT_VERSION`,
`MODULE_REL`, `TESTBENCHES_REL` (bench name -> repo-relative path) and
`inputs(texts, retained=None)`, where `texts` maps those same bench names to
bench text and `retained` is the record's own stored inputs (unused unless the
experiment selects from them, as slew/swing/power does). Build the
`fingerprint` / `fingerprint_lines` / `inputs_section` trio with
`harness.fingerprint_api`, record the line `- **Measurement fingerprint**: ...`
plus the inputs block in its records (see `harness.fingerprint_header_lines`),
add the module to `EXPERIMENTS` in `sim/test_measurement_config.py`, and add it
to `MEASUREMENT_CONFIG` in `sim/report/characterization_report.py`. An
experiment may add further keyword-only configuration after `retained` (this
module's `vcm_fixed_v`); the first two parameters are the contract. Records
without the line are reported as measurement-configuration freshness unknown.
"""

from __future__ import annotations

import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parents[1] / "sim"))
import harness  # noqa: E402

EXPERIMENT = "gain-gbw-pm"
FINGERPRINT_VERSION = harness.FINGERPRINT_VERSION
MODULE_REL = "sim/gain-gbw-pm/measurement_config.py"
TESTBENCHES_REL = {"bench": "sim/gain-gbw-pm/testbench/tb_gain_gbw_pm.spice"}

#: MOS corners; each is bundled with the SAME typical resistor and MIM
#: capacitor sections (see the driver's passive-section policy).
CORNERS = ["typical", "ff", "ss", "fs", "sf"]
PASSIVE_SECTIONS = ("res_typical", "mimcap_typical")
TEMPS_C = [-40.0, 27.0, 125.0]
SUPPLIES_V = [2.97, 3.30, 3.63]
MODEL_LIB = "libs.tech/ngspice/sm141064.ngspice"

#: `.ac dec` sweep.
AC_FSTART, AC_FSTOP, AC_PPD = 0.1, 1e9, 20


#: Documented nominal consumer (LDO) amplifier-input common mode (issue #125;
#: spec/decision-records/0005-input-common-mode-range-row.md). Used ONLY by the
#: opt-in `--vcm-fixed` mode; the default rule stays VDD/2.
VCM_FIXED_CONSUMER_V = 1.20


def vcm_rule(vcm_fixed_v: float | None = None) -> str:
    """Common-mode policy label embedded in the fingerprint and records."""
    return "vdd/2" if vcm_fixed_v is None else f"fixed:{float(vcm_fixed_v):g}"


def inputs(texts: dict, retained: dict | None = None, vcm_fixed_v: float | None = None) -> dict:
    """The effective measurement inputs, as a JSON-serialisable dict.

    `texts` maps TESTBENCHES_REL keys to bench text. `retained` (the record's
    stored inputs) is unused here; it exists so every experiment shares one
    signature (slew/swing/power selects figures from it). `vcm_fixed_v=None`
    (the default VDD/2 policy) yields exactly the inputs -- and fingerprint --
    of every existing record.
    """
    return {
        "experiment": EXPERIMENT,
        "fingerprint_version": FINGERPRINT_VERSION,
        "bench": harness.canonical_bench_lines(texts["bench"]),
        "analysis": {"kind": "ac", "args": f"dec {AC_PPD:g} {AC_FSTART:g} {AC_FSTOP:g}"},
        "corners": harness.corner_axes(CORNERS, PASSIVE_SECTIONS, TEMPS_C, SUPPLIES_V, vcm_rule(vcm_fixed_v)),
        "models": {"pdk_variant": harness.DEFAULT_VARIANT, "lib": MODEL_LIB},
    }


fingerprint, fingerprint_lines, inputs_section = harness.fingerprint_api(
    inputs,
    "bench content, `.ac` analysis, corner axes, model library/sections",
    MODULE_REL)
