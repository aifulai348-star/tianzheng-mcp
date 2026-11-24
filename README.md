# 本机天正 MCP / CLI

目标：AutoCAD 2024 (`AutoCAD.Application.24.3`) + `D:/天正` T20 建筑 V9。
独立项目，不修改原 AutoCAD MCP。不要让两个服务同时编辑同一 CAD 实例。

当前代码是 v1.0.0 基线加 2026-11-24 增量。MCP 固定 9 个工具：`system_diagnose`、`list_capabilities`、`queue_submit`、`queue_acknowledge`、`queue_status`、`queue_cancel`、`worker_start`、`worker_stop`、`worker_status`；21 个操作由 `queue_submit` 的 recipe 选择。

当前 launch.cmd 与 MCP 配置调用原有 D:/python/python.exe，通过 bootstrap.py 只读加载现有 AutoCAD MCP 依赖。项目 venv 的 python.exe 带有 Low 完整性标签，已实测无法连接 Medium CAD，因此不作为真机入口。未修改安全标签或原有依赖环境；依赖目前共享。

**2026-11-17 第一阶段真机验收通过：最终同一代码版本连续 10 轮、60 个操作步骤及 90 项验收检查通过。** 覆盖墙体创建、上下文引用、保存关闭重开、handle 与几何核对，正常完成没有 ESC 参与。详细结果见 `docs/` 下的验收记录；早期失败及修复过程保留在各批次记录中。

**第二阶段实际 MCP 连续队列验收也已通过：30 个正常任务、180 个步骤，包含去重、冲突拒绝、排队取消、任务内失败停止后队列继续、上下文隔离、独占 worker 和退出检查。** 见 `docs/` 下的验收记录。真实模态、崩溃恢复、运行中取消、耐久与其他构件仍未验收，不能扩展为全部功能稳定的结论。

## 2026-11-24 增量

- `wall.split`：走 T20 屏幕菜单的 `TKATANA`，一个 recipe 形状覆盖两种宿主情况——门留在原墙，或迁到新段；接缝和对象数核对后，保存、关闭、分页 `document.list` 与两次 `document.readback` 都过，文件 hash 不变、没有文档残留。
- `opening.move` / `column.move`：按 `document_id` + `handle` + 平面内非零 `delta` 移动；门沿宿主墙轴，矩形柱四个顶点同步位移；保存关闭后两次回读一致。跨轴、越墙端、重叠、非矩形/外来 handle、短位移这些拒绝路径目前只有离线测试。
- `stair.rect_create`：真机建成 `TDbRectStair`。`width` 是单跑宽，向导只接受 (0,1230] mm：600/900/1200/1230 生效，1300/1500/3000 会被落回 1230。适配器现在回读比对，请求/实测不符就以 `unknown_outcome` 失败并点名已建句柄；实体可能已经留在图里，人工核对后走 `acknowledge`，不要盲目重放。
- `wall.create`：交点处可以连续拆分段；返回的 `effects.additional_wall_handles` 记录新段，避免后续改错对象。
- 本轮真机证据覆盖 19/21 个操作；`stair.create` 与 `roof.create` 仍未验证。`list_capabilities` 目前仍把 `document.save`、`document.reopen`、`entity.get`、`entity.verify`、`wall.find` 这 5 条留在 fallback，标签合并后会与下面的表一致。

## 运行

先通过 TArch20V9 配置启动加载天正的 AutoCAD，再在本目录执行：

```powershell
.\launch.cmd doctor
.\launch.cmd preview examples\wall.json
.\launch.cmd submit examples\wall.json
.\launch.cmd worker start
.\launch.cmd result 返回的job_id
```

`worker once` 在当前进程执行一个任务，`worker run` 持续执行；`worker start` 隐藏启动持续 worker。提交只入队，不暗中启动 CAD。MCP 可调用 worker_start。
`launch.cmd serve` 启动 stdio MCP，连接配置见 mcp-config.json，尚未修改用户全局注册配置。

## 上下文

成功步骤可设置 `bind: "wall"`，结果保存为任务上下文：

```json
{"wall":{"success":true,"document_id":"会话文档标识","handles":["A1"],"entities":[{"handle":"A1","object_id":123,"document_id":"会话文档标识","object_name":"TDbWall"}]}}
```

上述数值是格式示例，不是本机实测结果。B 步骤可引用整份对象，而非复制字符串：

```json
{
  "id":"door_b", "op":"opening.create", "timeout_s":15,
  "args":{
    "host":{"$ref":"wall.entities.0"},
    "point":[1000,0],
    "kind":0, "subkind":0, "width":900, "height":2100
  },
  "bind":"door"
}
```

`kind/subkind` 必须写成同一个已实测的类型串，目前只有 `普通门` 与 `普通窗`；两者走的是两条不同的天正命令（门 `TGDOOR`、窗 `TGWINDOW`），各开一块无标题调色板，尺寸行按自上而下的顺序写入，最后在宿主墙上点一下。建完会核对 `GetKind/GetSubKind`、DXF 的宽高与插入点、以及宿主墙洞面积的增量（`docs/OPENING-WINDOW-20261120.md`）。不要用包围盒中心比对插入点：门的包围盒含开启弧线。

`$ref` 支持字典路径和列表索引，仅能引用同一任务中前序成功绑定。变量写入 SQLite，每步保存。ObjectID 仅为当前 CAD 会话的辅助标识，跨关闭重开优先使用 handle。墙体自动分割可能使旧句柄失效，解析失败立即停止，不能自动猜另一面墙。

## 超时和 ESC

每步 `timeout_s` 默认 30 秒，最大 300 秒。独立 watchdog 不调用 COM，以 100 ms 周期检查截止时间、CAD 主窗口禁用状态和可见原生对话框。

发现异常先持久化 unknown_outcome，再定向向绑定 CAD PID 的窗口投递 ESC（最多三轮）；不向系统前台注入键盘输入。发送成功只代表 Windows 接收消息，不能保证每种天正模态框都会响应。

原 COM 调用未返回时，不创建第二个执行线程；旧进程继续持有 OS 锁，防止重复执行。即使 ESC 后返回也不继续后面的步骤。使用 result 查看异常和已完成上下文；人工核对图纸并确认命令结束后：

```powershell
.\launch.cmd acknowledge JOB_ID --inspected --side-effects "图里已有 1 根柱子，命令已结束"
.\launch.cmd worker start
```

`--side-effects` 是**必填**的：`unknown_outcome` 不等于没画（实测超时的柱任务是**已经建出柱子**才超时的）。
这句话会被永久留档——job 的 `error` 里、以及 `<state>/acks/<job_id>.json` 回执，`queue_status` 会带回整份回执。
acknowledge 只解除该任务的不确定性并标为 failed，**不重放原任务**；worker 未退出时会被 OS 锁拒绝。
运行中取消在下一步骤边界生效；worker stop 在任务边界停止，不强杀 AutoCAD。

## 验证范围

以下是 2026-11-24 的真实状态；每条都对应真机证据，没验过的就写没验过。

| 能力 | 状态 | 证据 / 边界 |
|---|---|---|
| `document.new` | 已验证 | `Documents.Add` + `SaveAs`，文档数核对 |
| `document.save` / `document.reopen` | 已验证（标签待合并） | 每个端到端任务都用；保存、关闭、重开后 handle 仍在 |
| `document.close` | 已验证 | 只关 job 自有、活动、已保存图；同路径重开、外来文档、未保存文档均拒绝（拒绝路径离线测试） |
| `document.readback` | 已验证 | 只读绝对路径已保存 DWG；1..100 个 handle；仅 ModelSpace；读完关闭，文件 hash 不变 |
| `document.list` | 已验证 | 同 readback 的只读打开/校验/关闭路径；`limit` 1..100、`after_handle` 分页；仅 ModelSpace；实测 3 页 7 个实体 |
| `entity.get` / `entity.verify` | 已验证（标签待合并） | 按 handle 读属性、校验 `ObjectName` |
| `wall.create` | 已验证 | `TGWall` 面板 + 画布点选；MCP 全链路 4 面墙 35A–35D；交点可连续拆分段 |
| `wall.find` | 已验证（标签待合并） | 点落在墙包围盒内且必须**唯一命中**；0 个或多个命中都拒绝 |
| `wall.split` | 已验证 | 只接受本任务创建的原生墙 + 绝对 DWG 路径 + `document_id` + 墙 handle + Z=0 分割点；`TKATANA` 屏幕菜单；门留原墙/迁新段两种都过；接缝、对象数、保存关闭后的两次 readback 核对；超时/焦点丢失仅离线测试 |
| `opening.create` | 已验证，**仅 `普通门` 与 `普通窗`** | 门 3CF 900×2100，洞面积 0→1.89；窗 409 1200×1600、sill 600，洞面积 1.89→3.81；`GetKind/GetSubKind`、DXF 宽高/插入点、洞面积增量核对；保存重开仍在 |
| `opening.move` | 已验证 | `document_id` + `handle` + Z=0 非零 `delta`；沿宿主墙轴；DXF 插入、宽、高、旋转与 `HoleArea` 一致；实测 800；跨轴/越墙端/重叠/短位移拒绝仅离线测试；保存关闭后两次 readback，hash 不变 |
| `column.create` | 已验证 | `TGColumn` 面板逐字段键入 + 画布点选；handle 35B；DXF 回读宽/深/高/插入点；点选前 OSMODE 置 0 |
| `column.move` | 已验证 | `document_id` + `handle` + Z=0 非零 `delta`；独立矩形原生柱；四顶点同步位移，高度和 flags 不变；实测 500；非矩形/外来 handle/短位移拒绝仅离线测试 |
| `slab.create` | 已验证 | `TSlab` 边界/洞多段线，板厚可回读（是否严格等于请求值本轮未判）；当前适配器只支持 Z=0 轴对齐矩形，最多一个严格位于边界内的洞 |
| `stair.rect_create` | 已验证 | `TRStair` 平面点建 `TDbRectStair`；`height`/`steps`/`step_width`/`width` 保存关闭重开后回读；向导建双跑梯，`GetBoundingBox` 跨两跑，不等于单跑宽；`width` 单跑宽只支持 (0,1230] mm，600/900/1200/1230 生效，1300/1500/3000 落回 1230；请求/实测不符即 `unknown_outcome` 并点名已建句柄，实体可能已在图里，人工核对后 acknowledge，禁止盲目重放 |
| `stair.create` | **未验证** | `TLStair` 只有历史证据，没有真机验收 |
| `roof.create` | **未验证** | `TSlopeRoof` 边界消费没有真机验收 |
| `annotation.text` / `polyline.create` | 已验证 | 普通 `AcDbText` / `AcDbPolyline`；文字单行不超过 160 字符；多段线 2..100 点、Z=0、至少两个不同点 |

其它门窗类型（防火门、弧窗、凸窗…）仍未接；`opening.create` 的 `kind/subkind` 只接受 `普通门` 与 `普通窗`，且两者必须相同。

`list_capabilities` 的 `validation` 字段是这张表的机器可读版本。当前它会报 14 条 `verified_live`，因为上面 5 条本轮已验证的标签还没合并；这是标签口径差，不是能力没验。合并后两边应一致；调用方以实际返回为准，不要相信文档里的口头承诺。

Agent 已经能在只读 `list_capabilities` 的前提下自行规划出一个矩形房间（四面墙 + 柱 + 门 + 窗 + 保存 + 回读），
见 `docs/AGENT-CLOSEDLOOP-20261120.md`。

命令内部使用固定原生命令配方，部分旧适配器仍包含 AutoLISP 包装，不接受调用方任意代码；路径限制在项目根目录（默认 `D:/CAD绘图`，可用环境变量 `TZ_ROOT` 覆盖），拒绝覆盖已存在的工作图。任何创建后异常都归为未知结果，避免重试重复构件。

2026-11-17 别墅回归排查：绘墙改为直接命令串，保留退出空输入；正交墙按目标位置识别，交墙拆分记录在 `effects.additional_wall_handles`，避免改错对象。原生命令在窗口最小化或纸空间时拒绝发送；绘墙前局部取景，避免本机天正的视图相关联接偏移。内部完成等待超时也会先持久化未知结果再定向ESC。坡屋顶改为原生命令提示下按确切边界handle选择，避免外层AutoLISP执行环境问题（坡屋顶仍只有历史证据，见上表）。最终同配方36步队列通过，重开后14墙、2梯、1坡顶，40项回归检查通过；这不代表别墅设计完整。门窗和轴线的参考实现、本机实测及未解决项（这两份记录未随仓库发布。）

### 历史记录（保留，不要当现状读）

调度层测试使用受控适配器，不代表天正对象创建已真机通过。已编写墙、直跑梯和坡屋顶命令适配器，命令序列来自本机技能历史证据；门窗入口保持关闭，等待类别和宿主探针。本次适配器的成功率、ESC 在实际模态框中的响应、构件几何与三维关联，必须以真机验收为准。

## 测试

```powershell
D:/python/python.exe -X utf8 -m unittest discover -s tests -v
```

离线回归 211 项通过；测试不连接 CAD。
