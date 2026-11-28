# MCP 工具调用规范（v1.0.1 候选）

面向要调用这套能力的人（或 Agent）。写的是**现在真实行为**，不是设计意图；每条状态转换、
每个错误串都有实测或离线 mock 支撑。既有真机记录见 `docs/FAIL-E2E-20261120.md`、
`docs/LIVE-E2E-20261119.md`；v1.0.1 关闭/读回的真机范围见
internal acceptance report，未覆盖边界只有离线 mock 覆盖；
分页发现 `document.list` 的真机范围见
internal acceptance report，负路径仍只有离线覆盖；
宿主字段与跨页图档变化检测未验证（§4、§7）。

## 0. 心智模型

```
Agent ──queue_submit──▶ 队列(sqlite) ──worker(独立进程)──▶ CAD/T20 ──▶ DXF 读回校验
         ◀──queue_status──            ◀── 状态：succeeded / failed / cancelled / unknown_outcome
```

三条必须记住的规则：

1. **提交不等于执行**。`queue_submit` 只入队；必须有人显式 `worker_start`（或跑 `bootstrap.py worker once/run`）。
2. **执行结果只有成功、失败、取消、未知四种**。未知（`unknown_outcome`）不是失败，也绝不自动重试。
3. **一次操作一个 `operation_id`**。同 id 同内容重复提交返回同一个 job，不会画第二遍；同 id 换内容直接拒绝。

## 1. 九个工具

| 工具 | 入参 | 返回 | 失败形态 |
|---|---|---|---|
| `system_diagnose` | — | `{cad_executable, tianzheng_root, progid, session?}`；连不上给 `connection_error` | 不抛错，诊断信息在返回体里 |
| `list_capabilities` | — | 71 个 op，返回 `{required, optional, validation}` | — |
| `queue_submit` | `recipe` | `{job_id}` | **提交期拒绝**（见 §3 A 类） |
| `queue_status` | `job_id` | 整条记录（见 §2） | `Unknown job` |
| `queue_cancel` | `job_id` | `{cancel_requested: true}` | `Unknown job` |
| `queue_acknowledge` | `job_id`, `side_effects`（必填） | `{acknowledged: true, replayed: false, side_effects, receipt}` | 只允许 `unknown_outcome`，其它报 `Only unknown_outcome can be acknowledged`；`side_effects` 为空报 `side_effects must describe what the drawing actually shows` |
| `worker_start` | — | `{spawned_pid, spawned_at, log}` | 后台进程起来后再拒（错误写 `worker.log`，可用 `worker_status` 与队列状态看结果） |
| `worker_stop` | — | 停止请求落 `.state/stop`，worker 在**下一个步骤/任务边界**退出并自己删掉该文件 | — |
| `worker_status` | — | `{pid, state: ready/stopped, cad_pid, updated, state_dir}` | 陈旧时间戳说明 worker 卡在一次 COM 调用里 |

## 2. `queue_status` 的返回结构

```json
{
  "seq": 170, "id": "<uuid>", "token": "<operation_id>", "digest": "<sha256>",
  "recipe": {"operation_id": "...", "steps": [...]},
  "state": "queued|running|succeeded|failed|cancelled|unknown_outcome",
  "context": {"doc": {"document_id": "...", "handles": [...]}},
  "results": [{"id": "wall", "op": "wall.create", "state": "succeeded",
               "result": {"success": true, "handles": ["35A"], "entities": [...], "evidence": {...}}}],
  "error": null, "cancel": 0, "updated": 1795140810.06
}
```

- `results` 是**逐步骤**的：`state` 取 `running/succeeded/failed/unknown_outcome`，失败步带 `error`，超时/模态中断步带 `recovery: {reason, escape}`。
- `context` 只放**已确认**的产物（`bind` 绑定的 document/handle 列表）；失败步不会往里写。
- `error` 是任务级原因：前置条件错、超时、COM 拒绝、worker 中断。

## 3. 错误分类（调用方必须分开处理）

### A. 参数错误 —— 提交期就拒，队列里没有痕迹
工具直接返回错误文本（MCP `isError`），不会产生 job：

| 文本 | 含义 |
|---|---|
| `Unsupported operation` | op 名不在 71 条里 |
| `Invalid arguments for <op>` / `<op>: requires [...]` | 缺必填或多传字段 |
| `left_width must be nonnegative finite number` 等 | 数值非法（负数/NaN/布尔） |
| `Only measured ordinary door/window kind strings are supported` | 门窗类型不在 `普通门`/`普通窗` |
| `kind and subkind must name the same measured type` | 门/窗类型自相矛盾 |
| `operation_id reused with different input` | 复用 id 但内容变了 |
| `Forward or missing reference: <ref>` | `$ref` 指向上游还没产出的绑定 |
| `timeout_s must be finite, >0 and <=300` | 单步超时越界 |
| `Readback requires 1..100 handles` | `document.readback` 的 `handles` 为空或超过上限 |
| `Readback handles must be hexadecimal strings` | `handles` 里有非十六进制字符串 |
| `Readback path must be a non-empty string` | `document.readback` 的 `path` 不是非空字符串 |
| `Readback path must be an absolute DWG path` | `document.readback` 的静态 `path` 不是绝对 `.dwg` 路径 |
| `Discovery path must be a non-empty string` | `document.list` 的 `path` 不是非空字符串 |
| `Discovery path must be an absolute DWG path` | `document.list` 的静态 `path` 不是绝对 `.dwg` 路径 |
| `Discovery limit must be an integer 1..100` | `limit` 不是 1..100 的整数（布尔、浮点、字符串都拒） |
| `after_handle must be a hexadecimal handle without 0x prefix` | 游标不是裸十六进制串 |

处理：改正参数重新提交，可以复用 `operation_id` 吗——**不能**，改内容就必须换新 id；只有完全相同的重提交才会命中幂等。

### B. 环境/前置条件错误 —— job 落 `failed`，现场没被动过
例：`No owned active drawing; run document.new first`（没先开图就画墙）。
处理：补齐前置步骤重提交（新 id）。这类错误**确定性**，可以直接重试。

v1.0.1 的确定性拒绝也属于这一类（没动图，`failed`）：`No owned drawing to close; run document.new in this job first`、
`Owned drawing has unsaved changes; refusing to close`、`Active drawing differs from owned document; close refused`、
`Readback is restricted to saved test artifacts under ...`、`Target drawing is already open; readback will not take over an existing document`。
其中读回路径的静态形状在提交期检查，绝对路径、已存在的普通 `.dwg`、解析后仍在 allowed outputs 内
这些事实在执行期检查。

`document.list` 与 `document.readback` 共用同一段只读打开/核验/关闭代码，因此确定性拒绝也相同，只是
消息里的动作名换成 `document.list`（例如 `Target drawing is already open; document.list will not take over an existing document`）。

### C. 结果未知 —— job 落 `unknown_outcome`，必须人工确认
来源：超时（`timeout`）、窗口弹模态（`modal`）、命令完成超时（`command_completion_timeout`）、
worker 被杀（`Worker interrupted; do not replay automatically`）、建后校验与请求不一致（`OutcomeUnknown: ...`）。
v1.0.1 还明确包括：读回时 `Documents.Open` 是否生效不明、返回文档的 FullName/ReadOnly 无法核实、
本操作打开的文档 `Close(False)` 是否生效不明；这些情况下**不重试、不猜**，保留 `unknown_outcome`。
这些不确定边界目前只有离线 mock 覆盖（真机未触发 COM 超时或 Open/Close 不确定）。

此时**队列进入不确定态**：任何 worker 启动都会被拒
`Uncertain prior outcome: inspect and acknowledge before starting worker`，直到：
1. 人看一眼图（或查 `results[*].recovery`、DXF）；
2. `queue_acknowledge(job_id, side_effects)` —— **必须写清图上实际发生了什么**，只清账，`replayed: false`，**不会重放**。

`side_effects` 是必填的一句话，例如 `"after the timeout timeout-101219.dwg shows columns=1 walls=0 openings=0 cmdactive=0"`。
空字符串、纯空白、`null` 都被拒（`side_effects must describe what the drawing actually shows`），
超过 4000 字符也拒。写下的这句会被**永久留档**在两处：

- job 的 `error` 变成 `Operator inspected outcome; original job will not be replayed. Observed side effects: <这句话>`；
- 回执文件 `<state>/acks/<job_id>.json`：`{job_id, state_before, error_before, side_effects, recorded_at}`，
  `queue_status` 会把整份回执以 `acknowledgement` 字段带回来。

> 为什么要逼着写：`unknown_outcome` **不等于没画**。s6 的超时任务就在图里留下了 1 根柱子；
> 如果只写「ack 了」，后来的人会以为那张图是干净的，下一次画墙就会撞在柱子旁边。
> 所以「未知」永远按「可能有副作用」处理，处理完必须留下观察结果。

### D. 并发/多实例保护
- 同一队列只允许一个 worker：抢不到 OS 锁就写 `worker-refusal-<pid>.json` 并报 `Another worker owns this queue`。
- 多开 AutoCAD 且没有 `TZ_CAD_PID` 时，worker 直接拒绝启动：
  `Several AutoCAD windows are running (...). COM would answer for the first one...`
  —— 因为 COM 只认第一个实例，盲发可能画到别人的图上。

## 4. 71 条 capability（`list_capabilities` 的 validation 字段）

| op | 必填 | 状态 |
|---|---|---|
| `document.new` | path | ✅ verified_live |
| `document.save` | — | ✅ `verified_live` |
| `document.reopen` | — | ✅ `verified_live` |
| `document.close` | — | ✅ `verified_live` |
| `document.readback` | path, handles | ✅ `verified_live` |
| `document.list` | path | ✅ `verified_live` |
| `wall.create` | start, end | ✅ 面板 + 点击，按几何段认领新对象；跨已有墙体时返回共线多段 `handles` |
| `wall.split` | path, document_id, handle, point | ✅ `verified_live` |
| `wall.edit` | document_id, handle（可选 `left_width`/`right_width`/`height` 至少一项） | ✅ `verified_live` |
| `axis.rect_create` | origin, bays, depths | ✅ `verified_live` |
| `axis.from_walls` | document_id, wall_handles | ✅ `verified_live` |
| `axis.single_create` | start, end（可选 overhang，默认 1000 mm） | ✅ `verified_live` |
| `axis.annotate` | start_point, end_point（可选 side，默认 double；start_label，默认 1） | ✅ `verified_live` |
| `column.create` | point, width, depth, height | ✅ TGColumn 面板逐字段 + 一次点击，DXF 读回，OSMODE/VTENABLE 临时置 0 |
| `column.move` | document_id, handle, delta | ✅ `verified_live` |
| `column.rotate` | document_id, handle, angle_deg | ✅ `verified_live` |
| `opening.create` | host, point, kind, subkind | ✅ 普通门走 TGDOOR、普通窗走 TGWINDOW 面板 + 一次点击（门 3C8/3CF、窗 409；仅普通门窗） |
| `opening.move` | document_id, handle, delta | ✅ `verified_live` |
| `opening.edit` | document_id, handle（`width`、`height`、`sill`、`text_height` 至少一项，单位 mm） | ✅ `verified_live` |
| `opening.group_create` | document_id, handles（2–10 个不同的十六进制句柄）, label（去除首尾空白后 1–64 字符，无空白、双引号或控制字符） | ✅ `verified_live` |
| `entity.copy` | document_id, handle, delta | ✅ `verified_live` |
| `entity.move` | document_id, handle, delta | ✅ `verified_live` |
| `entity.rotate` | document_id, handle, base_point, angle_deg | ✅ `verified_live` |
| `entity.scale` | document_id, handle, base_point, scale_factor | ✅ `verified_live` |
| `entity.mirror` | document_id, handle, axis_start, axis_end（可选 erase_source，默认 false） | ✅ `verified_live` |
| `entity.delete` | document_id, handle | ✅ `verified_live` |
| `opening.relabel` | document_id, handle, label（去除首尾空白后 1–64 字符，支持中文，无控制字符） | ✅ `verified_live` |
| `opening.mirror` | document_id, handle, direction（`left_right` 或 `inside_outside`） | ✅ `verified_live` |
| `slab.create` | boundary, thickness | ✅ TSlab 消费闭合多段线/洞口，板厚读回 |
| `slab.edit` | document_id, handle（`thickness`、`elevation` 至少一项，单位 mm） | ✅ `verified_live` |
| `annotation.text` | text, point, height | ✅ 普通 AcDbText |
| `annotation.mtext` | text, point, width（可选 height、rotation_deg、attachment） | ✅ `verified_live` |
| `dim.aligned` | ext1, ext2, text_position（可选 text_height） | ✅ `verified_live` |
| `dim.linear` | ext1, ext2, dim_line_point（可选 rotation_deg，默认 0；可选 text_height） | ✅ `verified_live` |
| `dim.radial` | center, chord_point（可选 leader_length，默认 0；可选 text_height） | ✅ `verified_live` |
| `dim.angular` | vertex, first_endpoint, second_endpoint, text_position（可选 text_height） | ✅ `verified_live` |
| `dim.t20_angular` | first_pick, second_pick, arc_point（平面点，两个拾取点须相距大于 `1e-6`） | ✅ `verified_live` |
| `dim.diameter` | chord_point, far_chord_point（可选 leader_length，默认 0，可选 text_height） | ✅ `verified_live` |
| `line.create` | start, end | ✅ `verified_live` |
| `circle.create` | center, radius | ✅ `verified_live` |
| `arc.create` | center, radius, start_angle_deg, end_angle_deg | ✅ `verified_live` |
| `ellipse.create` | center, major_axis（相对中心的半长轴向量）, radius_ratio（`1e-6 < r <= 1`） | ✅ `verified_live` |
| `hatch.create` | document_id, boundary_handle（可选 pattern_name、pattern_scale、pattern_angle_deg） | ✅ `verified_live` |
| `layer.create` | name（可选 color，ACI 1–255） | ✅ `verified_live` |
| `layer.list` | 可选 prefix | ✅ `verified_live` |
| `layer.edit` | name（`color`、`layer_on`、`freeze`、`lock` 至少一项） | ✅ `verified_live` |
| `layer.delete` | name | ✅ `verified_live` |
| `block.define` | name, items（可选 base_point，默认原点） | ✅ `verified_live` |
| `block.insert` | name, point（可选 scale_x、scale_y、scale_z、rotation_deg） | ✅ `verified_live` |
| `block.list` | 可选 prefix、include_system | ✅ `verified_live` |
| `block.delete` | name | ✅ `verified_live` |
| `entity.set_layer` | document_id, handle, layer | ✅ `verified_live` |
| `polyline.create` | points | ✅ 普通 AcDbPolyline |
| `rectangle.create` | corner1, corner2（可选 elevation、constant_width） | ✅ `verified_live` |
| `polyline.edit` | document_id, handle（closed、constant_width、elevation 至少一项） | ✅ `verified_live` |
| `entity.set_color` | document_id, handle, color（整数 0–256） | ✅ `verified_live` |
| `layer.set_active` | name | ✅ `verified_live` |
| `entity.query` | 可选 object_name、layer、bbox、limit（整数 1–100，默认 100） | ✅ `verified_live` |
| `stair.create` | point | ⚠️ 历史证据，未真机验证 |
| `stair.rect_create` | point, height, steps, step_width, width | ✅ `verified_live` |
| `annotation.text_edit` | document_id, handle（`text`、`height`、`rotation_deg` 至少一项） | ✅ `verified_live` |
| `column.edit` | document_id, handle（`width`、`depth`、`height` 至少一项，单位 mm） | ✅ `verified_live` |
| `roof.edit` | document_id, handle（`elevation`、`slope_angle` 至少一项，单位 mm 和度） | ✅ `verified_live` |
| `roof.create` | boundary, angle, overhang | ✅ `verified_live` |
| `entity.get` | handle（可选 `document_id`） | ✅ `verified_live` |
| `entity.verify` | handles, object_name | ✅ `verified_live` |
| `wall.find` | point | ✅ `verified_live` |
| `space.create` | point（封闭原生墙体内的平面点，Z=0） | ✅ `verified_live` |
| `space.edit` | document_id, handle, name（去除首尾空白后 1–64 字符，无控制字符） | ✅ `verified_live` |
| `dim.wall` | start, end | ✅ `verified_live` |
| `wall.fix_corner` | corner1, corner2 | ✅ `verified_live` |

> `verified_live` 只覆盖表中写明的真机场景；未保存/外来/身份不可读拒绝、路径越界、只读/身份核对失败、
> Open/Close 不确定与 COM 超时等边界仍只有离线 mock 覆盖，不宣称真机验证。

### 4.1 实测行为补充（`wall.create` / `opening.create` / `column.create` / `stair.rect_create`）

以下为 2026-11-21 复式户型 179 步实战复刻（`C:/CADWorkspace/户型复刻_20261121`）验证并并入主线的稳定行为：

- **`wall.create` 跨墙分段多句柄返回**：当新墙沿 Axis 穿过既有墙体被天正自动打断为共线多段时，`select_wall_segments` 会在单段匹配失败后验证所有共线片段是否无间断铺满请求区间 `[low, high]`，若连续覆盖则按顺序在 `handles` 与 `entities` 中返回全部段句柄（并将每一段均登记到 `created_walls` 供后续 `wall.split` 使用），不再误报 `OutcomeUnknown`。
- **`column.create` 视图平滑过渡保护**：插入柱前固定设置 `VTENABLE=0` 并在动态兼容视口外框（至少覆盖 `[-2000, -2000]` 至 `[31300, 15200]`，若目标柱点超出则自动外扩 2000 mm）下执行，防止平滑缩放动画期间点击落入上一帧视口中心。
- **`opening.create` 延迟刷新与中心线吸附**：通过 `wait_hole_delta` 轮询宿主墙 `HoleArea`（允许 `0.02 m²` 取整精度，最长 5 s）；`verify_opening_dxf` 允许 DXF 插入点匹配像素容差内的点击世界坐标，或匹配被天正吸附到墙中心线后的请求中心 `point`。
- **`stair.rect_create` 宽度口径（2026-11-24 实测）**：请求里的 `width` 是**单跑梯段宽**，落成 `StairWidth`/`StairWidth2` 属性；向导实际生成的是**双跑矩形梯**（本次 `StepNum1`/`StepNum2` = 9/9、`StepTotalNum` 18），因此 `GetBoundingBox` 的 2560 × 3340 覆盖两跑（2 × 1200 + 160 梯井）与休息平台，**不等于**请求的 1200；验收核对 `StairWidth` 属性，不核对包围盒宽度。
- **`stair.rect_create` 支持范围：单跑宽 (0, 1230]**（2026-11-24 实测，保存关闭后双回读）：600 / 900 / 1200 / 1230 生效；1300 / 1500 / 3000 一律落回 **1230**。原因是向导建梯时梯间宽固定为 2560（= 2 × 1230 + 100 梯井），`StairWidth`/`StairWidth2` 只改单跑宽，超过梯间宽一半的写入被向导拒绝（`Property '<unknown>.StairWidth' can not be set.`），而当前参数面没有梯间宽/通用属性写入口。需要 > 1230 的单跑宽，须先扩展梯间宽能力。
- **`stair.rect_create` 验收口径（修复后，2026-11-24）**：写入后对全部请求属性（含被拒的 `StairWidth`/`StairWidth2`）回读比对，任一不符立即按 `unknown_outcome` 失败，消息列举 `requested` / `measured` / 向导默认值 / 被拒写入，并点名已创建的 `TDbRectStair` 句柄与「remains in the drawing - side effect present, do not replay blind」；`validation.properties` 只有在实测 = 请求时才为 `true`。此前的实现只校验可写属性、把宽度不符记进 `properties_wizard_owned` 后仍标通过，属于假通过，已删除。成功时 `evidence` 返回：
  `{"placement": "bounding_box_min", "rotation_deg": ..., "properties_requested": {...}, "properties_wizard_default": {...}, "properties_measured": {...}, "properties_applied": {...}, "properties_wizard_owned": [...], "stair_width": ..., "bounds": [[x0, y0], [x1, y1]]}`。
  失败作业不会自动重放：先 `queue_acknowledge` 记录图中残留实体，再决定重建。

### 4.2 墙体切割 `wall.split`（`verified_live`，2026-11-24）

基于 P5 / P5B 真机验证（`TKATANA`）与 P6 接入的能力：2026-11-24 经真实 stdio MCP `queue_submit` + Worker 跑通两例门洞归属（断点 3000 门留原墙 35A；断点 1000 门迁新墙 3D0），保存关闭后分页 `document.list` 与双次 `document.readback` 一致、文件哈希不变，已升格为 `verified_live`（证据见 internal acceptance report）。

- **入参必填项**：
  - `path`：绝对 `.dwg` 路径，必须与当前 job 拥有的打开图纸一致；
  - `document_id`：当前 job 的 `document_id`；
  - `handle`：无 `0x` 前缀的十六进制句柄字符串；
  - `point`：切割拾取参考点 `[x, y]`（平面点 `Z=0`），须落在墙厚带内且距墙端至少 `10.0 mm`。
- **同作业墙轴约束与预检**：
  - 仅接受**同一个 job 内**由 `wall.create` 创建的直墙（从 `created_walls` 读取创建起终点校验墙轴向与中心线，因为已证实保存后墙的 DXF 300 实体数据在原生切割前后不变，无法独立佐证实时墙轴）。
  - 分割位置与已有门窗（`TDbOpening`）沿墙包围盒区间须保持至少 `10.0 mm` 净距；当前版本不支持单面墙存在多个洞口。
- **执行链路与验证契约**：
  - 不使用命令行输入（`TDivWall` / `T20_WALLKNIFE` 不可用，`TKATANA` 直接点墙中线会被忽略），而是通过窗口消息 `BM_CLICK` 触发 T20 屏幕菜单 `墙体 → 墙体切割`，随后在 AutoCAD 确认位于前台时向**可见墙体边线**发送单次前景鼠标点击并等待 `CMDACTIVE=0`。
  - 割后断言严格新增 `+1 TDbWall` 与 `+1 GlwKatana`、原句柄保留且无实体删除、两段共线并无重叠/无缝拼合在请求断点 `±2.0 mm` 内、`GlwKatana` 落在断口 `±50.0 mm` 内、其余原有实体属性不变；若存在洞口，则扫描门洞全部 DXF `330` 引用（保留不可解析行以防漏检），核对唯一 `TCH_WALL` 宿主与原 `HoleArea` 仅落在唯一真实所属墙段。
  - 若菜单派发之后发生任意超时、失焦或后验失败，抛 `CommandCompletionTimeout` 使引擎落入 `unknown_outcome` + `quarantine` 并触发既有 ESC 恢复，绝不自动保存、关图或重放。

### 4.3 构件平移 `opening.move` 与 `column.move`（`verified_live`，2026-11-24）

基于 P3 / P4 脚本真机实证沉淀的正式 MCP 操作：2026-11-24 经真实 stdio MCP `queue_submit` + Worker 端到端验收通过（门 3CF 沿墙 35A 平移 800、柱 3D0 平移 500，保存关闭后双次回读一致、哈希不变），已升格为 `verified_live`。

- **`column.move`（独立矩形柱平移）**：
  - **入参必填项**：`document_id`（当前 job 的 `document_id`）、`handle`（无 `0x` 前缀十六进制句柄）、`delta`（非零平面向量 `[dx, dy]` 或 `[dx, dy, 0]`）。
  - **前置校验**：仅允许 `TDbColumn` 且 `SectionShapeText == '矩形'` 的柱；移动前从 `column_dxf` 读取 `TCH_COLUMN`、组码 `11` 插入点及 4 个组码 `10` 截面顶点。
  - **执行与后验**：下发原子 LISP `_.MOVE`（以原组码 `11` 插入点为基点、`_non` 显式目标点），完成后断言全图句柄集合不变、新组码 `11` 与 4 个组码 `10` 顶点的实际偏移与 `delta` 偏差均 `<= 0.01 mm`、柱高与 `70/71/72` 标志位不变；否则抛 `OutcomeUnknown`。
- **`opening.move`（普通门窗沿原宿主墙平移）**：
  - **入参必填项**：`document_id`、`handle`、`delta`（非零平面位移 `[dx, dy]`）。
  - **前置校验**：仅允许 `TDbOpening`；通过全量 DXF `330`（`entity_wall_hosts`）解析出唯一 `TCH_WALL` 宿主并提取墙轴框架，严格约束 `delta` 必须沿宿主墙方向（垂直/水平偏移容差 `1e-6`），目标位置左右边缘须距墙端至少 `10.0 mm` 且不得与同墙其他洞口重叠（`10.0 mm` 保护净距）。
  - **执行与后验**：下发原子 LISP `_.MOVE` 后，断言句柄集不变、DXF 组码 `10` 插入点平移误差 `<= 0.5 mm`、`Width`/`Height`/`Rotation` 不变（`<= 0.01`）、唯一 `TCH_WALL` DXF `330` 宿主仍为原墙句柄、且所有墙体 `HoleArea` 变化 `<= 0.02 m²`；任一偏差均抛 `OutcomeUnknown`。

### 4.4 原生实体平移与旋转（`pending_live`）

`entity.move` 和 `entity.rotate` 支持 `AcDbText`、`AcDbPolyline`、`AcDbLine`、`AcDbCircle` 和 `AcDbArc`。`delta` 与 `base_point` 接受二维或 Z 为 0 的三维坐标。两个操作均在写入前校验文档和句柄归属，并拒绝天正墙、柱、门窗等非白名单类型。

写入后核对 ModelSpace 句柄集、目标类型及 DXF 读回。按实体类型检查端点、圆心、插入点或有序顶点是否达到目标位置，并核对半径、文字内容与字高、多段线闭合标志。位置和尺寸容差为 0.05 mm，圆弧角和文字旋转角按 2π 比较，容差为 0.001 rad。成功结果包含同一目标句柄及变换前后的 DXF 快照。写后读回不符时返回 `unknown_outcome`。

## 5. 配方（recipe）的写法

```json
{"operation_id": "agent-20261120-001",
 "steps": [
   {"id": "doc",  "op": "document.new",  "args": {"path": "C:/CADWorkspace/tianzheng-mcp/outputs/room.dwg"}, "timeout_s": 90, "bind": "doc"},
   {"id": "wall", "op": "wall.create",   "args": {"start": [0,0], "end": [4000,0]}, "timeout_s": 90, "bind": "wall"},
   {"id": "col",  "op": "column.create", "args": {"point": [2000,600], "width": 600, "depth": 600, "height": 3000},
                  "timeout_s": 240, "bind": "column"}
 ]}
```

- 提交时整个配方包在 `recipe` 字段里：`queue_submit({"recipe": {...}})`，不是直接把配方当参数。
- `bind` 把该步产出的 `{document_id, handles}` 存进 `context`，后续步骤用 `{"$ref": "wall"}` 引用。
  **只有写了 `bind` 的步才能被引用，`id` 本身不能**；引用要写绑定的名字（`wall.handles.0`），
  写成 `{"$ref": "id"}` 会在提交期报 `Forward or missing reference`。
- `step.id` 与 `bind` 名都必须是 `[A-Za-z][A-Za-z0-9_]*`：**不能有连字符**（`get-w1` 会被拒）。
- `$ref` 可以出现在任意位置，包括标量位置 —— 所以 `entity.get` 能这样回读上一步建出来的对象：
  `{"id": "get_w1", "op": "entity.get", "args": {"handle": {"$ref": "w1.handles.0"}}}`。
- `on_error` 只支持 `stop`；200 步上限；单步 `timeout_s` ≤ 300。
- 门窗的 `host` 必须写成 `{"handle": "35A", "document_id": "..."}`，或按字段分别用 `$ref` 绑定到墙的
  `handles` 数组元素与 `document_id`：`{"handle": {"$ref": "wall.handles.0"}, "document_id": {"$ref": "wall.document_id"}}`
  （`wall.create` 返回的是 `handles` 数组而非顶层 `handle`；两个字段都要给，不能整个 `host` 写成 `{"$ref": "wall"}`）。
- **同一 job 内的验收步**：worker 在每个 job 开始时清空内存所有权（`doc/owned_doc/path/document_id` 全部清空），
  所以上一个 job 的图不会因为 worker 常驻而被隐式续用。旧配方里的 `document.reopen` + `entity.get`
  仍照原样工作（2026-11-20 的 room2 配方就是这么验的）。跨 job 要读已保存的图，只能用 §5.1 的 `document.readback`（不知道 Handle 时先用 §5.2 的 `document.list`
  拿一页 Handle）；直接 `entity.get` 仍会落 `failed`（`No owned active drawing; run document.new first`）。

## 5.1 跨 job 只读回读（v1.0.1）

job A 新建、保存并**显式**关闭自己创建的测试图；`document.close` 不会在 job 结束时隐式执行：

```json
{"steps": [
  {"id": "new",   "op": "document.new", "args": {"path": "C:/CADWorkspace/tianzheng-mcp/outputs/job-a.dwg"}, "bind": "doc"},
  {"id": "save",  "op": "document.save", "args": {}},
  {"id": "close", "op": "document.close", "args": {}}
]}
```

job B 在另一个 job 里按已保存路径和 Handle 只读回读（不需要、也不接受旧 job 的 `document_id`）：

```json
{"steps": [
  {"id": "read", "op": "document.readback",
   "args": {"path": "C:/CADWorkspace/tianzheng-mcp/outputs/job-a.dwg", "handles": ["35A", "35B"]}}
]}
```

读回规则（2026-11-20 真机验收见
internal acceptance report；未列边界只做离线 mock 测试）：

- `path` 必须是绝对路径、已存在的普通 `.dwg` 文件，解析后仍在 `C:/CADWorkspace/tianzheng-mcp/outputs` 内；
  静态路径的形状在提交期检查，绝对/存在/是普通文件/解析后仍在范围内这些事实在执行期检查；
  相对路径、静态非 `.dwg`、目录、越界路径都会被拒绝。
- `handles` 是 1..100 个十六进制字符串；只认当前路径里的 ModelSpace 对象，不用 `document_id` 当凭据。
  返回的实体描述里 `document_id` 固定为 `null`，不把任何 job 的 document_id 带出来。
- 只用 worker 已经绑定的那个 AutoCAD 实例：重新核对 PID/HWND，**不自动启动** AutoCAD。
- 先枚举已有文档；目标已经打开就拒绝，不接管、不关闭任何已有文档。已有文档的 FullName 读不到时也拒绝
  （无法证明目标没被打开）。
- 用 `Documents.Open(path, True)` 只读打开，核对返回文档的 FullName 与 `ReadOnly`。数据直接从该文档的
  `ModelSpace` 读，不依赖 `ActiveDocument`。
- 不调用 `Save` / `SaveAs` / `SendCommand`；清理路径只 `Close(False)` 本操作打开且身份已核实的文档。
  身份丢失、Open 是否生效不明或 Close 是否生效不明 → `unknown_outcome`，不自动重试。
- `document.close` 只允许关闭本 job 拥有、当前活动且已保存的图。所有权是 `document.new` /
  `document.reopen` 成功时记住的原始 Add/Open 文档代理（`guard()` 刷新 `doc` 不改变它），close 用
  `_oleobj_.QueryInterface(pythoncom.IID_IUnknown)` 比较当前活动文档与该代理；FullName 相同但 COM
  身份不同（用户关闭后按同路径重开）也拒绝，身份读不到则在 `Close` 之前拒绝。`Document.Saved` 为 false
  （可能有未保存改动）时拒绝。`Close(False)` 成功后才清空内存里的 `doc/owned_doc/path/document_id`。
- 读回、发现与显式关闭的看门狗在超时或模态时仍落 `unknown_outcome` 并 quarantine，但**不发 ESC**
  （这三个步骤都没有前台命令）；原有绘图步骤的超时与 ESC 恢复行为不变。

## 5.2 分页发现 Handle（v1.0.1 P1）

只知道路径、不知道 Handle 时，先用 `document.list` 只读打开同一张已保存的图，按 Handle 数值升序拿一页：

```json
{"steps": [
  {"id": "p1", "op": "document.list",
   "args": {"path": "C:/CADWorkspace/tianzheng-mcp/outputs/job-a.dwg", "limit": 2}}
]}
```

返回形如

```json
{"path": "C:/CADWorkspace/tianzheng-mcp/outputs/job-a.dwg",
 "entities": [{"handle": "34C", "object_name": "AcDbPolyline", "document_id": null, "...": "..."},
              {"handle": "35A", "object_name": "TDbWall", "document_id": null, "...": "..."}],
 "matched_count": 7,
 "next_after_handle": "35A"}
```

`next_after_handle` 就是下一页的游标：本页最后一个 Handle。把它原样（不带 `0x`）传给 `after_handle`，
下一页只返回 Handle 数值**严格更大**的对象 —— 所以 `2 → A → 10` 这种长短不一的 Handle 也是升序，
不会重也不会漏；它是 `null` 表示没有下一页：

```json
{"steps": [
  {"id": "p2", "op": "document.list",
   "args": {"path": "C:/CADWorkspace/tianzheng-mcp/outputs/job-a.dwg", "limit": 2, "after_handle": "35A"}}
]}
```

拿到 Handle 后按 Handle 回读（§5.1）：

```json
{"steps": [
  {"id": "read", "op": "document.readback",
   "args": {"path": "C:/CADWorkspace/tianzheng-mcp/outputs/job-a.dwg", "handles": ["35A"]}}
]}
```

规则与 readback 相同：仅限 `C:/CADWorkspace/tianzheng-mcp/outputs` 内已保存的普通 `.dwg`、只用 worker 已绑定的
实例、目标已打开就拒绝、只 `Close(False)` 本次打开且身份已核实的文档、结果不明落 `unknown_outcome`。
`limit` 默认 100、上限 100，且只对本页对象读取昂贵属性；`matched_count` 是本次扫描到的 ModelSpace
对象总数（不是本页条数）。分页期间不要改这张图或换 DWG 版本，否则同一个游标可能落在不同的对象上。

真机验收（2026-11-20，AutoCAD 2024 + T20，证据见
internal acceptance report）：只凭 DWG 路径，
用 `limit: 3` 连翻三页，依次拿到 `35A 35B 35C`、`35D 364 3D3`、`40D`；7 个对象是 4 面 TDbWall、
1 个矩形 TDbColumn、1 个普通门和 1 个普通窗，门/窗的 `GetKind`/`GetSubKind` 为
`普通门`/`普通门` 与 `普通窗`/`普通窗`。随后按这 7 个 Handle 用 §5.1 回读，属性与发现页一致；
文件 SHA-256 前后不变，关闭后 AutoCAD 无残留文档。负路径（已打开目标、越界、缺失文件、坏参数）
以及 Open/Close 不确定、COM 超时仍只有离线测试；宿主字段未验证（本版不推断宿主），
跨页图档变化检测未验证（见 §7 第 11、12 条）。

## 6. 运营约定

- 状态目录：默认 `tianzheng-mcp/.state`，可用 `TZ_STATE` 指向别处（测试隔离就靠它）。
- 目标实例：`TZ_CAD_PID` 固定要驱动的 CAD 进程；多实例时必须设，否则 worker 拒绝启动。
- `worker once`：跑完当前队列就退出，适合脚本化验证；`worker run`：常驻轮询，等 `stop`。
- 停止闸：`.state/stop` 存在时 worker 起来会**消费并删除**它再退出；生产上那份是人留下的停止标记。
- 生产库与测试库必须分开：测试一律 `TZ_STATE=_tmp/...`，不要指向 `.state`。

## 7. 已知限制

1. 门窗只验证了 `普通门` 与 `普通窗`；防火门/卷帘/人防门等其它类型未验证（见 `docs/OPENING-WINDOW-20261120.md`）。
2. 直线梯 `stair.create`（`TLStair`）仍是历史证据，未真机验证；双跑矩形梯 `stair.rect_create` 与坡屋顶 `roof.create` 均已真机验证。
3. 轴线在本版天正是 `DOTE` 层普通 `LINE`，没有 `TDbAxis` 对象；验收要数线，不是数对象。
4. COM 只连第一个 AutoCAD 实例 → 单实例或用 `TZ_CAD_PID`。
5. 超时/中断后一律 `unknown_outcome`：宁可停下来让人看一眼，也不自动重放。
6. `document.close` / `document.readback` 已在 AutoCAD 2024 + 天正真机验收（2026-11-20，同一 worker）：
   新建-保存-关闭、跨 job 读回 34C 多段线/35A 天正墙、文件哈希不变且两份回读图均关闭、预先打开的目标
   被拒绝且保留打开文档；验收文件见
   internal acceptance report。未保存/外来/身份不可读
   拒绝、路径越界、只读/身份核对失败、同路径重开身份拒绝、Open/Close 不确定及 COM 超时仍只有离线 mock
   覆盖，不宣称真机验证。
7. `document.readback` 只允许 `C:/CADWorkspace/tianzheng-mcp/outputs`；其它已保存 DWG（哪怕是本项目别处的测试图）
   仍需在同一 job 内用 `document.reopen` + `entity.get` 读。
8. 读回与显式关闭的超时/模态不会发 ESC，而是 `unknown_outcome` + quarantine；Open/Close 不确定同样不重试，
   需要人工检查 AutoCAD 文档列表后再 `queue_acknowledge`。
9. `document.close` 的未保存检测依赖 AutoCAD 的 `Document.Saved` 属性；若目标版本 COM 不提供该属性，
   close 会失败而不是冒险关闭。身份核对依赖活代理的 `_oleobj_.QueryInterface(IID_IUnknown)`；读不到身份
   同样拒绝，不退回只比 FullName。`Close(False)` 后同一 worker 的下一个 job 能立即只读打开已在上述真机
   场景通过；其它版本/跨实例未验证。
10. job A 忘记在 job 内 `document.close` 时，它的所有权会在 job B 开始时被清空，后续 job **不能**再替它
    close（`No owned drawing to close`），图会留在 AutoCAD 里；job B 的 `document.readback` 会按「目标已打开」
    拒绝。这种遗留图只能人工关闭。显式 close 必须写在该图所属 job 的配方里。
11. `document.list` 已在 AutoCAD 2024 + T20 真机验收（2026-11-20，证据见
    internal acceptance report）：只凭 DWG 路径，
    `limit: 3` 三页发现 4 墙、1 矩形柱、1 普通门、1 普通窗，门/窗 `GetKind`/`GetSubKind` 照实读回；
    7 个 Handle 回读属性一致、文件哈希不变、关闭后无残留。负路径（已打开目标、越界、缺失文件、坏参数）
    与 Open/Close 不确定、COM 超时仍只有离线覆盖，不宣称真机验证。宿主字段未验证（本版不推断宿主），
    跨页图档变化检测未验证。
12. 分页正确性依赖「同一轮发现里图不变」：`after_handle` 只是 Handle 数值游标，不校验文件哈希或 DWG 版本，
    也不检测跨页图档变化（本版不检测、未真机验证）。枚举本身要读全图 Handle，所以大图的第一页也会扫一遍
    ModelSpace（昂贵属性仍只读本页）。
