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

Extension contract for the other experiments: give each experiment a stdlib
`measurement_config.py` exposing `EXPERIMENT`, `inputs(root)` and
`FINGERPRINT_VERSION`, record the line
`- **Measurement fingerprint**: ...` plus the inputs block in its records
(see `fingerprint_lines`), and add it to `MEASUREMENT_CONFIG` in
`sim/report/characterization_report.py`. Until then its records are reported
as measurement-configuration freshness unknown.
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


def inputs(testbench_text: str) -> dict:
    """The effective measurement inputs, as a JSON-serialisable dict."""
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
            "vcm_rule": "vdd/2",
        },
        "models": {"pdk_variant": harness.DEFAULT_VARIANT, "lib": MODEL_LIB},
    }


def fingerprint(testbench_text: str) -> str:
    return harness.measurement_fingerprint(inputs(testbench_text))


def fingerprint_lines(testbench_text: str) -> list[str]:
    """Markdown lines a record embeds (header line + retained inputs block)."""
    inp = inputs(testbench_text)
    return [
        f"- **Measurement fingerprint**: version {FINGERPRINT_VERSION}, sha256 "
        f"`{harness.measurement_fingerprint(inp)}` over the canonical inputs retained in "
        "the 'Measurement fingerprint inputs' section (bench content, `.ac` analysis, corner axes, "
        "model library/sections; excludes record IDs, paths and backend scheduling)",
    ]


def inputs_section(testbench_text: str) -> list[str]:
    inp = inputs(testbench_text)
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
