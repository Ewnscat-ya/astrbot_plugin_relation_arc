import ast
from pathlib import Path
import sys
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'tools'))
import compatibility_probe as probe


class CompatibilityProbeTests(unittest.IsolatedAsyncioTestCase):
    def test_reference_renderer_rejects_executable_expressions(self):
        node = ast.parse('f"{dangerous()}"', mode='eval').body
        with self.assertRaises(ValueError):
            probe._render(node, {})

    async def test_probe_reports_limits_and_preserves_both_labels(self):
        with tempfile.TemporaryDirectory() as directory:
            source = Path(directory) / 'reference.py'
            source.write_text('''
raise RuntimeError("never execute this source")
static_prompt = f"<FavorabilityPlugin>{mode_instruction}</FavorabilityPlugin>"
dynamic_prompt = f"<FavourContext>{user_id}</FavourContext>"
self.favour_pattern = re.compile(r"\\[好感度 持平\\]", re.IGNORECASE)
''', encoding='utf-8')
            result = await probe.probe(ROOT, source, Path(directory) / 'report.json')
            self.assertEqual(8, len(result['records']))
            self.assertEqual('UNKNOWN', result['feedback_plugin_identity'])
            self.assertTrue(all(r['synthetic_label_cleaning'] for r in result['records']))
            self.assertEqual(4, sum(r['both_cleaning_orders_tested'] for r in result['records']))
