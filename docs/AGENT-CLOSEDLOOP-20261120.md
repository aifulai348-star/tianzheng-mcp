# Agent 自主绘图闭环验收（2026-11-20）

目标（用户 m04503 P1）：**不许预先写死配方再机械调用**。让 Agent 先读 `list_capabilities`，
自己规划「矩形房间：四面墙 + 一根矩形柱 + 一扇普通门 + 一扇普通窗，门窗挂对应墙，保存并检查类型与几何」，
重点验 `$ref` 引用墙。本文记录这次规划、两次被提交期校验拦下、以及最终一次跑通的结果。

## 一、规划是从 capabilities 推出来的

`list_capabilities` 返回 15 条 op，每条只有 `required` / `optional` / `validation`。用到的那几条：

| op | required | optional | 我从这里得到的约束 |
| --- | --- | --- | --- |
| `document.new` | `path` | — | 一个 job 必须以它开头 |
| `wall.create` | `start`, `end` | `left_width`, `right_width` | **墙没有 height 参数**，高度是默认 3000 |
| `column.create` | `point`, `width`, `depth`, `height` | — | 截面是宽×深，插入点单独给 |
| `opening.create` | `host`, `kind`, `point`, `subkind` | `width`, `height`, `sill` | `host` 是对象引用，正好用 `$ref` |
| `document.save` / `document.reopen` | — | — | 保存后能重开核对 |

驱动脚本 `_tmp/room_agent.py` 里有一行是这次验收的核心：提交前**逐条**拿配方去比对 capabilities，
`required` 缺一个就 `assert` 失败。也就是说配方不是照抄某个模板，而是必须对得上声明。

## 二、被提交期校验拦下的两次（都是好拦）

1. `queue_submit` 的参数形状：`Error ... recipe  Field required` —— 配方要包在 `recipe` 字段里。
2. `$ref` 找不到上游：`Forward or missing reference: w1.handles.0` —— 我按 `step.id` 引用，
   但校验只认 `bind` 声明的名字（`tz_mcp/store.py` 里 `names` 收的是 `bind`）。加 `bind` 后通过。
3. 同一轮里还踩到 id 字符集：`get-w1` → `Unique identifier required for every step`，
   连字符不在 `[A-Za-z][A-Za-z0-9_]*` 里，改成 `get_w1`。

这三条都已补进 `docs/MCP-TOOL-SPEC.md` §5。

## 三、配方

房间：A(30000,30000) B(36000,30000) C(36000,34500) D(30000,34500)，6000×4500，墙厚 120。
柱子在房间中部 (33000,32000)，600×600×3000。门在 w1（南墙）(31500,30000)，900×2100。
窗在 w3（北墙）(33000,34500)，1200×1500，台高 900。

```json
[
 {"id":"doc",   "op":"document.new", "args":{"path":".../room2.dwg"}},
 {"id":"w1",    "op":"wall.create",  "args":{"start":[30000,30000],"end":[36000,30000],"left_width":120,"right_width":120}, "bind":"w1"},
 {"id":"w2",    "op":"wall.create",  "args":{"start":[36000,30000],"end":[36000,34500],"left_width":120,"right_width":120}},
 {"id":"w3",    "op":"wall.create",  "args":{"start":[36000,34500],"end":[30000,34500],"left_width":120,"right_width":120}, "bind":"w3"},
 {"id":"w4",    "op":"wall.create",  "args":{"start":[30000,34500],"end":[30000,30000],"left_width":120,"right_width":120}},
 {"id":"column","op":"column.create","args":{"point":[33000,32000],"width":600,"depth":600,"height":3000}, "bind":"column"},
 {"id":"door",  "op":"opening.create","args":{"host":{"handle":{"$ref":"w1.handles.0"},"document_id":{"$ref":"w1.document_id"}},
                                            "kind":"普通门","subkind":"普通门","point":[31500,30000],"width":900,"height":2100}, "bind":"door"},
 {"id":"window","op":"opening.create","args":{"host":{"handle":{"$ref":"w3.handles.0"},"document_id":{"$ref":"w3.document_id"}},
                                            "kind":"普通窗","subkind":"普通窗","point":[33000,34500],"width":1200,"height":1500,"sill":900}, "bind":"window"},
 {"id":"save",  "op":"document.save","args":{}},
 {"id":"reopen","op":"document.reopen","args":{}},
 {"id":"get_w1",     "op":"entity.get", "args":{"handle":{"$ref":"w1.handles.0"}}},
 {"id":"get_w3",     "op":"entity.get", "args":{"handle":{"$ref":"w3.handles.0"}}},
 {"id":"get_column", "op":"entity.get", "args":{"handle":{"$ref":"column.handles.0"}}},
 {"id":"get_door",   "op":"entity.get", "args":{"handle":{"$ref":"door.handles.0"}}},
 {"id":"get_window", "op":"entity.get", "args":{"handle":{"$ref":"window.handles.0"}}}
]
```

## 四、结果

job `a418cad0-fecb-468e-92c5-f3173de4c802`，11:25:33 提交，11:26:06 结束（约 33 秒），**15 步全部 succeeded**。
产物 `_tmp/room/room2.dwg`；证据 `_tmp/room/room2.json`、`_tmp/room/report.json`。

| 对象 | handle | 读回 | 包围盒 |
| --- | --- | --- | --- |
| 南墙 w1 | `35A` | `LeftWidth/RightWidth 120`、`Height 3000` | x 30000..36000，y 29880..30120 |
| 东墙 w2 | `35B` | 同 | x 35880..36120，y 30000..34500 |
| 北墙 w3 | `35C` | 同 | x 30000..36000，y 34380..34620 |
| 西墙 w4 | `35D` | 同 | x 29880..30120，y 30000..34500 |
| 柱 | `364` | `Width 600`、`Deep 600`、`Height 3000` | 32705..33305 × 31703..32303 |
| 门 | `3D3` | `GetKind=GetSubKind=普通门`、900×2100 | 31052..31952 |
| 窗 | `40D` | `GetKind=GetSubKind=普通窗`、1200×1500、`WinSill 900` | 32405..33605 |

几何核对：

- **闭合**：四道墙的包围盒首尾相接，轴线正好落在 30000 / 36000 × 30000 / 34500，缺口 0。
- **门窗挂对墙**：门 `host_reference.handle = 35A`（= w1），窗 `= 35C`（= w3），与配方里的
  `$ref` 完全一致 —— 这就是本次要验的「引用墙」；DXF 里第二个 `330` 也是 `35A` / `35C`。
- **墙洞面积**：门 0 → 1.89（0.9×2.1），窗 0 → 1.80（1.2×1.5），都是按各自墙的增量算的。
- **保存重开**：`document.reopen` 回来正好 7 个对象 —— `35A 35B 35C 35D` 加 `364 3D3 40D`，类型不变。
- **偏移**：柱插入点 `(33005.5, 32002.9)`、门 `(31502.7, 30000)`、窗 `(33005.5, 34500)`，
  与请求差 2–5 个单位，等于当前视图一次点击的量化（4.94 单位/像素），在校验容差内。
- 窗的 `rotation` 是 `3.14159`：北墙是反向画的，天正按墙方向放窗，几何仍然落在墙带内。

## 五、这次暴露的一条真实限制

验收步必须写进同一份配方。我最初把 `entity.get` 拆成第二个 job，worker 直接落 `failed`：
`No owned active drawing; run document.new first` —— `Cad.guard()` 发现 `self.doc is None`。
每个 worker 进程都从「没有图纸」开始，**没法 attach 到上一次保存的 dwg**，`document.reopen` 也需要
同 job 里先有 `document.new` 定下的 `path`。所以「建完再验」不是一个独立任务，而是同一份配方里的收尾步。
这一点已写进 `docs/MCP-TOOL-SPEC.md` §5。

## 六、边界

- 只有这一间房、一种布局；没有做「随机布局」「读图改图」。
- 柱是矩形柱；异形柱、楼梯、坡屋顶未验证。
- 门窗仍是 `普通门` / `普通窗` 两种。
- 规划由 Agent 现场完成，但**点位是 Agent 自己算的**，不是从图纸读出来的；距离真正的「看图绘图」还有一步。
