"""Formal MIS-117 bridge-contract checks for the real Pages bundle.

The node harness (pages_app_harness.mjs) drives pages/settings/app.js in a
synthetic DOM — the host's endpoint validation rules (no '?' in bridge
endpoints), separate-params calls, inflight de-dup keyed on endpoint+params,
working page navigation and config-conflict recovery. Real-browser
verification of the same flows stays with the real-host acceptance task."""
from pathlib import Path
import shutil
import subprocess
import unittest

ROOT = Path(__file__).resolve().parents[1]


class PagesScriptBehaviourTests(unittest.TestCase):
    def test_pages_script_behaviour(self):
        node = shutil.which("node")
        if not node:
            self.skipTest("node unavailable for pages script harness")
        proc = subprocess.run(
            [node, str(ROOT / "tests" / "pages_app_harness.mjs")],
            capture_output=True, text=True, timeout=120, cwd=str(ROOT))
        self.assertEqual(
            0, proc.returncode,
            f"node harness failed:\n{proc.stdout}\n{proc.stderr}")
        self.assertNotIn("FAIL ", proc.stdout, proc.stdout)


if __name__ == "__main__":
    unittest.main()
