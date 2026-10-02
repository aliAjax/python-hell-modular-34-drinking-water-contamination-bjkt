import uuid

from . import domain, rules
from .domain import DomainError

# 重放时由流程状态导致的失败保留为待重试；由数据本身导致的失败直接拒绝
RETRYABLE_ERROR_CODES = {"invalid_state"}


def _is_retryable(exc):
    return getattr(exc, "code", None) in RETRYABLE_ERROR_CODES


class Service:
    def __init__(self, repository):
        self.repository = repository

    def create_item(self, payload, actor, role, region=None):
        if not actor or not role:
            raise DomainError("identity_required", "需要用户身份和角色", 401)
        if role not in rules.CREATE_ROLES:
            raise DomainError("forbidden", "当前角色不能创建此类业务记录", 403)
        normalized = domain.normalize_create(payload)
        stable_key = normalized.pop("_stable_key")
        return self.repository.create_item(
            rules.ENTITY_TYPE, stable_key, rules.INITIAL_STATUS, normalized, actor, role
        )

    def add_source(self, item_id, payload, actor, role, region=None):
        if not actor or not role:
            raise DomainError("identity_required", "需要用户身份和角色", 401)
        if role not in rules.SOURCE_ROLES:
            raise DomainError("forbidden", "当前角色不能提交来源记录", 403)
        item = self.repository.get_item(item_id)
        normalized = domain.normalize_source(payload)
        if region and rules.ENFORCE_REGION and role != "regulator" and normalized.get("region") and normalized["region"] != region:
            raise DomainError("region_mismatch", "来源记录不属于当前管辖区域", 403)
        result = self.repository.add_source(
            item_id,
            normalized.pop("source_type"),
            normalized.pop("external_id"),
            normalized,
            normalized.pop("observed_at"),
            actor,
            role,
        )
        return result

    def act(self, item_id, action, payload, actor, role, expected_version=None, region=None):
        if not actor or not role:
            raise DomainError("identity_required", "需要用户身份和角色", 401)
        item = self.repository.get_item(item_id)
        allowed = rules.ACTION_ROLES.get(action, set())
        if role not in allowed:
            raise DomainError("forbidden", "当前角色不能执行该操作", 403)
        if rules.ENFORCE_REGION and action in rules.REGION_SENSITIVE_ACTIONS and region and role != "regulator":
            if item["payload"].get("region") != region:
                raise DomainError("region_mismatch", "不能处理其他区域的记录", 403)
        if action in rules.ACTION_REQUIRES_VERSION and expected_version is None:
            raise DomainError("expected_version_required", "该操作需要 expected_version", 400)
        new_status, new_payload, event_payload = rules.apply_action(item, action, payload, actor, role)
        self.repository.apply_action(
            item_id, action, actor, role, new_status, new_payload, event_payload, expected_version
        )
        return self.get_item(item_id)

    # ------------------------------------------------------------------
    # 离线补录
    # ------------------------------------------------------------------

    def _require_offline_role(self, kind, role):
        if role not in rules.OFFLINE_ROLES.get(kind, set()):
            raise DomainError("forbidden", "当前角色不能登记该类离线补录", 403)

    def _require_base_version(self, payload):
        value = payload.get("expected_version")
        if value is None:
            raise DomainError("expected_version_required", "离线补录必须携带原始 expected_version", 400)
        try:
            version = int(value)
        except (TypeError, ValueError):
            raise DomainError("invalid_version", "expected_version 必须是整数")
        if version < 1:
            raise DomainError("invalid_version", "expected_version 必须为正整数")
        return version

    def _batch_id(self, payload):
        batch_id = payload.get("batch_id")
        if batch_id is None:
            return "batch-%s" % uuid.uuid4().hex
        if not isinstance(batch_id, str) or not batch_id.strip():
            raise DomainError("invalid_batch_id", "batch_id 必须是非空字符串")
        return batch_id.strip()

    def register_offline_flush(self, item_id, payload, actor, role, region=None, replay=True):
        """登记现场巡查组带回的离线冲洗记录。

        记录以导出时的 expected_version 作为原始版本入库排队；网络恢复后
        按登记顺序重放，三路合并进当前冲洗任务。
        """
        if not actor or not role:
            raise DomainError("identity_required", "需要用户身份和角色", 401)
        self._require_offline_role("flush", role)
        base_version = self._require_base_version(payload)
        records = payload.get("records")
        if not isinstance(records, list) or not records:
            raise DomainError("records_required", "至少需要一条离线冲洗记录")
        flush_records = [rules.build_flush_record(record) for record in records]
        item = self.repository.get_item(item_id)
        if region and rules.ENFORCE_REGION and role != "regulator":
            if item["payload"].get("region") != region:
                raise DomainError("region_mismatch", "不能处理其他区域的记录", 403)
        original_payload = {"response_actions": flush_records}
        batch_id = self._batch_id(payload)
        batch = self.repository.enqueue_offline_batch(
            batch_id, item_id, "flush", base_version, actor, role, original_payload
        )
        results = [batch]
        if replay:
            results = self.replay_pending(item_id)
        return {"registered": batch, "replay": results}

    def register_offline_sample(self, item_id, payload, actor, role, region=None, replay=True):
        """登记离线化验复检记录；复检不合格时拒绝写入。"""
        if not actor or not role:
            raise DomainError("identity_required", "需要用户身份和角色", 401)
        self._require_offline_role("sample", role)
        base_version = self._require_base_version(payload)
        item = self.repository.get_item(item_id)
        if region and rules.ENFORCE_REGION and role != "regulator":
            if item["payload"].get("region") != region:
                raise DomainError("region_mismatch", "不能处理其他区域的记录", 403)
        record = rules.build_sample_record(payload)
        batch_id = self._batch_id(payload)
        batch = self.repository.enqueue_offline_batch(
            batch_id, item_id, "sample", base_version, actor, role, record
        )
        results = [batch]
        if replay:
            results = self.replay_pending(item_id)
        return {"registered": batch, "replay": results}

    def replay_pending(self, item_id=None):
        """按原始登记顺序重放待重试的离线补录。

        每个批次在独立事务内处理：已成功写入的批次先提交，不会因后续
        批次失败而回滚；遇到仍需重试的批次时停止，排在后面的批次保持
        pending，下次按原顺序继续。被拒绝的批次为终态，不阻塞后续。
        """
        if item_id is not None:
            pending = self.repository.pending_offline_batches(item_id)
            return self._replay_in_order(pending)
        results = []
        pending = self.repository.list_offline_batches(status="pending")
        grouped = {}
        for batch in pending:
            grouped.setdefault(batch["item_id"], []).append(batch)
        for item_pending in grouped.values():
            results.extend(self._replay_in_order(item_pending))
        return results

    def _replay_in_order(self, pending):
        results = []
        for batch in pending:
            result = self.replay_batch(batch["batch_id"])
            results.append(result)
            if result["status"] == "pending":
                break
        return results

    def replay_batch(self, batch_id):
        batch = self.repository.get_offline_batch(batch_id)
        kind = batch["kind"]
        if kind == "flush":
            plan = self._flush_plan
        elif kind == "sample":
            plan = self._sample_plan
        else:
            plan = self._unsupported_plan
        return self.repository.replay_offline_batch(batch_id, plan, _is_retryable)

    def _unsupported_plan(self, item_row, base_snapshot, current_payload, batch):
        raise DomainError("unsupported_offline_kind", "不支持的离线补录类型: %s" % batch["kind"])

    def _flush_plan(self, item_row, base_snapshot, current_payload, batch):
        rules.require_status_for("flush", item_row["status"])
        original_payload = batch["original_payload"]
        merged_payload, merge_info = rules.merge_flush_records(
            base_snapshot["payload"], current_payload, original_payload
        )
        event_payload = {
            "batch_id": batch["batch_id"],
            "seq": batch["seq"],
            "kind": "flush",
            "base_version": batch["base_version"],
            "base_payload": base_snapshot["payload"],
            "original_payload": original_payload,
            "merged_payload": merged_payload,
            "merge_info": merge_info,
        }
        result_summary = {"merge_info": merge_info}
        return "flushing", merged_payload, event_payload, result_summary

    def _sample_plan(self, item_row, base_snapshot, current_payload, batch):
        rules.require_status_for("sample", item_row["status"])
        record = dict(batch["original_payload"])
        # 复检不合格照旧拒绝写入（不进入待重试，避免不合格记录反复重放）
        limit = float(current_payload.get("limit", 0) or 0)
        if limit and float(record["concentration"]) > limit:
            raise DomainError(
                "quality_not_met",
                "离线复检结果 %s 超过限值 %s，拒绝写入" % (record["concentration"], limit),
                409,
            )
        if rules.offline_sample_duplicated(current_payload, record):
            merged_payload = current_payload
            merge_info = {"merged": [], "skipped": [{"sample_id": record["sample_id"], "reason": "already_applied"}]}
        else:
            merged_payload = rules.merge_sample_record(current_payload, record)
            merge_info = {"merged": [{"sample_id": record["sample_id"], "record_id": record["record_id"]}], "skipped": []}
        event_payload = {
            "batch_id": batch["batch_id"],
            "seq": batch["seq"],
            "kind": "sample",
            "base_version": batch["base_version"],
            "base_payload": base_snapshot["payload"],
            "original_payload": record,
            "merged_payload": merged_payload,
            "merge_info": merge_info,
        }
        result_summary = {"merge_info": merge_info}
        return "sampled", merged_payload, event_payload, result_summary

    def list_offline_batches(self, item_id=None, status=None):
        return self.repository.list_offline_batches(item_id, status)

    def get_item(self, item_id):
        item = self.repository.get_item(item_id)
        item["sources"] = self.repository.list_sources(item_id)
        item["audit"] = self.repository.audit_trail(item_id)
        item["assessment"] = rules.assess(item["payload"])
        return item

    def list_items(self, status=None):
        return self.repository.list_items(status)

    def state(self):
        return self.repository.state_summary()
