"""Reject invalid recipes, stale bundles, seed duplication and false completion."""

from __future__ import annotations

import copy
import json
import shutil
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import audit_korean_case_pilot as audit
import materialize_korean_case_pilot as pilot


class KoreanPreflightTest(unittest.TestCase):
    """Keep development execution explicit and inputs paired and immutable."""

    @classmethod
    def setUpClass(cls) -> None:
        """Generate one valid inventory and the reviewed design."""
        cls.temporary = tempfile.TemporaryDirectory()
        cls.root = Path(cls.temporary.name)
        cls.lock_patch = patch.object(audit, "LOCK_PARENT", cls.root)
        cls.lock_patch.start()
        cls.bundle = cls.root / "valid"
        cls.manifest = pilot.materialize(cls.bundle)
        cls.design = json.loads((pilot.DATA / "design.json").read_text())
        cls.sources = json.loads((pilot.DATA / "sources.json").read_text())

    @classmethod
    def tearDownClass(cls) -> None:
        """Remove only this test's own temporary directories."""
        cls.lock_patch.stop()
        cls.temporary.cleanup()

    def clone(self) -> Path:
        """Create an isolated bundle for a negative tampering check."""
        path = self.root / self._testMethodName
        shutil.copytree(self.bundle, path)
        return path

    def test_valid_matrix_is_not_d1_certification(self) -> None:
        """Correct files do not imply operational timing or significance."""
        result = audit.audit_bundle(self.bundle)
        self.assertEqual(result["inputs"], 36)
        self.assertEqual(result["planned_policy_runs_per_timing_mode"], 144)
        self.assertTrue(result["readiness"]["D1"].startswith("NOT_CERTIFIED"))

    def test_invalid_numeric_and_boolean_treatments_rejected(self) -> None:
        """Probabilities, floors and flags must have their actual domain types."""
        for change in (
            {"hotspot_share": -0.1},
            {"hotspot_share": 1.1},
            {"floors": True},
            {"burst_multiplier": 10},
            {"speed_mix": "false"},
            {"charger_slots": 0},
        ):
            design = copy.deepcopy(self.design)
            design["conditions"][0]["parameters"] = change
            with self.subTest(change=change), self.assertRaises(ValueError):
                pilot.validate_design(design, self.sources)

    def test_multiple_axes_rejected(self) -> None:
        """Changing load shape and slots cannot masquerade as one-factor contrast."""
        design = copy.deepcopy(self.design)
        row = next(c for c in design["conditions"] if c["id"] == "KR03_burst4")
        row["parameters"]["charger_slots"] = 1
        with self.assertRaisesRegex(ValueError, "exactly its declared axis"):
            pilot.validate_design(design, self.sources)

    def test_speed_mix_cannot_change_its_claimed_mean(self) -> None:
        """Incomplete repeated speed groups must not silently change the mean."""
        design = copy.deepcopy(self.design)
        next(f for f in design["families"] if f["id"] == "KR02")["robots"] = 7
        with self.assertRaisesRegex(ValueError, "four-speed groups"):
            pilot.validate_design(design, self.sources)

    def test_duplicate_policy_and_family_rejected(self) -> None:
        """Invalid denominators must fail before a directory is created."""
        for field in ("policies", "families"):
            design = copy.deepcopy(self.design)
            design[field].append(design[field][0])
            with self.subTest(field=field), self.assertRaises(ValueError):
                pilot.validate_design(design, self.sources)

    def test_unknown_common_or_family_fields_rejected(self) -> None:
        """Typos in physical inputs must not silently retain an old value."""
        for location in ("common", "family"):
            design = copy.deepcopy(self.design)
            target = design["common"] if location == "common" else design["families"][0]
            target["charge_rate_typo"] = 0.5
            with (
                self.subTest(location=location),
                self.assertRaisesRegex(ValueError, "input key"),
            ):
                pilot.validate_design(design, self.sources)

    def test_comparison_parent_cycle_rejected(self) -> None:
        """Two contrasts cannot silently remove the family's anchor."""
        design = copy.deepcopy(self.design)
        first = design["conditions"][0]
        first["compare_to"] = "KR01_restricted"
        first["changed_axis"] = "edge_width_at_900_to_1200"
        with self.assertRaisesRegex(ValueError, "parent cycle"):
            pilot.validate_design(design, self.sources)

    def test_settings_tampering_rejected(self) -> None:
        """Edited solver settings invalidate the prepared inventory."""
        bundle = self.clone()
        (bundle / "rh_settings.json").write_text('{"horizon": 99}')
        with self.assertRaisesRegex(ValueError, "support file hash mismatch"):
            audit.audit_bundle(bundle)

    def test_escape_and_duplicate_entry_rejected(self) -> None:
        """Inputs outside the bundle and repeated condition/seed pairs are invalid."""
        bundle = self.clone()
        m = json.loads((bundle / "manifest.json").read_text())
        m["entries"][0]["scenario"] = "../outside.json"
        (bundle / "manifest.json").write_text(json.dumps(m))
        with self.assertRaisesRegex(ValueError, "escapes"):
            audit.audit_bundle(bundle)
        m["entries"] = list(self.manifest["entries"]) + [self.manifest["entries"][0]]
        (bundle / "manifest.json").write_text(json.dumps(m))
        with self.assertRaisesRegex(ValueError, "condition/seed"):
            audit.audit_bundle(bundle)

    def test_stale_application_source_rejected(self) -> None:
        """Old inputs cannot silently be executed under a changed application."""
        bundle = self.clone()
        m = json.loads((bundle / "manifest.json").read_text())
        m["source_hash"] = "old-source"
        (bundle / "manifest.json").write_text(json.dumps(m))
        with self.assertRaisesRegex(ValueError, "application source differs"):
            audit.audit_bundle(bundle)

    def test_wrong_declared_denominator_rejected(self) -> None:
        """An incomplete matrix cannot advertise a larger completed experiment."""
        bundle = self.clone()
        m = json.loads((bundle / "manifest.json").read_text())
        m["planned_policy_runs_per_timing_mode"] = 999
        (bundle / "manifest.json").write_text(json.dumps(m))
        with self.assertRaisesRegex(ValueError, "denominator"):
            audit.audit_bundle(bundle)

    def test_restriction_restores_original_width(self) -> None:
        """Recovery must not widen the control corridor beyond its original state."""
        entry = next(
            e for e in self.manifest["entries"] if e["condition"] == "KR01_restricted"
        )
        raw = json.loads((self.bundle / entry["scenario"]).read_text())
        width = next(e["width"] for e in raw["factory"]["edges"] if e["id"] == 1)
        self.assertEqual(raw["interventions"][-1]["value"], width)
        self.assertEqual(width, 2.2)
        self.assertFalse(
            any(n["node_type"] == "intersection" for n in raw["factory"]["nodes"])
        )

    def test_control_has_matching_sham_layout_updates(self) -> None:
        """Match requests; geometry-specific draining remains part of treatment."""
        cases = {}
        for condition in ("KR01_clear", "KR01_restricted"):
            entry = next(
                e for e in self.manifest["entries"] if e["condition"] == condition
            )
            cases[condition] = json.loads((self.bundle / entry["scenario"]).read_text())
        control = cases["KR01_clear"]["interventions"]
        treatment = cases["KR01_restricted"]["interventions"]
        self.assertEqual(len(control), 2)
        self.assertEqual([e["value"] for e in control], [2.2, 2.2])
        self.assertEqual([e["value"] for e in treatment], [1.3, 2.2])
        self.assertEqual(
            [{k: v for k, v in e.items() if k != "value"} for e in control],
            [{k: v for k, v in e.items() if k != "value"} for e in treatment],
        )

    def test_executor_uses_exactly_one_seed_and_keeps_failed_denominator(self) -> None:
        """One concrete input gets four policies, never a second demand seed product."""
        calls = []

        def fake_suite(
            cases: tuple, seeds: tuple, output: Path, **kwargs: object
        ) -> dict:
            calls.append((cases, seeds, kwargs))
            output.mkdir()
            return {
                "status": "PARTIAL",
                "runs": [
                    {
                        "policy": p,
                        "status": "TIMING_COVERAGE_VIOLATION",
                        "failure": "diagnostic fixture",
                    }
                    for p in self.manifest["policies"]
                ],
            }

        output = self.root / "recorded_failure"
        with patch("simulation.evaluation.runner.run_suite", fake_suite):
            result = audit.execute(self.bundle, output, stage="d1-diagnostic", limit=1)
        self.assertEqual(len(calls), 1)
        cases, seeds, settings = calls[0]
        self.assertEqual(len(cases), 1)
        self.assertEqual(len(seeds), 1)
        self.assertTrue(cases[0].id.endswith(str(seeds[0])))
        self.assertTrue(settings["compress_artifacts"])
        self.assertEqual(settings["archive_codec"], "xz")
        self.assertEqual(result["planned_policy_runs"], 144)
        self.assertEqual(result["status"], "PARTIAL_PLAN_RECORDED")
        self.assertEqual(sum(j["status"] == "NOT_STARTED" for j in result["jobs"]), 35)
        with self.assertRaises(FileExistsError):
            audit.execute(self.bundle, output, stage="d1-diagnostic", limit=1)

    def test_second_launcher_rejected(self) -> None:
        """A concurrent invocation cannot contaminate operational timing."""
        output = self.root / "parallel_must_not_exist"
        with (
            audit.execution_lock(),
            self.assertRaisesRegex(ValueError, "another main launcher"),
        ):
            audit.execute(self.bundle, output, stage="d1-diagnostic", limit=1)
        self.assertFalse(output.exists())


if __name__ == "__main__":
    unittest.main()
