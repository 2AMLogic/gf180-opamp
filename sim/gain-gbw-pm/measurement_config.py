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

Extension contract for the other experiments (migrated in issue #89; see
`sim/noise/measurement_config.py` for the multi-bench form): give each
experiment a stdlib `measurement_config.py` exposing `EXPERIMENT`,
`FINGERPRINT_VERSION`, `TESTBENCHES_REL` and `inputs(texts, retained)`,
record the line `- **Measurement fingerprint**: ...` plus the inputs block in
its records (see `harness.fingerprint_header_lines`), and add it to
`MEASUREMENT_CONFIG` in `sim/report/characterization_report.py`. Records
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
TESTBENCH_REL = "sim/gain-gbw-pm/testbench/tb_gain_gbw_pm.spice"

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


def inputs(testbench_text: str, vcm_fixed_v: float | None = None) -> dict:
    """The effective measurement inputs, as a JSON-serialisable dict.

    `vcm_fixed_v=None` (the default VDD/2 policy) yields exactly the inputs --
    and fingerprint -- of every existing record.
    """
    return {
        "experiment": EXPERIMENT,
        "fingerprint_version": FINGERPRINT_VERSION,
        "bench": harness.canonical_bench_lines(testbench_text),
        "analysis": {"kind": "ac", "args": f"dec {AC_PPD:g} {AC_FSTART:g} {AC_FSTOP:g}"},
        "corners": {
            "process": list(CORNERS),
            "passive_sections": list(PASSIVE_SECTIONS),
            "temperature_c": [float(t) for t in TEMPS_C],
            "supply_v": [float(v) for v in SUPPLIES_V],
            "vcm_rule": vcm_rule(vcm_fixed_v),
        },
        "models": {"pdk_variant": harness.DEFAULT_VARIANT, "lib": MODEL_LIB},
    }


def fingerprint(testbench_text: str, vcm_fixed_v: float | None = None) -> str:
    return harness.measurement_fingerprint(inputs(testbench_text, vcm_fixed_v))


def fingerprint_lines(testbench_text: str, vcm_fixed_v: float | None = None) -> list[str]:
    """Markdown lines a record embeds (header line + retained inputs block)."""
    inp = inputs(testbench_text, vcm_fixed_v)
    return [
        f"- **Measurement fingerprint**: version {FINGERPRINT_VERSION}, sha256 "
        f"`{harness.measurement_fingerprint(inp)}` over the canonical inputs retained in "
        "the 'Measurement fingerprint inputs' section (bench content, `.ac` analysis, corner axes, "
        "model library/sections; excludes record IDs, paths and backend scheduling)",
    ]


def inputs_section(testbench_text: str, vcm_fixed_v: float | None = None) -> list[str]:
    inp = inputs(testbench_text, vcm_fixed_v)
    return [
        "## Measurement fingerprint inputs",
        "",
        "Canonical JSON (sorted keys, compact separators); its sha256 is the fingerprint in the header. "
        "Recompute with `sim/gain-gbw-pm/measurement_config.py`.",
        "",
        "```json",
        harness.fingerprint_json(inp),
        "```",
        "",
    ]
