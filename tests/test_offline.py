import os
import sys
import tempfile
import threading
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))

from src.repository import Repository
from src.service import Service
from src.domain import DomainError


class OfflineCase(unittest.TestCase):
    prepare_counter = 0

    def setUp(self):
        self.tmp = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
        self.tmp.close()
        self.repo = Repository(self.tmp.name)
        self.repo.initialize()
        self.service = Service(self.repo)
        self.item = self._prepare_item(["Z-1", "Z-2"])

    def tearDown(self):
        os.unlink(self.tmp.name)

    def _prepare_item(self, zones, status="switched", tag=None):
        OfflineCase.prepare_counter += 1
        seq = OfflineCase.prepare_counter
        tag = tag or str(seq)
        item = self.service.create_item({
            "source_id": "SRC-OFF-" + tag,
            "contaminant": "nitrate",
            "detected_at": "2026-10-02T06:%02d:%02d+00:00" % (seq // 60, seq % 60),
            "concentration": 20,
            "limit": 10,
            "zone_ids": zones,
            "population": 5000,
        }, "analyst-1", "analyst")
        if status != "detected":
            item = self.service.act(item["id"], "verify", {"sample_count": 1}, "analyst-1", "analyst", item["version"])
        if status in ("advisory", "flushing", "switched", "disinfected", "sampled"):
            item = self.service.act(
                item["id"], "advise",
                {"notice_id": "N-OFF-" + tag, "kind": "boil", "message": "煮沸"},
                "disp-1", "dispatcher", item["version"],
            )
        if status in ("flushing", "switched", "disinfected", "sampled"):
            item = self.service.act(
                item["id"], "switch_source", {"alternate_source_id": "ALT-1"},
                "coord-1", "coordinator", item["version"],
            )
        if status in ("flushing", "disinfected", "sampled"):
            item = self.service.act(
                item["id"], "flush", {"record_id": "F-PREP-" + tag, "zone_id": zones[0]},
                "field-1", "field_operator", item["version"],
            )
        if status in ("disinfected", "sampled"):
            item = self.service.act(
                item["id"], "disinfect", {"zone_id": zones[0], "completed": True},
                "field-1", "field_operator", item["version"],
            )
        return item

    def _flushed_zones(self, item):
        return [a["zone_id"] for a in item["payload"]["response_actions"] if a.get("type") == "flush"]

    # ------------------------------------------------------------------

    def test_offline_flush_based_on_stale_version_merges_by_zone(self):
        base = self.item["version"]  # 回所时手上两小时前导出的版本
        result = self.service.register_offline_flush(self.item["id"], {
            "batch_id": "B-OLD",
            "expected_version": base,
            "records": [{"record_id": "F-OLD", "zone_id": "Z-1", "recorded_at": "2026-10-02T08:00:00+00:00"}],
        }, "field-1", "field_operator")
        self.assertEqual(result["replay"][0]["status"], "applied")
        self.assertEqual(result["replay"][0]["applied_version"], base + 1)
        fresh = self.service.get_item(self.item["id"])
        self.assertIn("Z-1", self._flushed_zones(fresh))

    def test_two_crews_different_zones_do_not_overwrite_each_other(self):
        base = self.item["version"]
        # 两个班组都基于同一旧版本，在不同管网区离线登记
        self.service.register_offline_flush(self.item["id"], {
            "batch_id": "B-A", "expected_version": base,
            "records": [{"record_id": "F-A", "zone_id": "Z-1"}],
        }, "field-A", "field_operator", replay=False)
        self.service.register_offline_flush(self.item["id"], {
            "batch_id": "B-B", "expected_version": base,
            "records": [{"record_id": "F-B", "zone_id": "Z-2"}],
        }, "field-B", "field_operator", replay=False)
        # 网络恢复后按原始登记顺序重放
        results = self.service.replay_pending(self.item["id"])
        self.assertEqual([r["batch_id"] for r in results], ["B-A", "B-B"])
        self.assertTrue(all(r["status"] == "applied" for r in results))
        fresh = self.service.get_item(self.item["id"])
        self.assertEqual(sorted(self._flushed_zones(fresh)), ["Z-1", "Z-2"])
        # 每个批次各自产生一个新版本
        self.assertEqual(fresh["version"], base + 2)

    def test_concurrent_offline_replay_and_online_flush_keep_both_zones(self):
        from src.domain import ConflictError
        base = self.item["version"]
        self.service.register_offline_flush(self.item["id"], {
            "batch_id": "B-C", "expected_version": base,
            "records": [{"record_id": "F-C", "zone_id": "Z-1"}],
        }, "field-A", "field_operator", replay=False)
        barrier = threading.Barrier(2)
        errors = []

        def offline():
            barrier.wait()
            try:
                self.service.replay_pending(self.item["id"])
            except Exception as exc:  # pragma: no cover - 并发不应失败
                errors.append(exc)

        def online():
            barrier.wait()
            for _ in range(10):
                fresh = self.repo.get_item(self.item["id"])
                try:
                    self.service.act(
                        self.item["id"], "flush", {"record_id": "F-ON", "zone_id": "Z-2"},
                        "field-B", "field_operator", fresh["version"],
                    )
                    return
                except ConflictError as exc:
                    if exc.code != "version_conflict":  # pragma: no cover
                        raise
            errors.append(RuntimeError("在线冲洗重试耗尽"))  # pragma: no cover

        t1 = threading.Thread(target=offline)
        t2 = threading.Thread(target=online)
        t1.start()
        t2.start()
        t1.join()
        t2.join()
        self.assertFalse(errors)
        fresh = self.service.get_item(self.item["id"])
        self.assertEqual(sorted(self._flushed_zones(fresh)), ["Z-1", "Z-2"])

    def test_failed_batch_stays_pending_and_written_part_is_kept(self):
        raw = self._prepare_item(["Z-9"], status="verified")
        # verified 状态不允许冲洗 -> 可重试失败
        result = self.service.register_offline_flush(raw["id"], {
            "batch_id": "B-WAIT", "expected_version": raw["version"],
            "records": [{"record_id": "F-WAIT", "zone_id": "Z-9"}],
        }, "field-1", "field_operator")
        batch = result["replay"][0]
        self.assertEqual(batch["status"], "pending")
        self.assertEqual(batch["attempts"], 1)
        self.assertIn("invalid_state", batch["last_error"])

        # 另一个区在此期间已经在线冲洗成功（已写好的部分）
        item = self.service.get_item(raw["id"])
        item = self.service.act(
            item["id"], "switch_source", {"alternate_source_id": "ALT-1"},
            "coord-1", "coordinator", item["version"],
        )
        item = self.service.act(
            item["id"], "flush", {"record_id": "F-ONLINE", "zone_id": "Z-OTHER"},
            "field-2", "field_operator", item["version"],
        )
        online_version = item["version"]

        results = self.service.replay_pending(raw["id"])
        self.assertEqual(results[0]["status"], "applied")
        fresh = self.service.get_item(raw["id"])
        self.assertEqual(sorted(self._flushed_zones(fresh)), ["Z-9", "Z-OTHER"])
        # 已写好的在线冲洗未被回滚或覆盖
        self.assertGreater(fresh["version"], online_version)

    def test_replay_preserves_registration_order_with_head_blocking(self):
        base = self.item["version"]
        for bid, zone in [("B1", "Z-1"), ("B2", "Z-2")]:
            self.service.register_offline_flush(self.item["id"], {
                "batch_id": bid, "expected_version": base,
                "records": [{"record_id": "F-" + bid, "zone_id": zone}],
            }, "field-" + bid, "field_operator", replay=False)
        # 人为把 B1 置为待重试失败条件：直接用较早的基版本并把事件保持可冲洗
        # 这里用正常状态，B1/B2 都会成功；顺序仍须严格为 B1,B2
        results = self.service.replay_pending(self.item["id"])
        self.assertEqual([r["batch_id"] for r in results], ["B1", "B2"])

        # 队头阻塞场景：新事件处于不可冲洗状态时，排在后面的批次不能抢先执行
        raw = self._prepare_item(["Z-A", "Z-B"], status="verified")
        for bid in ("H1", "H2", "H3"):
            self.service.register_offline_flush(raw["id"], {
                "batch_id": bid, "expected_version": raw["version"],
                "records": [{"record_id": "F-" + bid, "zone_id": "Z-A"}],
            }, "field-" + bid, "field_operator", replay=False)
        first_pass = self.service.replay_pending(raw["id"])
        self.assertEqual([r["batch_id"] for r in first_pass], ["H1"])
        self.assertEqual(first_pass[0]["status"], "pending")
        for bid, attempts in (("H2", 0), ("H3", 0)):
            self.assertEqual(self.repo.get_offline_batch(bid)["attempts"], attempts)

    def test_replay_is_idempotent_and_keeps_online_fields(self):
        base = self.item["version"]
        result = self.service.register_offline_flush(self.item["id"], {
            "batch_id": "B-IDEM", "expected_version": base,
            "records": [{"record_id": "F-IDEM", "zone_id": "Z-1"}],
        }, "field-1", "field_operator")
        self.assertEqual(result["replay"][0]["status"], "applied")
        # 再次重放不应重复写入
        self.assertEqual(self.service.replay_pending(self.item["id"]), [])
        fresh = self.service.get_item(self.item["id"])
        self.assertEqual(len(self._flushed_zones(fresh)), 1)
        # 旧基版本上没有的在线字段不能被覆盖丢失
        self.assertTrue(fresh["payload"]["notifications"][0]["notice_id"].startswith("N-OFF-"))
        self.assertEqual(fresh["payload"]["alternate_source_id"], "ALT-1")

    def test_rejected_lab_sample_is_not_retried(self):
        item = self._prepare_item(["Z-1"], status="disinfected")
        result = self.service.register_offline_sample(item["id"], {
            "record_id": "S-BAD", "sample_id": "LAB-BAD", "zone_id": "Z-1",
            "concentration": 12, "expected_version": item["version"],
        }, "lab-1", "lab")
        batch = result["replay"][0]
        self.assertEqual(batch["status"], "rejected")
        self.assertTrue(batch["last_error"].startswith("quality_not_met"))
        # 不会进入待重试队列
        self.assertEqual(self.repo.pending_offline_batches(item["id"]), [])
        fresh = self.service.get_item(item["id"])
        self.assertEqual(fresh["version"], item["version"])
        self.assertNotIn("LAB-BAD", [s["sample_id"] for s in fresh["payload"].get("sample_results", [])])

    def test_qualified_offline_sample_is_merged(self):
        item = self._prepare_item(["Z-1"], status="disinfected")
        result = self.service.register_offline_sample(item["id"], {
            "record_id": "S-OK", "sample_id": "LAB-OK", "zone_id": "Z-1",
            "concentration": 2, "expected_version": item["version"],
        }, "lab-1", "lab")
        self.assertEqual(result["replay"][0]["status"], "applied")
        fresh = self.service.get_item(item["id"])
        self.assertEqual(fresh["status"], "sampled")
        self.assertIn("LAB-OK", [s["sample_id"] for s in fresh["payload"]["sample_results"]])

    def test_audit_shows_original_base_and_merged_payload(self):
        from src.audit import audit_hash
        base = self.item["version"]
        self.service.register_offline_flush(self.item["id"], {
            "batch_id": "B-AUD", "expected_version": base,
            "records": [{"record_id": "F-AUD", "zone_id": "Z-1"}],
        }, "field-1", "field_operator")
        fresh = self.service.get_item(self.item["id"])
        registered = [e for e in fresh["audit"] if e["event_type"] == "offline_registered"]
        merged = [e for e in fresh["audit"] if e["event_type"] == "offline_flush_merged"]
        self.assertEqual(len(registered), 1)
        self.assertEqual(len(merged), 1)
        payload = merged[0]["payload"]
        # 可核对原始版本、现场原始记录和合并后的新版本
        self.assertEqual(payload["base_version"], base)
        self.assertEqual(payload["kind"], "flush")
        self.assertEqual(payload["original_payload"]["response_actions"][0]["record_id"], "F-AUD")
        self.assertTrue(any(
            a.get("record_id") == "F-AUD"
            for a in payload["merged_payload"]["response_actions"]
        ))
        self.assertIn("base_payload", payload)
        self.assertEqual(payload["merge_info"]["merged"][0]["zone_id"], "Z-1")
        # 哈希链完整
        previous = "GENESIS"
        for event in fresh["audit"]:
            body = {key: event[key] for key in ("item_id", "event_type", "actor", "role", "payload", "created_at")}
            self.assertEqual(audit_hash(previous, body), event["event_hash"])
            previous = event["event_hash"]

    def test_duplicate_batch_registration_is_idempotent(self):
        base = self.item["version"]
        payload = {
            "batch_id": "B-DUP", "expected_version": base,
            "records": [{"record_id": "F-DUP", "zone_id": "Z-1"}],
        }
        first = self.service.register_offline_flush(self.item["id"], payload, "field-1", "field_operator", replay=False)
        second = self.service.register_offline_flush(self.item["id"], payload, "field-1", "field_operator", replay=False)
        self.assertEqual(first["registered"]["batch_id"], second["registered"]["batch_id"])
        self.assertEqual(second["registered"]["seq"], first["registered"]["seq"])
        pending = self.repo.pending_offline_batches(self.item["id"])
        self.assertEqual(len(pending), 1)

    def test_offline_flush_requires_version_and_role(self):
        with self.assertRaises(DomainError) as context:
            self.service.register_offline_flush(self.item["id"], {
                "records": [{"zone_id": "Z-1"}],
            }, "lab-1", "lab")
        self.assertEqual(context.exception.code, "forbidden")
        with self.assertRaises(DomainError) as context:
            self.service.register_offline_flush(self.item["id"], {
                "records": [{"record_id": "F-X", "zone_id": "Z-1"}],
            }, "field-1", "field_operator")
        self.assertEqual(context.exception.code, "expected_version_required")


if __name__ == "__main__":
    unittest.main()
