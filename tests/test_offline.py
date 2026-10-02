import os
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))

from src.repository import Repository
from src.service import Service
from src.domain import DomainError


def _base_payload():
    return {
        "source_id": "SRC-9",
        "contaminant": "nitrate",
        "detected_at": "2026-09-27T06:00:00+00:00",
        "concentration": 20,
        "limit": 10,
        "zone_ids": ["Z-1", "Z-2"],
        "population": 5000,
        "complaints": 4,
    }


class OfflineRecordTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
        self.tmp.close()
        self.repo = Repository(self.tmp.name)
        self.repo.initialize()
        self.service = Service(self.repo)
        self.item = self.service.create_item(_base_payload(), "a", "analyst")
        self.item = self.service.act(self.item["id"], "verify", {"sample_count": 2}, "a", "analyst", self.item["version"])
        self.item = self.service.act(self.item["id"], "advise", {"notice_id": "N-9", "kind": "boil", "message": "煮沸"}, "d", "dispatcher", self.item["version"])

    def tearDown(self):
        os.unlink(self.tmp.name)

    def _flush(self, zone_id, expected_version, client_id, recorded_at):
        return {
            "client_record_id": client_id,
            "action": "flush",
            "payload": {"zone_id": zone_id},
            "expected_version": expected_version,
            "recorded_at": recorded_at,
        }

    def test_zone_merge_keeps_both_flushes(self):
        # 班组 A 在线冲洗 Z-1
        self.item = self.service.act(self.item["id"], "flush", {"zone_id": "Z-1"}, "f1", "field_operator", self.item["version"])
        self.assertEqual(self.item["version"], 4)
        # 班组 B 两小时前导出的版本还是 3，离线冲洗 Z-2，回所补录
        summary = self.service.submit_offline_records(
            self.item["id"],
            [self._flush("Z-2", 3, "c-b", "2026-09-27T08:00:00+00:00")],
            "f2",
            "field_operator",
        )
        self.assertEqual(len(summary["applied"]), 1)
        self.assertEqual(summary["applied"][0]["version_after"], 5)
        item = self.service.get_item(self.item["id"])
        zones = [a["zone_id"] for a in item["payload"]["response_actions"] if a["type"] == "flush"]
        self.assertIn("Z-1", zones)
        self.assertIn("Z-2", zones)
        self.assertEqual(len(zones), 2)

    def test_replay_is_idempotent_no_loss(self):
        self.item = self.service.act(self.item["id"], "flush", {"zone_id": "Z-1"}, "f1", "field_operator", self.item["version"])
        records = [self._flush("Z-2", 3, "c-b", "2026-09-27T08:00:00+00:00")]
        first = self.service.submit_offline_records(self.item["id"], records, "f2", "field_operator")
        self.assertEqual(len(first["applied"]), 1)
        # 网络恢复后按原始登记顺序重放同一条，已写好的不能再丢/再重复
        second = self.service.submit_offline_records(self.item["id"], records, "f2", "field_operator")
        self.assertEqual(len(second["skipped"]), 1)
        self.assertEqual(len(second["applied"]), 0)
        item = self.service.get_item(self.item["id"])
        zones = [a["zone_id"] for a in item["payload"]["response_actions"] if a["type"] == "flush"]
        self.assertEqual(zones.count("Z-2"), 1)
        self.assertIn("Z-1", zones)

    def test_failed_records_become_pending_and_replay_in_order(self):
        # 补录顺序：冲洗 Z-1、消毒 Z-1、合格样本 S-1、不合格样本 S-2
        records = [
            {"client_record_id": "r1", "action": "flush", "payload": {"zone_id": "Z-1"}, "expected_version": 3, "recorded_at": "2026-09-27T08:00:00+00:00"},
            {"client_record_id": "r2", "action": "disinfect", "payload": {"zone_id": "Z-1", "completed": True}, "expected_version": 3, "recorded_at": "2026-09-27T08:05:00+00:00"},
            {"client_record_id": "r3", "action": "sample", "payload": {"sample_id": "S-1", "zone_id": "Z-1", "concentration": 2}, "expected_version": 3, "recorded_at": "2026-09-27T08:10:00+00:00"},
            {"client_record_id": "r4", "action": "sample", "payload": {"sample_id": "S-2", "zone_id": "Z-1", "concentration": 99}, "expected_version": 3, "recorded_at": "2026-09-27T08:15:00+00:00"},
        ]
        summary = self.service.submit_offline_records(self.item["id"], records, "f2", "field_operator")
        self.assertEqual(len(summary["applied"]), 3)
        self.assertEqual(len(summary["pending"]), 1)
        self.assertEqual(summary["pending"][0]["error"], "quality_not_met")
        # 待重试项按原始登记顺序重放；r1~r3 已写好，不能再丢
        replay = self.service.replay_offline_records(self.item["id"], "f2", "field_operator")
        self.assertEqual(len(replay["pending"]), 1)
        self.assertEqual(replay["pending"][0]["error"], "quality_not_met")
        item = self.service.get_item(self.item["id"])
        self.assertTrue(any(a["type"] == "flush" and a["zone_id"] == "Z-1" for a in item["payload"]["response_actions"]))
        self.assertTrue(any(a["type"] == "disinfect" and a["zone_id"] == "Z-1" for a in item["payload"]["response_actions"]))
        sample_ids = [s["sample_id"] for s in item["payload"]["sample_results"]]
        self.assertIn("S-1", sample_ids)
        self.assertNotIn("S-2", sample_ids)

    def test_unqualified_sample_refused(self):
        records = [
            {"client_record_id": "r1", "action": "flush", "payload": {"zone_id": "Z-1"}, "expected_version": 3, "recorded_at": "2026-09-27T08:00:00+00:00"},
            {"client_record_id": "r2", "action": "disinfect", "payload": {"zone_id": "Z-1", "completed": True}, "expected_version": 3, "recorded_at": "2026-09-27T08:05:00+00:00"},
            {"client_record_id": "r3", "action": "sample", "payload": {"sample_id": "S-bad", "zone_id": "Z-1", "concentration": 50}, "expected_version": 3, "recorded_at": "2026-09-27T08:10:00+00:00"},
        ]
        summary = self.service.submit_offline_records(self.item["id"], records, "f2", "field_operator")
        self.assertEqual(len(summary["pending"]), 1)
        self.assertEqual(summary["pending"][0]["error"], "quality_not_met")
        item = self.service.get_item(self.item["id"])
        self.assertEqual(item["payload"].get("sample_results", []), [])
        pending = self.service.list_offline_records(self.item["id"], status="pending")
        self.assertEqual(len(pending), 1)
        self.assertEqual(pending[0]["client_record_id"], "r3")
        self.assertEqual(pending[0]["last_error"], "quality_not_met")

    def test_audit_shows_original_and_merged(self):
        records = [self._flush("Z-2", 3, "c-b", "2026-09-27T08:00:00+00:00")]
        self.service.submit_offline_records(self.item["id"], records, "f2", "field_operator")
        item = self.service.get_item(self.item["id"])
        events = [e for e in item["audit"] if e["event_type"] == "offline_record_applied"]
        self.assertEqual(len(events), 1)
        payload = events[0]["payload"]
        self.assertEqual(payload["client_record_id"], "c-b")
        self.assertEqual(payload["original"]["action"], "flush")
        self.assertEqual(payload["original"]["payload"]["zone_id"], "Z-2")
        self.assertEqual(payload["original"]["expected_version"], 3)
        self.assertEqual(payload["merged"]["zone_id"], "Z-2")
        self.assertEqual(payload["merged"]["type"], "flush")
        self.assertIn("version_before", payload)
        self.assertIn("version_after", payload)

    def test_replay_preserves_written_records_after_partial_failure(self):
        # 第一批只补 flush Z-1（成功），第二批补 flush Z-2 与不合格样本
        first = self.service.submit_offline_records(
            self.item["id"],
            [self._flush("Z-1", 3, "c-1", "2026-09-27T08:00:00+00:00")],
            "f2",
            "field_operator",
        )
        self.assertEqual(len(first["applied"]), 1)
        second = self.service.submit_offline_records(
            self.item["id"],
            [
                self._flush("Z-2", 3, "c-2", "2026-09-27T08:05:00+00:00"),
                {"client_record_id": "c-3", "action": "sample", "payload": {"sample_id": "S-x", "zone_id": "Z-2", "concentration": 77}, "expected_version": 3, "recorded_at": "2026-09-27T08:10:00+00:00"},
            ],
            "f2",
            "field_operator",
        )
        self.assertEqual(len(second["applied"]), 1)
        self.assertEqual(len(second["pending"]), 1)
        # 重放后已写好的 Z-1、Z-2 都还在，S-x 仍被拒绝
        replay = self.service.replay_offline_records(self.item["id"], "f2", "field_operator")
        self.assertEqual(len(replay["pending"]), 1)
        item = self.service.get_item(self.item["id"])
        zones = [a["zone_id"] for a in item["payload"]["response_actions"] if a["type"] == "flush"]
        self.assertIn("Z-1", zones)
        self.assertIn("Z-2", zones)
        self.assertNotIn("S-x", [s["sample_id"] for s in item["payload"].get("sample_results", [])])


if __name__ == "__main__":
    unittest.main()
