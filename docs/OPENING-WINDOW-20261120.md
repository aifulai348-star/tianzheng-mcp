# 普通窗真机验收（2026-11-20）

本文记录 `opening.create` 支持「普通窗」的真机验收证据。工作实例是测试专用 CAD
（pid 28936 / 主窗 788990，`D:/天正/TGStart.exe` 拉起），生产队列与 `.state` 全程未触碰。

## 一、为什么换命令：门与窗是两个插入器

前一轮（`docs/LIVE-PROBE-20261118.md`）记录过一条限制：`TOpening` 的**门窗参数**面板类型
列表里只有 8 种门，没有窗。本轮把这条限制查到底：

| 途径 | 结果 |
| --- | --- |
| `TOpening` 面板的 `类型` ComboBox | 门模式时列 8 个门类型，窗模式时列 `普通窗 / 防火窗`。**列表跟着面板当前模式走**，无法从门模式跳到窗模式 |
| `类型` 按钮 → 编号设置 | 只改编号类别（门/窗/弧窗…），不影响插入类型 |
| `TOpTem`（门窗原型） | 提示语是 `TOPTEM 选择图中的门窗:`，要求先选图中已有门窗当原型，没有现成窗就用不了 |
| `TOpeningLib` | 只出「常用门窗库」，没有类型列表控件 |
| 双击面板预览 → 天正图库管理系统 | 能打开，但 `EnumChildWindows / GetWindowText` 跨进程会挂死（`_tmp/w13b.py` 120 秒无输出），UIA 也只读到空的树/列表；不可自动化 |
| ARX 字符串 | `TCH_UIPanelWindowAndDoor.arx` 里有 `TGDOOR` 与 `TGWINDOW`，且只有 `普通门` 字面量，**没有 `普通窗`** —— 类型名来自图库，不是硬编码 |

结论：**插入器是 `TGDOOR` 与 `TGWINDOW` 两条命令，各自开一块无标题调色板**（`#32770`，
rect (80,120)-(255,583)），而不是共用的「门窗参数」面板。实测命令行提示
`点取门窗插入位置(shift-左右开,Ctrl-上下开)<退出>:`。

顺带查清了原来那条混跑失败的根因：`TOpening` 的「门窗参数」是常驻面板，会记住上次的
模式。2026-11-20 11:11 的 job `194822b2-9bf1-4587-8ec4-0a3ddca2c5bc` 里，先插窗再插门，
门这一步直接失败并记 `unknown_outcome`：

```
Opening panel is set to 普通窗, not 普通门
```

门与窗改走各自的命令后，这个状态残留问题不再存在。

## 二、调色板结构（`_tmp/w50_rows.py`）

两种模式的调色板布局完全一致，只是类型下拉与行标签不同：

| 位置 | 控件 | 含义 |
| --- | --- | --- |
| (188,457) ComboBox + (191,460) Edit | 编号（默认空） | — |
| **(188,479) ComboBox** | `普通门` / `普通窗` … | **类型**，`opening_kind()` 读它 |
| (188,501) ComboBox | `木复合` | 材料 |
| (118,373) `#32770` → Edit (186,377,188,390) | `900` / `1200` | 门洞宽 / 窗宽 |
| (118,395) `#32770` → Edit (186,399,188,412) | `2100` / `1600` | 门洞高 / 窗高 |
| (118,417) `#32770` → Edit (186,421,188,434) | `0` / `600` | 门槛高 / 窗台高 |
| (90,527)–(249,578) Button 网格 | 窗类型图标 | 未使用（默认即是普通窗） |
| (118,351) `#32770` 两个 30px Edit | 空 | **仅门模式**的开向行，不属于尺寸行 |

行标签是自绘的，读不到文字，所以**按自上而下的顺序寻址**。可写的 Edit 被压成 **2px 宽**，
这正是与开向行（两个 30px Edit）的区分点：`value_row_edit()` 只收宽度 ≤ 6px 的 Edit，
否则门模式下每个索引都会错一位（第一次实测就是这样把 `900` 写进了编号行）。

## 三、生产代码改动（`tz_mcp/panel.py`）

1. 新增常量 `WINDOW_TYPES = ('普通窗',)`、`WINDOW_FIELDS = (('窗宽','width'),('窗高','height'),('窗台高','sill'))`。
2. `window_row_edit` → **`value_row_edit(u, panel, index)`**：按子对话框 top 排序取第 index 行，只认 2px Edit。
3. `_place_opening` 门/窗统一：`command = 'TGWINDOW' if window else 'TGDOOR'`，先 ESC 清残留调色板，
   再向绘图视口**逐字键入命令**（`PostCommand` 开不出这两块调色板），随后等任意可见面板。
4. 点击前若目标点不在可见范围内，先用 `frame_wall` 框一次视图再读 VIEWCTR/VIEWSIZE ——
   调色板一开 COM 就阻塞，视图只能在命令键入前调整。
5. 类型不符时仍抛 `OutcomeUnknown('Opening panel is set to %s, not %s')` 并 ESC，不盲点。

`parts.py` 里 `opening.create` 的建后校验不变：`GetKind/GetSubKind` 必须与请求一致，
DXF 的宽/高/插入点/宿主墙要过，宿主墙洞面积增量要吻合，超出墙高则判 `unknown_outcome`。

## 四、单步真机证据

| 时间 | 动作 | 结果 |
| --- | --- | --- |
| 11:05 | `TGWINDOW` + 直接点墙（不改字段） | `3A0`：`GetKind=GetSubKind=普通窗`、1800×1500（调色板默认值） |
| 11:06 | 写三行 1200/1600/600 后点墙 | `3A1`：1200×1600、`WinSill=600`、DXF `insertion (24000,20000)`、`host 35A`、`layer WINDOW` |
| 11:12 | 直调 `c.execute('opening.create', 普通窗)` | `3A0`（另一张图）：1200×1600/sill 600，洞面积 0.00→1.92，7.4 秒 |
| 11:17 | `TGDOOR` 打开调色板 | 三行 `900/2100/0`，类型 `普通门` —— 与窗调色板同构 |

## 五、端到端验收（真实 MCP 面）

驱动 `_tmp/win_e2e.py`：`queue_submit` 提交配方 → `bootstrap.py worker once`（`TZ_STATE=_tmp/win_e2e/state`）→
`queue_status` 回读。同一份配方里**门和窗都有**，专门用来证明混跑不再互相污染。

配方：`document.new → wall.create(20000,20000→28000,20000, 120/120) → 普通门 900×2100 @21000 →
普通窗 1200×1600 sill 600 @25000 → document.save → document.reopen`。

job `b636cee2-bba7-47ce-a578-9ba3fe83d3ad`（11:16:52 提交，11:17:13 结束，约 21 秒）：

| 步 | 状态 | 结果 |
| --- | --- | --- |
| document.new | succeeded | `_tmp/win_e2e/win-20261120-111652.dwg` |
| wall.create | succeeded | handle `35A` |
| opening.create（普通门） | succeeded | handle **`3CF`**，900×2100，`WinSill=0`，`GetKind=GetSubKind=普通门` |
| opening.create（普通窗） | succeeded | handle **`409`**，1200×1600，`WinSill=600`，`GetKind=GetSubKind=普通窗` |
| document.save | succeeded | — |
| document.reopen | succeeded | 两个 handle 都在，类型/尺寸/窗台高一致 |

关键校验（来自 job 结果的 `evidence`）：

| 项 | 门 `3CF` | 窗 `409` |
| --- | --- | --- |
| DXF 类型 | `TCH_OPENING`（layer `WINDOW`） | `TCH_OPENING`（layer `WINDOW`） |
| DXF 宽×高 | 900 × 2100 | 1200 × 1600 |
| DXF 插入点 | `(21001.7, 20000.0)` | `(25004.6, 20000.0)` |
| DXF 宿主（第二个 330） | `35A` | `35A` |
| 宿主墙洞面积 | 0.00 → **1.89**（=0.9×2.1） | 1.89 → **3.81**（+1.92 = 1.2×1.6） |
| 宿主关联判定 | `verified_by_wall_delta_and_position` | 同左 |

插入点与请求点差 1.7 / 4.6 个单位：点击按整像素下发，当前视图 3.749 单位/像素，落在
`verify_opening_dxf` 的像素容差内；因此校验比的是**实际点击落点的世界坐标**，不是请求坐标。

同一时刻 CAD 里还开着 4 张别的图（drawing-101154 / drawing-101219 / e2e-20261120-102652 /
win-43915）。任务只在新图上生效，其它图未被写入 —— 多图纸焦点选择走的是
`input_bridge.activate_view` 的「焦点所在文档框架里的视图」规则。

## 六、回归

- 单测：`cd '<repo>'; D:/python/python.exe -X utf8 _tmp/run_tests.py` →
  **116 项全绿**（新增 `ValueRowTests.test_rows_are_addressed_top_to_bottom`，含「开向行会被忽略」
  的反例；门的用例改成断言键入 `TGDOOR` 且不再发 `TOpening`）。
- 旧成功路径原样复跑（`_tmp/e2e2.py`，只含门，11:19:44 起）：六步全绿 ——
  `document.new`（`e2e-20261120-111944.dwg`）→ 墙 `35A` → 柱 `35B` → 门 `3D0`（900×2100）→
  `save` → `reopen`（三个对象都在）。失败矩阵四项同轮复绿：`r5a` 提交期拒、`r5b` `cancelled`、
  `r5c` `unknown_outcome`（`reason: timeout`）、`r5d` acknowledge 后 `failed` 且不重放。
- 生产现场未动：`.state` 29 文件、169 条队列（cancelled 120 / failed 11 / succeeded 37 /
  unknown_outcome 1，`144ad30f` 仍未 acknowledge）。

## 七、仍未验证

1. 窗的其它类型（`防火窗` 及调色板底部的图库图标）未验证，只用默认 `普通窗`。
2. 窗台高只验证了被 COM/DXF 读回，未验证它与墙高的合法性（当前只查「高+台高 ≤ 墙高」）。
3. 门的其它 7 种类型（防火门/卷帘/人防门…）未逐项验证，只验了 `普通门`。
4. `TOpening` 的「门窗参数」面板仍可被人工命令打开；自动化不再使用它。
