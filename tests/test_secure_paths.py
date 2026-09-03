import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import secure_paths


class SecurePathsTests(unittest.TestCase):
    def test_atomic_json_retries_short_writes(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "managed"
            tree = secure_paths.ManagedTree(root)
            original_write = os.write

            def short_write(fd, payload):
                return original_write(fd, payload[:3])

            with mock.patch.object(secure_paths.os, "write", side_effect=short_write):
                tree.atomic_json(("record.json",), {"value": "complete"})

            self.assertEqual(json.loads((root / "record.json").read_text()), {"value": "complete"})

    def test_json_read_rejects_fifo_without_blocking(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "managed"
            root.mkdir()
            os.mkfifo(root / "record.json")
            code = (
                "from pathlib import Path; from secure_paths import ManagedTree; "
                f"ManagedTree(Path({str(root)!r})).read_json(('record.json',))"
            )

            result = subprocess.run(
                [sys.executable, "-c", code],
                cwd=Path(__file__).resolve().parents[1],
                text=True,
                capture_output=True,
                timeout=2,
            )

            self.assertNotEqual(result.returncode, 0)


if __name__ == "__main__":
    unittest.main()
