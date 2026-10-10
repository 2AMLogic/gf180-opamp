"""Shared RZ x CC passive-corner study helpers (issues #70, #97).

ONE copy of the passive-section naming, the 3x3 level grid and the klt
corner-matrix construction, used by every `--passive-corners` side study
(`sim/gain-gbw-pm`, `sim/slew-swing-power`). Pure functions; no simulator.

Names map to the PDK's own sections in `sm141064.ngspice` (`.LIB res_typical|
res_ss|res_ff`, `.LIB mimcap_typical|mimcap_ss|mimcap_ff`). "worst" = the PDK
`_ss` section, "best" = `_ff`; which is worse for a given row is a RESULT, not
an assumption. res_ss: ppolyf_u_1k = 1000+200 ohm; res_ff = 1000-200.
mimcap_ss: mim_corner_2p0fF = 1.1; mimcap_ff = 0.9.
"""

from __future__ import annotations

PASSIVE_LEVELS = ("typical", "best", "worst")
_LEVEL_SUFFIX = {"typical": "typical", "best": "ff", "worst": "ss"}
RES_FACTOR = {"typical": 1.0, "best": 0.8, "worst": 1.2}
MIM_FACTOR = {"typical": 1.0, "best": 0.9, "worst": 1.1}

#: Study points (MOS corner, T, VDD): the PM-binding point, the GBW-binding
#: point (record 20261009-055759-2524b3e) and nominal.
PASSIVE_POINTS = [("fs", 125.0, 2.97), ("ss", 125.0, 2.97), ("typical", 27.0, 3.30)]

Key = tuple  # (process name, temperature_c, supply_v)


def passive_sections(res: str, mim: str) -> tuple[str, str]:
    """PDK section names for a (resistor level, MIM level) pair."""
    return (f"res_{_LEVEL_SUFFIX[res]}", f"mimcap_{_LEVEL_SUFFIX[mim]}")


def passive_name(mos: str, res: str, mim: str) -> str:
    """klt process-axis name encoding the MOS corner and both passive levels."""
    return f"{mos}__r-{res}__c-{mim}"


def split_passive_key(k: Key) -> tuple[str, str, str]:
    mos, rpart, cpart = k[0].split("__")
    return mos, rpart.removeprefix("r-"), cpart.removeprefix("c-")


def passive_combos() -> list[tuple[str, str]]:
    return [(r, c) for r in PASSIVE_LEVELS for c in PASSIVE_LEVELS]


def passive_process_axis(points=PASSIVE_POINTS) -> list[dict]:
    mos = list(dict.fromkeys(p[0] for p in points))
    return [
        {"name": passive_name(m, r, c), "sections": [m, *passive_sections(r, c)]}
        for m in mos
        for r, c in passive_combos()
    ]


def passive_expected_keys(points=PASSIVE_POINTS) -> list[Key]:
    return [
        (passive_name(m, r, c), float(t), float(v))
        for (m, t, v) in points
        for r, c in passive_combos()
    ]


def apply_passive_matrix(req: dict, points=PASSIVE_POINTS) -> dict:
    """Turn a request built for the T/VDD cross product of `points` into the
    ONE passive corner-matrix request: replace the process axis with the
    MOS x RZ x CC axis and remove, via klt's `exclude`, the cross-product cells
    that are not study points. Mutates and returns `req`."""
    temps = sorted({p[1] for p in points})
    vdds = sorted({p[2] for p in points})
    req["corners"]["process"] = passive_process_axis(points)
    req["corners"]["supply_v"] = {"vdd": list(vdds), "vcm": [round(v / 2, 6) for v in vdds]}
    req["corners"]["temperature_c"] = list(temps)
    want = set(points)
    exclude = []
    for m in dict.fromkeys(p[0] for p in points):
        for t in temps:
            for v in vdds:
                if (m, t, v) in want:
                    continue
                for r, c in passive_combos():
                    exclude.append({
                        "process": passive_name(m, r, c),
                        "temperature_c": t,
                        "supply_v": {"vdd": v},
                    })
    req["exclude"] = exclude
    return req


def deck_section_problems(key: Key, deck_text: str) -> list[str]:
    """Check that the deck klt generated for a passive cell really loads the
    MOS / resistor / MIM sections the cell's name encodes (fleet completion
    alone is not evidence the matrix was applied). Returns problem strings."""
    mos, r, c = split_passive_key(key)
    want = [mos, *passive_sections(r, c)]
    got = []
    for ln in deck_text.splitlines():
        parts = ln.split()
        if len(parts) >= 3 and parts[0].lower() == ".lib" and "sm141064" in parts[1]:
            got.append(parts[-1])
    if sorted(got) != sorted(want):
        return [f"{key[0]}: deck loads sections {got}, expected {want}"]
    return []
