# Tianzheng MCP Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task.

**Goal:** 本机天正命令队列，含独立超时/模态 ESC 与持久化对象上下文。
**Architecture:** CLI 和 MCP 生产 SQLite 任务，唯一 worker 在 STA 执行 COM；监控线程独立于 COM 发送定向 ESC。任务上下文保存 JSON 对象引用。
**Tech Stack:** Python 3.12, sqlite3, pywin32, ctypes, MCP Python SDK。
**Spec:** 设计文档（未随仓库发布）。

## Global Constraints
- ProgID AutoCAD.Application.24.3；天正安装 D:/天正；不修改现有 AutoCAD MCP 环境。
- 不模拟键盘绘图；只有恢复路径可以定向投递 ESC。
- 不自动重放不确定的绘图步骤，不启动第二个执行者绕过阻塞调用。

## Review Focus
- 超时后 COM 未结束时不继续队列。
- 句柄失效、跨图纸引用均失败停止。
- 重复操作 ID 与中断重启不重复绘制。
- 无模态的正常步骤不发送 ESC。
- 未解析/未来变量和无效参数在提交时拒绝。

### Task 1: Durable queue and context
Files: tz_mcp/store.py, context.py, tests/test_core.py.
Interfaces: Store.submit(recipe), claim(), finish(); resolve(value,context).
- [ ] 写 FIFO、去重、上下文引用和重启未知状态测试，运行确认失败。
- [ ] 实现 SQLite 队列与 JSON 引用；运行测试通过。

### Task 2: Guarded executor
Files: tz_mcp/engine.py, recovery.py, tests/test_core.py.
Interfaces: Engine.run_one(); recovery.modal(), escape(); adapter.execute(op,args).
- [ ] 写超时、模态、失败停止、上下文传递测试，确认失败。
- [ ] 实现 worker 独占锁、监控与持久化步骤结果；确认通过。

### Task 3: Native adapter and entry points
Files: tz_mcp/cad.py, cli.py, server.py, pyproject.toml, README.md, examples/.
- [ ] 写参数拒绝与命令行序列化测试，确认失败。
- [ ] 实现本机诊断、工作图、墙、门窗、楼梯、屋顶配方和句柄查询；不稳定入口明确标记。
- [ ] 实现 CLI/MCP 公共入口，运行全部 unittest、CLI 集成和 MCP 工具枚举。
- [ ] 真机可用时独立图验收；不可用时准确记录未验证范围。

## Ledger
- Ruling: 当前目录不是 Git 仓库，使用新建独立项目目录，不创建整个 CAD 工作区的 Git 仓库。
- Ruling: 用户已认可架构并要求增加两项具体功能，继续当前会话实现，不重复请求执行授权。
- Ruling: 网络安装构建依赖长时间无响应，终止安装器；新 .venv 通过 .pth 只读引用既有依赖，不修改原环境。代价：现阶段依赖共享，非完全独立。
- Final: fixed document mutation uncertainty with guarded_mutation; opening.create rejects before mutation until native kind/host probes are verified. Context protocol remains usable for other operations.
- Task 1: complete — FIFO, idempotency, persistent context, reference checks.
- Task 2: complete at scheduler level — watchdog, directed ESC channel, unknown-outcome quarantine. Actual CAD modal ESC responsiveness unverified.
- Task 3: CLI and MCP implemented; native CAD acceptance blocked by COM unavailable (0x800401E3). No claim of verified native drawing.
