import json
import tempfile
import unittest
from pathlib import Path

from report import export_json


class ExportTest(unittest.TestCase):
    def test_json_round_trip(self):
        rows = [{"name": "alpha", "count": 3}]
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "out.json"
            export_json(rows, str(path))
            self.assertEqual(json.loads(path.read_text()), rows)


if __name__ == "__main__":
    unittest.main()
