# 空气污染源许可与合规检查

管理设施、排放口、治理设备、现场检查、整改和许可续期。

## 模块结构

- `app.py`：参数解析、依赖组装和HTTP服务启动。
- `src/domain.py`：数据结构、错误、状态和基础校验。
- `src/rules.py`：状态机、角色矩阵、优先级、期限、关闭不变量、快照重算决策。
- `src/repository.py`：SQLite建表、旧库迁移、快照/设备版本、事务、幂等键和审计链。
- `src/service.py`：权限检查、用例编排、并发控制和审计。
- `src/http_api.py`：JSON路由和统一错误响应。
- `src/audit.py`：UTC时间和SHA-256审计事件。
- `static/index.html`：最小演示页。
- `tests/`：完整流程、规则、失败、快照/重算/并发/补齐测试。

## 初始化与启动

```bash
python3 app.py --db ./data.db --port 8314
```

默认端口为`8314`，首次启动自动建库。使用`X-Actor`和`X-Role`请求头传递身份。
打开旧版数据库时自动迁移：历史许可缺少快照的，按初始版本v1补齐（来源`initial`，触发方`backfill`），并写入审计事件。

## 快照与续期重算规则

- 续期申请提交（`draft → submitted`）时冻结排放量、许可限值和治理设备版本，生成v1快照（来源`submit`）。
- 申请进入待批阶段（submitted/inspection/correction）后，检查记录或治理设备一旦变化即触发重算，产生下一版快照：
  - 超标记录（`measured > 冻结限值`）或存在未关闭事项：申请**退回复核**（回到`submitted`，快照effect=`returned`）。
  - 其余变化：只刷新快照（effect=`refreshed`），不改变审批结论。
- 已批许可（`approved`）的重算：设备版本变化或出现超标/未关闭事项时生成**修订版**（来源`revision`，`revision_of`指向上一版，`revision_no`递增）。
- 批准时额外校验：存在超标记录一律阻断（即使事项已关闭）。
- 检查员提交必须携带`expected_snapshot_version`，只接受当前快照版本；版本过期返回409，失败方凭`idempotency_key`重试，重复提交不产生重复检查记录或重复修订。

## 主要接口

- `GET /health`
- `GET /api/items`（列表含最新快照、`recompute_reasons`、`revision_no`、`returned_for_review`）
- `POST /api/items`
- `GET /api/items/{id}`
- `POST /api/items/{id}/transition`，必须提交`expected_version`
- `POST /api/items/{id}/inspections`，检查员提交检查记录：
  `kind/detail/status/measured/expected_snapshot_version/idempotency_key`
- `POST /api/items/{id}/records/{recordId}/close`，关闭整改并触发重算
- `POST /api/items/{id}/records`，普通备注记录（不强制快照版本）
- `POST /api/items/{id}/equipment`，申请人登记/升级治理设备（带`external_ref`再次提交即为升级）
- `GET /api/items/{id}/equipment`
- `GET /api/items/{id}/snapshots`，快照链（冻结值、重算原因、修订来源）
- `GET /api/items/{id}/records`
- `GET /api/audit`（`recompute`/`revision`事件含trigger、reasons、revision_no、revision_of）

允许角色：applicant, inspector, compliance_manager, viewer。申报量超过许可量或检查发现高严重度问题时提高优先级；存在未关闭整改或超标记录时不能批准。

## 测试

```bash
python3 -m unittest discover -s tests -v
```
