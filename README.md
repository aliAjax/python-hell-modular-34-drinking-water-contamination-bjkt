# 市政饮用水污染响应

模块化纯 Python 3.9.6+ 标准库项目，默认端口 `8334`。

- `app.py`：服务生命周期和依赖组装。
- `src/domain.py`：水源、污染物、区域和来源校验。
- `src/rules.py`：污染评分、通知去重、停水、切换水源、冲洗、消毒、复检和恢复状态机，以及离线补录的按区三路合并。
- `src/repository.py`：SQLite、事务、重复保护、乐观版本、版本快照、离线补录队列和审计链。
- `src/service.py`：身份、角色、用例编排和离线补录重放。
- `src/http_api.py`：JSON 接口与静态首页。
- `src/audit.py`：审计哈希。

```bash
python3 app.py --init --db ./data.db
python3 app.py --db ./data.db --port 8334
python3 -m unittest discover -s tests -v
```

接口包括 `GET /health`、`GET /api/state`、`POST /api/items`、`POST /api/items/<id>/sources`、`POST /api/items/<id>/actions`、离线补录与重放接口和审计查询。测试覆盖完整响应流程、重复事件、重复通知、复检阈值、权限、版本冲突和离线补录合并。

## 离线补录（巡查组断网场景）

巡查组离线两小时后回所，手上仍是导出时的 `expected_version`。离线补录不再整份覆盖，而是以该原始版本为基准做三路合并：

- `POST /api/items/<id>/offline/flush`：登记现场离线冲洗记录（`expected_version` + `records[]`，每条带稳定 `record_id` 和 `zone_id`），随后立即尝试重放。
- `POST /api/items/<id>/offline/sample`：登记离线化验复检记录（`expected_version`、`sample_id`、`zone_id`、`concentration`）；复检结果超过限值照旧拒绝写入（批次标记 `rejected`，不会重试）。
- `POST /api/items/<id>/replay` / `POST /api/offline/replay`：网络恢复后按原始登记顺序（`seq`）重放待重试批次。
- `GET /api/items/<id>/offline/batches`、`GET /api/offline/batches?status=pending`：查看补录批次。

合并语义：

1. **按管网区三路合并**：以 `version_snapshots` 中原始版本为 base，只提取离线新增的冲洗记录追加进当前任务；其他班组对其他管网区的记录原样保留，互不覆盖。
2. **失败留待重试**：流程状态不允许（如还没进入冲洗阶段）时批次保持 `pending` 并记录 `attempts/last_error`；队头批次未通过时后续批次不抢先，恢复后严格按登记顺序重放。
3. **已写部分不丢**：每个批次独立事务提交，成功一个版本号加一；重放按 `record_id` 幂等去重，网络抖动重复触发不会产生重复记录。
4. **审计可核对**：`offline_registered` 记录登记时的原始载荷，`offline_flush_merged`/`offline_sample_merged` 同时保存 `base_version`、`base_payload`（原始版本）、`original_payload`（现场带回记录）和 `merged_payload`（合并后新版本），`offline_rejected` 记录拒绝原因（含不合格化验）。

内置规则不替代真实水质模型、法定通报渠道或供水控制系统的联锁。
