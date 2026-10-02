import os
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))

from src.repository import Repository
from src.service import Service
from src.domain import ConflictError, DomainError


class FailureTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
        self.tmp.close()
        self.repo = Repository(self.tmp.name)
        self.repo.initialize()
        self.service = Service(self.repo)
        self.payload = {
            "source_id": "SRC-2",
            "contaminant": "bacteria",
            "detected_at": "2026-09-27T07:00:00+00:00",
            "concentration": 30,
            "limit": 10,
            "zone_ids": ["Z-3"],
            "population": 1000,
            "complaints": 2,
        }

    def tearDown(self):
        os.unlink(self.tmp.name)

    def test_duplicate_and_notification_dedup(self):
        item = self.service.create_item(self.payload, "a", "analyst")
        with self.assertRaises(ConflictError):
            self.service.create_item(self.payload, "a", "analyst")
        item = self.service.act(item["id"], "verify", {"sample_count": 1}, "a", "analyst", item["version"])
        item = self.service.act(item["id"], "advise", {"notice_id": "N-2", "kind": "boil", "message": "煮沸"}, "d", "dispatcher", item["version"])
        with self.assertRaises(DomainError) as context:
            self.service.act(item["id"], "advise", {"notice_id": "N-2", "kind": "boil", "message": "煮沸"}, "d", "dispatcher", item["version"])
        self.assertEqual(context.exception.code, "duplicate_notification")

    def test_permission_and_version_conflict(self):
        item = self.service.create_item(self.payload, "a", "analyst")
        with self.assertRaises(DomainError) as context:
            self.service.act(item["id"], "verify", {"sample_count": 1}, "l", "lab", item["version"])
        self.assertEqual(context.exception.status, 403)
        item = self.service.act(item["id"], "verify", {"sample_count": 1}, "a", "analyst", item["version"])
        with self.assertRaises(ConflictError):
            self.service.act(item["id"], "advise", {"notice_id": "N-3", "kind": "boil", "message": "煮沸"}, "d", "dispatcher", item["version"] - 1)


if __name__ == "__main__":
    unittest.main()
