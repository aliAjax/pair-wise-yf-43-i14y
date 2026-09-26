# 实验室仪器校准与方法验证

这是一个只使用Python标准库和SQLite的模块化项目，默认端口为`8309`。所有业务规则集中在`src/rules.py`，`app.py`只负责组装依赖和启动服务。

## 模块结构

- `app.py`：命令行参数、依赖组装、启动和信号处理。
- `src/domain.py`：角色、数据结构、领域异常和基础校验。
- `src/rules.py`：状态机、权限、领域计算、冲突和跨对象校验。
- `src/repository.py`：SQLite建表、查询、事务和乐观锁。
- `src/service.py`：用例编排、幂等处理、版本控制和审计写入。
- `src/http_api.py`：HTTP路由、请求解析和统一错误响应。
- `src/audit.py`：实体操作审计时间线。
- `static/index.html`：最小演示页面。
- `tests/`：完整流程、规则和失败场景测试。

## 初始化与启动

```bash
python3 app.py --db ./data.db --port 8309
```

服务启动时会自动建表。`--host`可修改监听地址，`--db`可指定其他SQLite文件。

## 核心对象

- `instrument`：仪器状态（`active` / `calibrating` / `quarantined` / `stopped`）。
- `calibration`：校准记录。
- `method`：方法版本。
- `result`：检测结果（`pending` / `released` / `blocked` / `review` / `withdrawn`）。
- `standard`：核查用标准器，带有效期 `due_at`。
- `check`：期间核查记录。

## 期间核查与结果追溯

年检合格不保证两次校准之间一直可靠，因此在两次校准之间用标准器做期间核查：

1. `POST /api/standard` 登记标准器（`name`、`serial`、`due_at`）。
2. `POST /api/check` 为仪器创建核查单（`instrument_id`、`standard_id`）。
3. 对核查单执行 `perform`，必填 `standard_value`（标准值）、`measured_value`
   （实测值）、`tolerance`（允许偏差）、`checked_at`。系统登记偏差
   `|实测-标准|` 及标准器有效期。
4. **标准器过期（`due_at < checked_at`）或偏差超限（`deviation > tolerance`）
   时核查失败**，同一事务内级联处理：
   - 仪器置 `stopped` 停用，`stop_reason` 记录具体原因；停用期间任何放行
     （含复核后重新发布）都被拒绝，错误信息给出阻塞原因。
   - 所有未放行（`pending`）结果置 `blocked`，`block_reason` 指向失败核查。
   - 自上次成功核查（或无基线时的全部历史）以来已放行的结果置 `review`
     待复核；更早的放行结果不受影响。
5. 对 `review` 结果：
   - 确认无影响：`republish`（需 `impact_assessment` 与 `no_impact=true`，
     授权人角色），结果重新发布为 `released`，并以重新发布时间作为新的
     追溯锚点；
   - 确认有问题：`withdraw`（需 `reason`）撤回，终态 `withdrawn`。
6. 仪器经 `send_calibration` + 新校准合格的 `calibrate` 后恢复 `active`；
   被阻塞结果可 `reanalyze` 后重新放行。

原始放行记录、阻塞/复核流转、核查依据（标准器、标准值、实测值、允许偏差、
偏差、失败原因）都通过 `GET /api/audit` 保留在时间线中，不会被覆盖。

## 主要接口

- `GET /health`：健康检查。
- `GET /api/<kind>`：按对象类型查询，可用`?status=`过滤。
- `POST /api/<kind>`：创建对象；请求体为JSON。
- `GET /api/entities/<id>`：读取对象当前版本。
- `POST /api/entities/<id>/actions`：提交`{"action":"动作名","data":{...},"expected_version":数字}`。
- `GET /api/audit`：读取审计记录。

请求身份通过`X-User-Id`和`X-Role`请求头传入。创建和动作的可执行角色由规则引擎控制。

## 测试

```bash
python3 -m unittest discover -s tests -v
```

## 局限

校准周期、误差和放行规则是可演示的业务模型，不替代实验室质量体系或计量认证。
