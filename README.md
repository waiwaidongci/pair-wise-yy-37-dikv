# 空气污染源许可与合规检查

管理设施、排放口、治理设备、现场检查、整改和许可续期。

## 模块结构

- `app.py`：参数解析、依赖组装和HTTP服务启动。
- `src/domain.py`：数据结构、错误、状态和基础校验。
- `src/rules.py`：状态机、角色矩阵、优先级、期限和关闭不变量。
- `src/repository.py`：SQLite建表、事务、版本控制和审计链。
- `src/service.py`：权限检查、用例编排、并发控制和审计。
- `src/http_api.py`：JSON路由和统一错误响应。
- `src/audit.py`：UTC时间和SHA-256审计事件。
- `static/index.html`：最小演示页。
- `tests/`：完整流程、规则和失败测试。

## 初始化与启动

```bash
python3 app.py --db ./data.db --port 8314
```

默认端口为`8314`，首次启动自动建库。使用`X-Actor`和`X-Role`请求头传递身份。

## 主要接口

- `GET /health`
- `GET /api/items`
- `POST /api/items`
- `GET /api/items/{id}`
- `POST /api/items/{id}/records`，可传 `expected_snapshot_version` 与 `idempotency_key` 做乐观并发与幂等重试
- `POST /api/items/{id}/records/{record_id}/close`
- `GET /api/items/{id}/equipment`
- `POST /api/items/{id}/equipment`
- `PUT /api/items/{id}/equipment/{equipment_id}`
- `GET /api/items/{id}/snapshots`
- `GET /api/items/{id}/renewals`
- `POST /api/items/{id}/renewals`，提交时冻结排放量与设备版本，需 `idempotency_key`
- `POST /api/items/{id}/renewals/{renewal_id}/approve`
- `POST /api/items/{id}/renewals/{renewal_id}/resubmit`
- `POST /api/items/{id}/transition`，必须提交`expected_version`
- `GET /api/audit`

允许角色：applicant, inspector, compliance_manager, viewer。申报量超过许可量或检查发现高严重度问题时提高优先级；存在未关闭整改时不能批准。

## 续期、快照与修订

- 提交续期申请时冻结当前排放量与设备版本，生成许可快照（`snapshot_reason=renewal_submitted`）。
- 治理设备变更或检查记录新增/关闭触发重算，生成新快照并记录重算原因（`reason`）与修订来源（`source`）。
- 重算发现排放量超过许可限值或存在未关闭事项时，待批续期申请退回复核（`renewal_returned`）。
- 已批许可（工作流状态 approved 或续期已批准）改动时生成修订版（`revision` 审计事件）。
- 检查员并发提交只接受当前快照版本（`expected_snapshot_version`），冲突返回 409；失败方凭幂等键重试，不重复修订。
- 历史数据缺快照时启动按初始版本补齐（`reason=initial`, `source=backfill`）。
- 列表与审计均展示重算原因与修订来源。

## 测试

```bash
python3 -m unittest discover -s tests -v
```
