"""Protect pairing, floor connectivity and case-source provenance of pilot inputs."""

from __future__ import annotations

import copy
import json
import math
import tempfile
import unittest
from pathlib import Path
from typing import Any
from unittest.mock import patch

import materialize_korean_case_pilot as pilot


class KoreanCasePilotTest(unittest.TestCase):
    """Verify scientific input contracts before expensive solver runs."""

    @classmethod
    def setUpClass(cls) -> None:
        """Materialize independent directories to inspect reproducible inputs."""
        cls.temporary = tempfile.TemporaryDirectory()
        cls.root = Path(cls.temporary.name)
        cls.first = cls.root / "first"
        cls.manifest = pilot.materialize(cls.first)
        cls.second = cls.root / "second"
        pilot.materialize(cls.second)

    @classmethod
    def tearDownClass(cls) -> None:
        """Dispose temporary inputs without touching evidence directories."""
        cls.temporary.cleanup()

    def case(self, condition: str, seed: int = 2026111701) -> dict[str, Any]:
        """Read the concrete external input used by all policies."""
        entry = next(
            e
            for e in self.manifest["entries"]
            if e["condition"] == condition and e["seed"] == seed
        )
        return json.loads((self.first / entry["scenario"]).read_text())

    def test_reproduction_and_historical_source_identity(self) -> None:
        """Identical recipes reproduce bytes and don't alter the application."""
        first = {
            p.relative_to(self.first): pilot.file_sha(p)
            for p in self.first.rglob("*")
            if p.is_file()
        }
        second = {
            p.relative_to(self.second): pilot.file_sha(p)
            for p in self.second.rglob("*")
            if p.is_file()
        }
        self.assertEqual(first, second)
        self.assertEqual(len(self.manifest["entries"]), 36)
        self.assertEqual(self.manifest["planned_policy_runs_per_timing_mode"], 144)
        self.assertEqual(
            self.manifest["source_hash"], pilot.source_manifest()["source_hash"]
        )

    def test_no_overwrite(self) -> None:
        """Repeated execution cannot replace an existing input bundle."""
        digest = pilot.file_sha(self.first / "manifest.json")
        with self.assertRaises(FileExistsError):
            pilot.materialize(self.first)
        self.assertEqual(pilot.file_sha(self.first / "manifest.json"), digest)

    def test_physical_treatments_keep_trace_and_initial_state(self) -> None:
        """Physical changes retain external demand and starting robot state."""
        for child, parent in [
            ("KR01_restricted", "KR01_clear"),
            ("KR02_speed_mix", "KR02_uniform"),
            ("KR03_burst4_slot1", "KR03_burst4"),
            ("KR04_3f_cabin2", "KR04_3f_cabin1"),
            ("KR04_3f_distributed", "KR04_3f_cabin2"),
        ]:
            for seed in (2026111701, 2026111702, 2026111703):
                a, b = self.case(child, seed), self.case(parent, seed)
                self.assertEqual(a["workload"], b["workload"])
                self.assertEqual(a["initial_robots"], b["initial_robots"])

    def test_burst_preserves_jobs_noise_slack_and_warmup(self) -> None:
        """Arrival compression does not create extra jobs or favorable noise."""
        for seed in (2026111701, 2026111702, 2026111703):
            a = self.case("KR03_smooth", seed)["workload"]["jobs"]
            b = self.case("KR03_burst4", seed)["workload"]["jobs"]
            self.assertEqual(len(a), len(b))
            for old, new in zip(a, b, strict=True):
                self.assertEqual(
                    {k: v for k, v in old.items() if k not in {"at", "deadline"}},
                    {k: v for k, v in new.items() if k not in {"at", "deadline"}},
                )
                self.assertAlmostEqual(
                    old["deadline"] - old["at"], new["deadline"] - new["at"]
                )
                if old["at"] < 300:
                    self.assertEqual(old, new)

    def test_three_floors_and_nominal_charging_power(self) -> None:
        """Three-floor inputs connect and charger relocation preserves capacity."""
        for name in ("KR04_3f_cabin1", "KR04_3f_cabin2", "KR04_3f_distributed"):
            raw = self.case(name)
            self.assertEqual(
                {s["floor"] for s in raw["factory"]["stations"]}, {1, 2, 3}
            )
            self.assertEqual(len(raw["factory"]["stations"]), 6)
            active = [c for c in raw["factory"]["chargers"] if c["is_active"]]
            self.assertEqual(sum(c["capacity"] for c in active), 3)
            self.assertEqual(sum(c["capacity"] * c["charge_rate"] for c in active), 1.5)
            self.assertEqual(pilot.validate_routes(pilot.Scenario.from_dict(raw)), 72)
        distributed = self.case("KR04_3f_distributed")["factory"]["chargers"]
        self.assertEqual({c["floor"] for c in distributed if c["is_active"]}, {1, 2, 3})

    def test_source_linkage_and_realization_is_synthetic(self) -> None:
        """Every condition keeps a source and an explicit non-replica claim."""
        sources = json.loads((pilot.DATA / "sources.json").read_text())
        ids = {s["id"] for s in sources["sources"]}
        self.assertTrue(all(e["source_id"] in ids for e in self.manifest["entries"]))
        self.assertEqual(
            self.manifest["source_claim"], "case-inspired synthetic; not a site replica"
        )
        a, b = self.case("KR02_uniform"), self.case("KR02_speed_mix")
        speeds_a = [r["move_speed"] for r in a["factory"]["robots"]]
        speeds_b = [r["move_speed"] for r in b["factory"]["robots"]]
        self.assertTrue(math.isclose(sum(speeds_a), sum(speeds_b)))
        self.assertNotEqual(speeds_a, speeds_b)

    def test_restricted_route_stays_physically_reachable(self) -> None:
        """Both restriction and restoration retain service/charging reachability."""
        raw = self.case("KR01_restricted")
        for event in raw["interventions"]:
            changed = copy.deepcopy(raw)
            next(
                e
                for e in changed["factory"]["edges"]
                if str(e["id"]) == event["target"]
            )["width"] = event["value"]
            self.assertEqual(
                pilot.validate_routes(pilot.Scenario.from_dict(changed)), 48
            )

    def test_unknown_treatment_rejected_before_output_creation(self) -> None:
        """A typo cannot silently become an unchanged treatment."""
        data = self.root / "invalid_recipe"
        data.mkdir()
        design = json.loads((pilot.DATA / "design.json").read_text())
        design["conditions"][0]["parameters"]["floor_typo"] = 3
        (data / "design.json").write_text(json.dumps(design))
        (data / "sources.json").write_bytes((pilot.DATA / "sources.json").read_bytes())
        destination = self.root / "must_not_exist"
        with (
            patch.object(pilot, "DATA", data),
            self.assertRaisesRegex(ValueError, "unknown family or parameter"),
        ):
            pilot.materialize(destination)
        self.assertFalse(destination.exists())


if __name__ == "__main__":
    unittest.main()
