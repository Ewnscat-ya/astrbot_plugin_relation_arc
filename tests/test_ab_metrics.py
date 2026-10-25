"""Strict evaluation is distinct from the production compatibility parser."""
import importlib.util
import json
from pathlib import Path
import unittest

ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location('ab_metrics_tool', ROOT / 'tools/model_ab_test.py')
ab = importlib.util.module_from_spec(spec)
spec.loader.exec_module(ab)


def verdict(version=3):
    return '<relation_judgment>' + json.dumps(dict(schema_version=version, fact_effects=[],
        relationship_proposal=None, interaction_safety_proposal=None)) + '</relation_judgment>'


class StrictMetricsTests(unittest.TestCase):
    def test_legacy_schema_is_not_schema3_compliance(self):
        for version in (1, 2):
            self.assertFalse(ab._measure(verdict(version), '')['protocol_valid'])

    def test_first_line_is_part_of_validity(self):
        self.assertFalse(ab._measure('正文\n' + verdict(), '')['protocol_valid'])

    def test_multiline_control_block_is_not_first_line(self):
        self.assertFalse(ab._measure(verdict().replace('{', '{\n', 1), '')['protocol_valid'])

    def test_duplicate_and_dangling_control_block_rejected(self):
        for suffix in (verdict(), '<relation_judgment>', '</relation_judgment>'):
            self.assertFalse(ab._measure(verdict() + suffix, '')['protocol_valid'])

    def test_one_first_line_schema3_is_valid(self):
        self.assertTrue(ab._measure(verdict() + '\n正文', '')['protocol_valid'])

    def test_missing_metrics_are_unknown(self):
        summary = ab._summary({'records': [{'error': None}]})
        self.assertIsNone(summary.get('protocol_valid_rate'))
