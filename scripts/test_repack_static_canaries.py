"""Storage revisions must preserve evidence and cannot alter experimental inputs."""

from __future__ import annotations

import gzip
import hashlib
import tempfile
import unittest
from pathlib import Path

from repack_static_canaries import transcode, validate_storage_revision
from simulation.artifacts import read_saved


class RepackTests(unittest.TestCase):
    """Exercise actual compression and rejected parameter changes without running solvers."""

    def test_round_trip_and_corrupt_source_rejection(self) -> None:
        payload = b'{"snapshot":"same logical bytes"}\n' * 100
        with tempfile.TemporaryDirectory() as parent:
            root = Path(parent)
            source, target = root / "run.json.gz", root / "copy.json.xz"
            source.write_bytes(gzip.compress(payload, mtime=0))
            original = source.read_bytes()
            expected = {
                "stored_sha256": hashlib.sha256(original).hexdigest(),
                "logical_sha256": hashlib.sha256(payload).hexdigest(),
                "logical_bytes": len(payload),
                "stored_bytes": len(original),
            }
            result = transcode(source, target, expected)
            self.assertEqual(read_saved(target), payload)
            self.assertEqual(source.read_bytes(), original)
            self.assertEqual(result["logical_sha256"], expected["logical_sha256"])
            with self.assertRaises(FileExistsError):
                transcode(source, target, expected)
            with self.assertRaisesRegex(ValueError, "stored hash"):
                transcode(source, root / "bad.xz", {**expected, "stored_sha256": "invalid"})
            self.assertFalse((root / "bad.xz").exists())

    def test_only_storage_changes_are_allowed(self) -> None:
        old = {"duration": 2100, "run_limits": {"archive_codec": "gzip", "run_wall_seconds": 900}}
        new = {"duration": 2100, "run_limits": {"archive_codec": "xz", "run_wall_seconds": 900}}
        validate_storage_revision(old, new)
        with self.assertRaisesRegex(ValueError, "only an archive codec"):
            validate_storage_revision(old, {**new, "duration": 3900})
        with self.assertRaisesRegex(ValueError, "only an archive codec"):
            validate_storage_revision(old, {**new, "run_limits": {"archive_codec": "xz", "run_wall_seconds": 1200}})


if __name__ == "__main__":
    unittest.main()
