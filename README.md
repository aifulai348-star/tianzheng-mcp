# tianzheng-mcp (v1.0.1)

面向 **AutoCAD 2024**（`AutoCAD.Application.24.3`）与 **天正建筑 T20 V9** 的本地队列式 **MCP Server & CLI**。
独立项目，不修改原 AutoCAD MCP（切勿让两个服务同时编辑同一个 AutoCAD 实例）。

> **v1.0.1 核心里程碑**：固定 **9 个 MCP 顶层工具**，开放 **71 项图纸操作能力**，其中 **70 项已通过 AutoCAD 2024 + T20 V9 真机实测（`verified_live`）**、**0 项 `pending_live`**（仅保留 1 项历史直跑梯 `stair.create` 为 `unverified`），离线回归单元测试全绿。

---

## 核心架构与安全保障

1. **SQLite 持久化队列 + 单 STA COM Worker**
   提交（`queue_submit`）与执行（`worker`）分离；严格使用单个单线程套间（STA）进程持有 OS 排他锁驱动 COM，避免多线程重入冲突。
2. **幂等与上下文引用（`operation_id` + `$ref`）**
   同一 `operation_id` + 相同配方幂等返回原任务；相同 `operation_id` 篡改内容直接拒绝。前序步骤通过 `bind` 产出 `{document_id, handles}` 后，后续步骤可用 `{"$ref": "..."}` 安全引用（以 Handle 为跨重开稳定标识）。
3. **100 ms 独立 Watchdog + 定向 PID ESC**
   看门狗线程不接触 COM，以 100 ms 周期检测步骤超时、CAD 主窗口禁用状态与可见模态对话框，仅向绑定 `TZ_CAD_PID` 窗口投递受控 `ESC`，不向系统前台乱发按键。
4. **不确定即停（`unknown_outcome` + 必填 `side_effects`）**
   一旦触发超时、模态弹窗、进程中断或写后核验不符，任务立即落 `unknown_outcome` 并冻结队列——**绝不盲目重放**（防止重复绘墙/立柱）。必须人工检查图纸并调用 `queue_acknowledge` 填写 `--side-effects` 观察记录后才可恢复执行。
5. **只读跨任务发现与回读（`document.list` / `document.readback`）**
   支持仅凭 DWG 路径只读打开允许输出目录内的已保存图纸，按 Handle 数值游标分页发现对象并回读属性与 DXF 数据，校验证件全链路 `Documents.Open(..., True)` -> 校验 -> `Close(False)` 闭环且文件 SHA-256 不变。

---

## 9 个 MCP 顶层工具

| 工具 | 入参 | 说明 |
|---|---|---|
| `system_diagnose` | — | 检测 CAD/天正安装环境与运行会话（连接失败返回 `connection_error`，不抛未捕获异常） |
| `list_capabilities` | — | 列出全部 71 项能力的 `{required, optional, validation}` 契约 |
| `queue_submit` | `recipe` | 校验并入队执行配方（参数错误与 `$ref` 越界在提交期直接拒绝） |
| `queue_status` | `job_id` | 查询任务状态（`queued`/`running`/`succeeded`/`failed`/`cancelled`/`unknown_outcome`）、逐步骤结果与上下文 |
| `queue_cancel` | `job_id` | 请求取消任务（运行中任务将在下一个安全边界停止） |
| `queue_acknowledge` | `job_id`, `side_effects` | 对 `unknown_outcome` 任务记录人工实见副作用并解除队列冻结（不重放） |
| `worker_start` | — | 后台拉起独占 COM Worker 进程 |
| `worker_stop` | — | 请求 Worker 在下一个步骤/任务边界安全退出 |
| `worker_status` | — | 查看 Worker PID、运行态与队列状态目录 |

---

## 71 项图纸操作能力总览（70 项真机 `verified_live`）

完整参数定义、必填/可选字段与错误契约请查阅 [docs/MCP-TOOL-SPEC.md](docs/MCP-TOOL-SPEC.md) 及 `list_capabilities` 实时返回值（以工具实时返回的 `validation` 为准）。

| 功能域 | 数量 | 状态 | 包含操作 |
|---|---|---|---|
| **图档与只读回读** | 6 | 6 / 6 `verified_live` | `document.new`, `document.save`, `document.reopen`, `document.close`, `document.readback`, `document.list` |
| **天正墙体** | 5 | 5 / 5 `verified_live` | `wall.create`, `wall.edit`, `wall.split`, `wall.find`, `wall.fix_corner` |
| **天正门窗** | 6 | 6 / 6 `verified_live` | `opening.create`（普通门/普通窗）, `opening.move`, `opening.edit`, `opening.relabel`, `opening.mirror`, `opening.group_create` |
| **天正柱/板/屋顶/空间/楼梯** | 12 | 11 / 12 `verified_live` | `column.create`, `column.edit`, `column.move`, `column.rotate`, `slab.create`, `slab.edit`, `roof.create`, `roof.edit`, `space.create`, `space.edit`, `stair.rect_create`, `stair.create`（⚠️ 历史 `unverified`） |
| **轴网、标注与文字** | 14 | 14 / 14 `verified_live` | `axis.rect_create`, `axis.from_walls`, `axis.single_create`, `axis.annotate`, `dim.wall`, `dim.t20_angular`, `dim.aligned`, `dim.linear`, `dim.radial`, `dim.diameter`, `dim.angular`, `annotation.text`, `annotation.text_edit`, `annotation.mtext` |
| **基础几何与填充** | 8 | 8 / 8 `verified_live` | `line.create`, `circle.create`, `arc.create`, `ellipse.create`, `polyline.create`, `rectangle.create`, `polyline.edit`, `hatch.create` |
| **通用实体变换、样式与查询** | 11 | 11 / 11 `verified_live` | `entity.move`, `entity.copy`, `entity.rotate`, `entity.scale`, `entity.mirror`, `entity.set_layer`, `entity.set_color`, `entity.delete`, `entity.get`, `entity.verify`, `entity.query` |
| **图层管理** | 5 | 5 / 5 `verified_live` | `layer.create`, `layer.list`, `layer.edit`, `layer.set_active`, `layer.delete` |
| **图块管理** | 4 | 4 / 4 `verified_live` | `block.define`, `block.insert`, `block.list`, `block.delete` |

---

## 快速上手

### 1. 前置条件与环境变量

- **环境要求**：Windows、Python >= 3.12、`pywin32>=306`、`mcp>=1.20,<2`、已启动加载天正建筑 T20 V9 的 AutoCAD 2024。
- **注意完整性级别**：切勿使用带 Low Integrity 标签的虚拟环境连接 Medium 权限运行的 AutoCAD 进程，否则 `GetActiveObject` 会被 COM 拒绝。
- **常用环境变量**（均有默认值，可按需覆盖）：

| 变量名 | 说明 |
|---|---|
| `TZ_ROOT` | 允许新建/读写 DWG 图纸的工作根目录上限 |
| `TZ_OUTPUTS` | `document.readback` / `document.list` 允许只读访问的已保存 DWG 目录 |
| `TZ_STATE` | 队列数据库 `queue.sqlite3` 与确认回执目录（默认 `<repo>/.state`，测试请指向独立临时目录） |
| `TZ_CAD_PID` | 显式绑定目标 AutoCAD 进程 PID（多开 AutoCAD 实例时必填，否则 Worker 拒绝启动） |
| `TZ_CAD_EXE` / `TZ_TIANZHENG_ROOT` | `system_diagnose` 检查的 AutoCAD 与天正安装路径 |
| `TZ_PYTHON` / `TZ_EXTRA_SITE_PACKAGES` | `launch.cmd` 使用的 Python 可执行文件路径与 `bootstrap.py` 附加依赖目录 |

### 2. CLI 运行

先启动加载天正 T20 V9 的 AutoCAD 2024，再在仓库目录执行：

```powershell
python -m pip install -e .
.\launch.cmd doctor
.\launch.cmd capabilities
.\launch.cmd preview examples\wall.json
.\launch.cmd submit examples\wall.json
.\launch.cmd worker start
.\launch.cmd result <job_id>
```

- `worker once` 在当前进程执行单个任务后退出；`worker run` 持续轮询；`worker start` 后台拉起常驻 Worker。
- 提交动作只入队，不暗中拉起 AutoCAD。

### 3. Stdio MCP 接入

参考 [mcp-config.json](mcp-config.json) 配置客户端路径后即可通过 stdio 启动服务端：

```json
{
  "mcpServers": {
    "tianzheng-local": {
      "command": "C:/path/to/python.exe",
      "args": ["-X", "utf8", "-m", "tz_mcp", "serve"],
      "cwd": "<repo>"
    }
  }
}
```

---

## 配方写法与上下文示例

```json
{
  "operation_id": "room-demo-001",
  "steps": [
    {"id": "drawing", "op": "document.new", "args": {"path": "<repo>/outputs/room-demo-001.dwg"}, "timeout_s": 45, "bind": "doc"},
    {"id": "wall_a", "op": "wall.create", "args": {"start": [0, 0], "end": [4000, 0]}, "timeout_s": 15, "bind": "wall"},
    {
      "id": "door_b",
      "op": "opening.create",
      "timeout_s": 15,
      "args": {
        "host": {"handle": {"$ref": "wall.handles.0"}, "document_id": {"$ref": "wall.document_id"}},
        "point": [1500, 0],
        "kind": "普通门",
        "subkind": "普通门",
        "width": 900,
        "height": 2100
      },
      "bind": "door"
    },
    {"id": "save", "op": "document.save", "args": {}}
  ]
}
```

- 只有声明了 `bind` 的步骤才能通过 `$ref` 引用；`step.id` 与 `bind` 名称须符合 `[A-Za-z][A-Za-z0-9_]*`。
- `ObjectID` 仅作当前会话辅助标识，跨 `save` / `reopen` 必须使用十六进制 `handle`。

---

## 超时、`unknown_outcome` 与恢复流程

当步骤发生超时、模态框阻塞、COM 返回不明或写入后校验与请求不符时：

1. Worker 将失败事实持久化为 `unknown_outcome` 并锁定队列（对前台命令步骤定向投递最多 3 轮 `ESC`，对只读/纯 COM `NO_ESCAPE_STEPS` 步骤不发 `ESC`）。
2. 查看现场与已产出句柄后，必须手工确认并写明 `side_effects` 才可恢复：

```powershell
.\launch.cmd result <job_id>
.\launch.cmd acknowledge <job_id> --side-effects "已目视核对图纸，图内残留 1 根柱子且命令已结束"
.\launch.cmd worker start
```

`side_effects` 会同时永久写入任务 `error` 字段与 `<state>/acks/<job_id>.json` 回执中；`acknowledge` 只解除队列阻塞并将原任务标记为 `failed`，**绝不自动重放原任务**。

---

## 已知边界

- `opening.create` 目前仅支持并实测 `普通门` 与 `普通窗`（`kind` 与 `subkind` 须一致）；防火门、卷帘门、异形窗等特殊门窗未接入。
- `stair.rect_create` 创建双跑矩形梯，单跑梯段宽 `width` 实测有效范围为 `(0, 1230] mm`；若请求宽度被向导拒绝，会触发 `unknown_outcome` 保护。
- `stair.create`（直线梯）仍标记为 `unverified`，未经过本版本真机实测。
- 本版天正轴线为 `DOTE` 层普通 `AcDbLine` 直线（无 `TDbAxis` 对象），轴号与尺寸组为 `TDbAxisLabelSet` / `TDbDimension2`。
- `document.readback` 与 `document.list` 仅允许只读访问 `TZ_OUTPUTS` 目录内的已保存 `.dwg` 文件，若目标文件在 AutoCAD 中已打开则确定性拒绝。

---

## 离线单元测试

仓库内所有单元测试均使用本地伪造对象 / Mock 运行，不连接 AutoCAD、不改动生产队列状态：

```powershell
python -X utf8 -m unittest discover -s tests -v
```

更多细节参见 [docs/MCP-TOOL-SPEC.md](docs/MCP-TOOL-SPEC.md) 与 [docs/RELEASE-v1.0.1.md](docs/RELEASE-v1.0.1.md)。
