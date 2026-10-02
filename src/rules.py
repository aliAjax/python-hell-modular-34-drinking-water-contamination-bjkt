import uuid

from .domain import DomainError

ENTITY_TYPE = "water_contamination"
INITIAL_STATUS = "detected"
CREATE_ROLES = {"analyst", "dispatcher"}
SOURCE_ROLES = {"analyst", "dispatcher", "field_operator", "lab"}
ACTION_ROLES = {
    "verify": {"analyst", "dispatcher"},
    "advise": {"coordinator", "dispatcher"},
    "switch_source": {"coordinator"},
    "flush": {"field_operator"},
    "disinfect": {"field_operator"},
    "sample": {"lab", "field_operator"},
    "restore": {"coordinator", "regulator"},
    "cancel": {"coordinator"},
}
# 离线补录沿用对应在线操作的权限
OFFLINE_ROLES = {
    "flush": ACTION_ROLES["flush"],
    "sample": ACTION_ROLES["sample"],
    "source": SOURCE_ROLES,
}
ENFORCE_REGION = False
REGION_SENSITIVE_ACTIONS = set()
ACTION_REQUIRES_VERSION = {"advise", "switch_source", "flush", "disinfect", "sample", "restore", "cancel"}

ALLOWED_STATUS = {
    "verify": {"detected", "verified"},
    "advise": {"verified", "advisory"},
    "switch_source": {"verified", "advisory", "flushing", "disinfected", "sampled", "switched"},
    "flush": {"advisory", "flushing", "switched"},
    "disinfect": {"flushing", "disinfected"},
    "sample": {"disinfected", "sampled"},
    "restore": {"sampled"},
    "cancel": {"detected", "verified"},
}


def require_status_for(action, status):
    allowed = ALLOWED_STATUS.get(action)
    if allowed is not None and status not in allowed:
        raise DomainError("invalid_state", "当前状态 %s 不允许执行该操作" % status)


def assess(payload):
    concentration = float(payload.get("concentration", 0))
    limit = max(float(payload.get("limit", 0.000001)), 0.000001)
    ratio = concentration / limit
    population = int(payload.get("population", 0))
    score = min(100.0, ratio * 35.0 + min(population / 1000.0, 40.0))
    if score >= 80:
        level = "critical"
    elif score >= 50:
        level = "high"
    elif score >= 20:
        level = "medium"
    else:
        level = "low"
    return {"score": round(score, 2), "level": level, "ratio": round(ratio, 3)}


def _need_status(item, allowed):
    if item["status"] not in allowed:
        raise DomainError("invalid_state", "当前状态 %s 不允许执行该操作" % item["status"])


def _text(payload, name):
    value = payload.get(name)
    if not isinstance(value, str) or not value.strip():
        raise DomainError("field_required", "%s 不能为空" % name)
    return value.strip()


def apply_action(item, action, payload, actor, role):
    status = item["status"]
    current = dict(item["payload"])

    if action == "verify":
        _need_status(item, {"detected", "verified"})
        sample_count = int(payload.get("sample_count", 0) or 0)
        if sample_count < 1:
            raise DomainError("sample_required", "需要至少一份复检样本", 409)
        current["assessment"] = assess(current)
        current["verification"] = {"sample_count": sample_count, "note": payload.get("note", "")}
        return "verified", current, {"assessment": current["assessment"], "verification": current["verification"]}

    if action == "advise":
        _need_status(item, {"verified", "advisory"})
        notice_id = _text(payload, "notice_id")
        notice = {
            "notice_id": notice_id,
            "kind": _text(payload, "kind"),
            "message": _text(payload, "message"),
        }
        notices = current.setdefault("notifications", [])
        if any(existing.get("notice_id") == notice_id for existing in notices):
            raise DomainError("duplicate_notification", "同一通知编号不能重复发送", 409)
        notices.append(notice)
        return "advisory", current, {"notice": notice}

    if action == "switch_source":
        _need_status(item, {"verified", "advisory", "flushing", "disinfected", "sampled", "switched"})
        alternate = _text(payload, "alternate_source_id")
        current["alternate_source_id"] = alternate
        return "switched", current, {"alternate_source_id": alternate}

    if action == "flush":
        _need_status(item, {"advisory", "flushing", "switched"})
        flush_record = build_flush_record(payload)
        current.setdefault("response_actions", []).append(flush_record)
        return "flushing", current, {"zone_id": flush_record["zone_id"], "type": "flush", "record_id": flush_record["record_id"]}

    if action == "disinfect":
        _need_status(item, {"flushing", "disinfected"})
        if not payload.get("completed"):
            raise DomainError("disinfection_incomplete", "消毒尚未完成", 409)
        zone_id = _text(payload, "zone_id")
        current.setdefault("response_actions", []).append({"type": "disinfect", "zone_id": zone_id})
        return "disinfected", current, {"zone_id": zone_id, "type": "disinfect"}

    if action == "sample":
        _need_status(item, {"disinfected", "sampled"})
        result = build_sample_record(payload)
        current.setdefault("sample_results", []).append(result)
        return "sampled", current, {"sample_result": result}

    if action == "restore":
        _need_status(item, {"sampled"})
        if not payload.get("all_zones_cleared"):
            raise DomainError("zones_not_cleared", "仍有区域未完成水质恢复", 409)
        limit = float(current.get("limit", 0))
        results = current.get("sample_results", [])
        if not results or any(float(result["concentration"]) > limit for result in results):
            raise DomainError("quality_not_met", "复检结果未全部达到限值", 409)
        current["restoration"] = {"actor": actor, "note": payload.get("note", "")}
        return "restored", current, {"restoration": current["restoration"]}

    if action == "cancel":
        _need_status(item, {"detected", "verified"})
        reason = _text(payload, "reason")
        current["cancellation"] = {"reason": reason, "actor": actor}
        return "cancelled", current, {"reason": reason}

    raise DomainError("unknown_action", "不支持的操作")


def _record_id(payload, prefix):
    value = payload.get("record_id")
    if value is None:
        return "%s-%s" % (prefix, uuid.uuid4().hex[:12])
    if not isinstance(value, str) or not value.strip():
        raise DomainError("invalid_record_id", "record_id 必须是非空字符串")
    return value.strip()


def build_flush_record(payload):
    """构造带稳定编号的冲洗记录；编号用于离线重放时去重。"""
    return {
        "type": "flush",
        "record_id": _record_id(payload, "flush"),
        "zone_id": _text(payload, "zone_id"),
        "recorded_at": _text(payload, "recorded_at") if "recorded_at" in payload else None,
        "note": payload.get("note", ""),
    }


def build_sample_record(payload):
    """构造带稳定编号的化验复检记录，编号用于离线重放时去重。"""
    result = {
        "record_id": _record_id(payload, "sample"),
        "sample_id": _text(payload, "sample_id"),
        "zone_id": _text(payload, "zone_id"),
        "concentration": float(payload.get("concentration", 0)),
        "recorded_at": _text(payload, "recorded_at") if "recorded_at" in payload else None,
    }
    if result["concentration"] < 0:
        raise DomainError("invalid_concentration", "浓度不能为负数")
    return result


def _flush_index(payload):
    index = {}
    for record in payload.get("response_actions", []):
        if record.get("type") == "flush":
            index.setdefault(record.get("zone_id"), []).append(record)
    return index


def merge_flush_records(base_payload, current_payload, offline_payload):
    """三路合并离线冲洗补录。

    以班组离线时所依据的原始版本 base_payload 为基准，只提取离线期间
    现场新增的管网冲洗记录；服务端当前版本中其他班组对其他管网区的
    冲洗记录原样保留，互不覆盖。
    """
    base_by_zone = _flush_index(base_payload)
    current_by_zone = _flush_index(current_payload)

    merged = dict(current_payload)
    actions = [dict(record) for record in current_payload.get("response_actions", [])]
    merged["response_actions"] = actions

    merged_zones = []
    skipped = []
    for record in offline_payload.get("response_actions", []):
        if record.get("type") != "flush":
            continue
        zone_id = record.get("zone_id")
        record_id = record.get("record_id")
        existing_ids = {item.get("record_id") for item in current_by_zone.get(zone_id, [])}
        if record_id in existing_ids:
            # 重放的同一批记录已写入过，不能重复写
            skipped.append({"zone_id": zone_id, "record_id": record_id, "reason": "already_applied"})
            continue
        base_records = base_by_zone.get(zone_id, [])
        if record_id in {item.get("record_id") for item in base_records}:
            # 该记录在原始版本里已存在，不属于离线新增
            skipped.append({"zone_id": zone_id, "record_id": record_id, "reason": "present_in_base"})
            continue
        actions.append(dict(record))
        current_by_zone.setdefault(zone_id, []).append(record)
        merged_zones.append({"zone_id": zone_id, "record_id": record_id})

    status = "flushing" if merged_zones else None
    return merged, {"merged": merged_zones, "skipped": skipped}


def offline_sample_duplicated(current_payload, record):
    return any(
        existing.get("record_id") == record.get("record_id") or existing.get("sample_id") == record.get("sample_id")
        for existing in current_payload.get("sample_results", [])
    )


def merge_sample_record(current_payload, record):
    merged = dict(current_payload)
    merged["sample_results"] = list(current_payload.get("sample_results", [])) + [dict(record)]
    return merged
