"""Offline test of `sim/characterize.sh`'s driver routing (issue #167).

The real wrapper script is copied into a scratch directory and run against
stub drivers: tiny Python scripts at the exact relative paths the wrapper
calls, each appending its argv to a JSON-lines log (and exiting non-zero on
demand). No simulator, no PDK, no fleet campaign; nothing is written to the
repo.

What must hold:
  * every driver invocation receives the wrapper's common forwarded flags
    verbatim (--backend, --batch-*);
  * only the offset driver receives `--grid full`, pinned by the wrapper
    itself, and no other driver sees a `--grid` option at all;
  * the offset banner states the actual campaign size (45-point grid,
    13500 units), not the nominal 1500-unit population;
  * the report step runs last with exactly `--latest --update-manifest`
    and no forwarded driver flags;
  * a failing offset driver stops the run (`set -e`): no later driver and
    no report/publication step executes.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path

HERE = Path(__file__).resolve().parent
WRAPPER = HERE / "characterize.sh"

#: driver relative paths in wrapper invocation order (the report step last).
DRIVERS = [
    "gain-gbw-pm/run_gain_gbw_pm.py",
    "offset-mc/run_offset_mc.py",
    "noise/run_noise.py",
    "cmrr/run_cmrr.py",
    "psrr/run_psrr.py",
    "slew-swing-power/run_slew_swing_power.py",
    "input-common-mode/run_input_common_mode.py",
]
OFFSET = "offset-mc/run_offset_mc.py"
REPORT = "report/characterization_report.py"

#: common flags the wrapper documents as safe to forward to every driver.
COMMON_FLAGS = [
    "--backend", "local",
    "--batch-runner-version-check", "warn",
    "--batch-submit-retries", "4",
    "--batch-retry-wait-s", "7.5",
]


def stub_source(rel: str) -> str:
    """A stub driver at `rel`: log argv; exit 3 when named as the failure."""
    return (
        "import json, os, sys\n"
        f"entry = {{'driver': {rel!r}, 'argv': sys.argv[1:]}}\n"
        "with open(os.environ['WRAPPER_TEST_LOG'], 'a') as f:\n"
        "    f.write(json.dumps(entry) + '\\n')\n"
        f"if os.environ.get('WRAPPER_TEST_FAIL_DRIVER') == {rel!r}:\n"
        "    sys.exit(3)\n"
    )


class CharacterizeWrapperTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.sim = Path(self._tmp.name) / "sim"
        self.sim.mkdir()
        shutil.copy2(WRAPPER, self.sim / "characterize.sh")
        for rel in DRIVERS + [REPORT]:
            target = self.sim / rel
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text(stub_source(rel))
        self.log = Path(self._tmp.name) / "argv.jsonl"

    def _run(self, *, fail_driver: str | None = None) -> subprocess.CompletedProcess:
        env = dict(os.environ, WRAPPER_TEST_LOG=str(self.log))
        if fail_driver is not None:
            env["WRAPPER_TEST_FAIL_DRIVER"] = fail_driver
        return subprocess.run(
            ["bash", "characterize.sh", *COMMON_FLAGS],
            cwd=self.sim, env=env, capture_output=True, text=True, timeout=120,
        )

    def _entries(self) -> list[dict]:
        return [json.loads(line) for line in self.log.read_text().splitlines()]

    def test_common_flags_reach_every_driver_and_only_offset_gets_the_grid(self):
        proc = self._run()
        self.assertEqual(proc.returncode, 0, proc.stderr)
        entries = self._entries()
        self.assertEqual([e["driver"] for e in entries], DRIVERS + [REPORT])
        for entry in entries[:-1]:
            for flag in COMMON_FLAGS:
                self.assertIn(flag, entry["argv"], entry["driver"])
        offset = entries[DRIVERS.index(OFFSET)]
        self.assertIn("--grid", offset["argv"])
        self.assertEqual(offset["argv"][offset["argv"].index("--grid") + 1], "full")
        for entry in entries[:-1]:
            if entry["driver"] != OFFSET:
                self.assertNotIn("--grid", entry["argv"], entry["driver"])
        self.assertEqual(entries[-1]["argv"], ["--latest", "--update-manifest"])

    def test_offset_banner_states_the_full_grid_campaign(self):
        proc = self._run()
        self.assertEqual(proc.returncode, 0, proc.stderr)
        banners = [ln for ln in proc.stdout.splitlines() if ln.startswith("== sim/offset-mc:")]
        self.assertEqual(len(banners), 1)
        self.assertIn("45-point", banners[0])
        self.assertIn("13500", banners[0])

    def test_offset_failure_stops_subsequent_drivers_and_publication(self):
        proc = self._run(fail_driver=OFFSET)
        self.assertNotEqual(proc.returncode, 0)
        ran = [e["driver"] for e in self._entries()]
        self.assertEqual(ran, DRIVERS[: DRIVERS.index(OFFSET) + 1])


if __name__ == "__main__":
    unittest.main()
