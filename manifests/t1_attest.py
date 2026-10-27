#!/usr/bin/env python3
"""Artifact-anchored attestations for signoff T1 items 1, 9 and 10 (issue #160).

klt 0.8.0 accepts, for items 1, 2, 9 and 10, a `generic` envelope that declares
`t1_item`, names the audited artifact in `provenance.input.path` with its
content hash, and is pinned by the same hash in the block manifest.  This
script owns the three audited artifacts (inventories under manifests/attest/)
and the three envelopes beside them.

The envelope `status` is an assertion, so this script derives it: every check
below is evaluated mechanically against the working tree, and if any check
fails nothing is written (write mode) or the run fails (--check).  A pass here
means "the inventory is complete and consistent", not "every result passes".

    python3 manifests/t1_attest.py            # (re)write the six files and re-pin the manifest
    python3 manifests/t1_attest.py --check    # verify, write nothing (CI)

Stdlib only; no simulator, PDK or network.  Exit: 0 ok, 1 --check drift,
2 a check failed or an input is missing.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import re
import sys
from pathlib import Path

ATTEST = "manifests/attest"
MANIFEST = "manifests/gf180-opamp.json"
SELECTION = "sim/report/selection.json"

DESIGN_FILES = [
    "design/opamp_two_stage.sch",
    "design/opamp_two_stage.sym",
    "design/xschemrc",
]
NETLIST = "design/netlist/opamp_two_stage.spice"
WORKFLOWS = [".github/workflows/selftest.yml", ".github/workflows/signoff.yml"]


class AttestError(Exception):
    pass


def sha(data: bytes) -> str:
    return "sha256:" + hashlib.sha256(data).hexdigest()


def read(root: Path, rel: str) -> bytes:
    p = root / rel
    if not p.is_file():
        raise AttestError(f"missing file: {rel}")
    return p.read_bytes()


def netlist_bytes_normalised(raw: bytes) -> bytes:
    """Drop xschem's checkout-specific `** sch_path:` comment (the netlist
    check in sim/ci_netlist_check.py ignores exactly this line too)."""
    lines = raw.decode("utf-8").splitlines(keepends=True)
    return "".join(l for l in lines if not l.startswith("** sch_path:")).encode()


def dump(obj) -> str:
    return json.dumps(obj, indent=2, sort_keys=True) + "\n"


# --------------------------------------------------------------- item 1 ----
def design_sources(root: Path) -> dict:
    files = []
    for rel in DESIGN_FILES:
        files.append({"path": rel, "role": "source", "content_hash": sha(read(root, rel))})
    net = netlist_bytes_normalised(read(root, NETLIST))
    text = net.decode()
    if not re.search(r"(?im)^\*\*\.subckt\s+opamp_two_stage\b|^\.subckt\s+opamp_two_stage\b", text):
        raise AttestError(f"{NETLIST}: no opamp_two_stage subcircuit")
    if len(re.findall(r"(?m)^[xX]", text)) < 5:
        raise AttestError(f"{NETLIST}: fewer than 5 device instances (empty export?)")
    files.append({
        "path": NETLIST, "role": "derived-netlist",
        "content_hash": sha(net),
        "hash_note": "sha256 of the file with the checkout-specific '** sch_path:' comment removed",
    })
    gate = "sim/ci_netlist_check.py"
    read(root, gate)
    sel = (root / ".github/workflows/selftest.yml").read_text()
    if "sim/selftest.sh" not in sel:
        raise AttestError("selftest.yml does not run sim/selftest.sh")
    if "ci_netlist_check.py" not in (root / "sim/selftest.sh").read_text():
        raise AttestError("sim/selftest.sh does not run ci_netlist_check.py")
    return {
        "schema_version": 1,
        "t1_item": 1,
        "subject": "design-source inventory",
        "files": files,
        "regenerate": "xschem -n -x -q --rcfile design/xschemrc -o design/netlist design/opamp_two_stage.sch",
        "freshness_gate": {
            "script": gate,
            "run_by": "sim/selftest.sh via .github/workflows/selftest.yml",
            "what": "re-exports the schematic with the pinned xschem and diffs against the committed netlist",
        },
        "checks": [
            "every listed source and the derived netlist exist",
            "netlist declares subcircuit opamp_two_stage and has at least 5 device instances",
            "selftest.sh runs the netlist freshness gate and selftest.yml runs selftest.sh",
        ],
    }


# --------------------------------------------------------------- item 9 ----
#: Experiments with no `testbench/` of their own: the committed bench they run
#: is another experiment's, staged by their driver (a derived copy, guarded
#: against the committed bench) and documented in their README. The inventory
#: hash-pins the reused bench, so editing it still stales the attestation.
BENCH_REUSE = {"gain-gbw-pm-mc": "gain-gbw-pm"}


def testbench_inventory(root: Path) -> dict:
    sel = json.loads(read(root, SELECTION))
    exps = sorted(sel["experiments"])
    harness = read(root, "sim/harness.py").decode()
    m = re.search(r'^PINNED_PDK_REV\s*=\s*"([0-9a-f]{40})"', harness, re.M)
    if not m:
        raise AttestError("sim/harness.py: PINNED_PDK_REV not found")
    pdk_rev = m.group(1)
    wf = read(root, ".github/workflows/selftest.yml").decode()
    if f'GF180_PDK_REV: "{pdk_rev}"' not in wf:
        raise AttestError("selftest.yml GF180_PDK_REV disagrees with sim/harness.py PINNED_PDK_REV")
    char = read(root, "sim/characterize.sh").decode()
    entries = []
    for e in exps:
        d = f"sim/{e}"
        drivers = sorted(p.name for p in (root / d).glob("run_*.py"))
        if len(drivers) != 1:
            raise AttestError(f"{d}: expected exactly one run_*.py driver, found {drivers}")
        driver = drivers[0]
        tbs = sorted(p.name for p in (root / d / "testbench").glob("tb_*.spice"))
        reuse = BENCH_REUSE.get(e)
        if not tbs:
            if reuse is None:
                raise AttestError(f"{d}/testbench: no committed tb_*.spice")
        readme = read(root, f"{d}/README.md").decode()
        cmd = re.search(rf"^(python3 {re.escape(d)}/{re.escape(driver)}(?: [^\n#]*)?)", readme, re.M)
        if not cmd:
            raise AttestError(f"{d}/README.md documents no 'python3 {d}/{driver}' invocation")
        smoke = re.search(rf"^(python3 {re.escape(d)}/{re.escape(driver)} --smoke)", readme, re.M)
        if reuse:
            # bench reuse must be documented, and the reused bench must exist (exactly one)
            if f"sim/{reuse}/testbench/" not in readme:
                raise AttestError(f"{d}/README.md does not document reusing sim/{reuse}/testbench/")
            rtbs = sorted(p.name for p in (root / f"sim/{reuse}/testbench").glob("tb_*.spice"))
            if len(rtbs) != 1:
                raise AttestError(f"sim/{reuse}/testbench: expected exactly one committed tb_*.spice, found {len(rtbs)}")
            tb_paths = [f"sim/{reuse}/testbench/{rtbs[0]}"]
            tb_note = (f"no testbench/ of its own: {d}/{driver} stages a derived copy of {tb_paths[0]} "
                       "(mismatch switch added) and guards it against the committed bench")
        else:
            tb_paths = [f"{d}/testbench/{t}" for t in tbs]
            tb_note = None
        entries.append({
            "experiment": e,
            "testbenches": [
                {"path": p, "content_hash": sha(read(root, p))} for p in tb_paths
            ],
            "driver": f"{d}/{driver}",
            "readme": f"{d}/README.md",
            "documented_invocation": cmd.group(1).strip(),
            "documented_smoke_invocation": smoke.group(1) if smoke else None,
            "run_by_characterize_sh": f"{e}/{driver}" in char,
            **({"testbench_reuse": tb_note} if tb_note else {}),
        })
    for p in ("sim/characterize.sh", "sim/selftest.sh", "sim/ci_prereqs.py", "sim/ci_pdk_rev_check.sh"):
        read(root, p)
    return {
        "schema_version": 1,
        "t1_item": 9,
        "subject": "testbench inventory",
        "scope": "every experiment whose record sim/report/selection.json selects for the "
                 "characterization report (the claimed measurements)",
        "pinned_pdk": {
            "open_pdks_revision": pdk_rev,
            "pinned_in": ["sim/harness.py PINNED_PDK_REV", ".github/workflows/selftest.yml GF180_PDK_REV"],
            "enforced_by": ["sim/ci_prereqs.py", "sim/ci_pdk_rev_check.sh"],
        },
        "cold_start": {
            "prerequisites": "sim/README.md (ngspice, xschem 3.4.7, klt, pinned PDK via volare)",
            "all_experiments": "sim/characterize.sh",
            "fast_smoke": "sim/selftest.sh",
        },
        "experiments": entries,
        "checks": [
            "each selected experiment has exactly one run_*.py driver and at least one committed "
            "testbench/tb_*.spice of its own, or (BENCH_REUSE) pins the committed bench of the "
            "experiment it reuses, with the reuse documented in its README",
            "each experiment README documents a 'python3 sim/<exp>/<driver>' invocation",
            "sim/harness.py PINNED_PDK_REV equals selftest.yml GF180_PDK_REV",
            "sim/characterize.sh, sim/selftest.sh, sim/ci_prereqs.py, sim/ci_pdk_rev_check.sh exist",
        ],
    }


# -------------------------------------------------------------- item 10 ----
def repo_hygiene(root: Path) -> dict:
    readme = read(root, "README.md").decode()
    for heading in ("## Target specification", "## Reproducing the results", "## License"):
        if not re.search(rf"(?m)^{re.escape(heading)}", readme):
            raise AttestError(f"README.md lacks a '{heading}' section")
    if not re.match(r"# gf180-opamp\n\nA two-stage", readme):
        raise AttestError("README.md does not open by stating what the block is")
    if "spec/target-spec.md" not in readme:
        raise AttestError("README.md does not link spec/target-spec.md")
    spec = read(root, "spec/target-spec.md").decode()
    if len(re.findall(r"(?m)^\| .*Parameter .*\|", spec)) < 1 or len(re.findall(r"(?m)^\|", spec)) < 10:
        raise AttestError("spec/target-spec.md carries no spec table")
    lic = read(root, "LICENSE")
    if b"Apache License" not in lic:
        raise AttestError("LICENSE is not the Apache License text")
    for needle in ("sim/characterize.sh", "sim/selftest.sh", "manifests/t1_attest.py"):
        if needle not in readme:
            raise AttestError(f"README.md reproduction section does not mention {needle}")
    wfs = []
    for w in WORKFLOWS:
        wfs.append({"path": w, "content_hash": sha(read(root, w))})
    sig = read(root, ".github/workflows/signoff.yml").decode()
    for needle in ("characterization_report.py --check", "check_integrator.py", "t1_attest.py --check",
                   "cmp -s"):
        if needle not in sig:
            raise AttestError(f"signoff.yml no longer runs '{needle}'")
    return {
        "schema_version": 1,
        "t1_item": 10,
        "subject": "repo-hygiene audit record",
        "readme": {
            "path": "README.md",
            "audited_sections": ["block description (opening paragraph)", "Target specification",
                                  "Reproducing the results", "License"],
            "hash_note": "README.md is edited often and is deliberately not hash-pinned; "
                         "t1_attest.py --check re-audits the sections in CI",
        },
        "spec_table": {"path": "spec/target-spec.md", "note": "full per-row table; README links it"},
        "license": {"path": "LICENSE", "kind": "Apache-2.0", "content_hash": sha(lic)},
        "ci": {
            "workflows": wfs,
            "keeps_valid": [
                "selftest.yml: harness/extraction unit tests, pinned tools and PDK, report and spec-citation checks",
                "signoff.yml: characterization report + evidence sidecar freshness, integrator view, "
                "T1 attestation freshness, byte-reproducible klt signoff record",
            ],
        },
        "checks": [
            "README opens with a block description and has Target specification, Reproducing the results and License sections",
            "README links spec/target-spec.md and that file carries a table",
            "LICENSE is the Apache License text",
            "both workflows exist; signoff.yml still runs the report, integrator, attestation and byte-reproducibility gates",
        ],
    }


BUILDERS = {
    1: ("design-sources", design_sources, "every design source and the derived netlist are inventoried; "
        "the netlist is regenerated from the schematic and checked in CI"),
    9: ("testbench-inventory", testbench_inventory, "every claimed measurement's testbench is committed with a "
        "documented invocation; PDK revision pinned and CI-enforced"),
    10: ("repo-hygiene", repo_hygiene, "README (block, spec table link, reproduction), license and CI audited"),
}


def outputs(root: Path) -> dict[str, str]:
    out: dict[str, str] = {}
    for item, (name, fn, summary) in sorted(BUILDERS.items()):
        inv = dump(fn(root))
        rel = f"{ATTEST}/{name}.json"
        out[rel] = inv
        env = {
            "schema_version": 1,
            "kind": "generic",
            "status": "pass",
            "t1_item": item,
            "summary": summary,
            "source": rel,
            "provenance": {
                "klt_version": None, "klayout_version": None, "pdk": None, "deck": None,
                "input": {
                    "content_hash": sha(inv.encode()),
                    "path": {"path": rel, "scope": "repo"},
                },
            },
        }
        out[f"{ATTEST}/item-{item}.evidence.json"] = dump(env)
    return out


def manifest_pins_ok(root: Path, outs: dict[str, str]) -> list[str]:
    man = json.loads(read(root, MANIFEST))
    bad = []
    for item, (name, _fn, _s) in sorted(BUILDERS.items()):
        ent = man.get("evidence", {}).get(str(item))
        want_file = f"{ATTEST}/item-{item}.evidence.json"
        want_hash = sha(outs[f"{ATTEST}/{name}.json"].encode())
        if not ent or ent.get("file") != want_file or ent.get("content_hash") != want_hash:
            bad.append(f"{MANIFEST} evidence[{item}] must be {{file: {want_file}, content_hash: {want_hash}}}")
    return bad


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--check", action="store_true", help="verify only; write nothing")
    ap.add_argument("--root", default=str(Path(__file__).resolve().parent.parent))
    args = ap.parse_args(argv)
    root = Path(args.root)
    try:
        outs = outputs(root)
    except AttestError as exc:
        print(f"t1_attest: check failed: {exc}", file=sys.stderr)
        return 2
    if args.check:
        drift = []
        for rel, text in outs.items():
            p = root / rel
            if not p.is_file() or p.read_text() != text:
                drift.append(rel)
        drift += manifest_pins_ok(root, outs)
        if drift:
            print("t1_attest: stale (run `python3 manifests/t1_attest.py`, repin " + MANIFEST + "):",
                  file=sys.stderr)
            for d in drift:
                print(f"  {d}", file=sys.stderr)
            return 1
        print("t1_attest: attestations for items 1, 9, 10 are current")
        return 0
    (root / ATTEST).mkdir(parents=True, exist_ok=True)
    for rel, text in outs.items():
        (root / rel).write_text(text)
    man = json.loads(read(root, MANIFEST))
    for item, (name, _fn, _s) in sorted(BUILDERS.items()):
        man.setdefault("evidence", {})[str(item)] = {
            "file": f"{ATTEST}/item-{item}.evidence.json",
            "content_hash": sha(outs[f"{ATTEST}/{name}.json"].encode()),
        }
    man["evidence"] = dict(sorted(man["evidence"].items(), key=lambda kv: int(kv[0])))
    (root / MANIFEST).write_text(dump(man))
    print("t1_attest: wrote attestations and re-pinned " + MANIFEST)
    return 0


if __name__ == "__main__":
    sys.exit(main())
