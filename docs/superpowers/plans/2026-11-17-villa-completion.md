# Villa Completion Implementation Plan

> **For agentic workers:** Use superpowers:executing-plans to implement task by task. Do not run simultaneous CAD actors.

**Goal:** Deliver a new editable Tianzheng villa DWG with doors/windows, an internal stair, columns and floor slabs.
**Architecture:** Extend the existing validated structured CLI/MCP queue; native commands create entities and COM reads/writes verified properties. Diagnostic probes operate only in their own drawings.
**Tech Stack:** Python, pywin32, FastMCP, SQLite, AutoCAD2024/T20 V9.
**Spec:** docs/superpowers/specs/2026-11-17-villa-completion-design.md

## Global Constraints

- Preserve previous DWGs; no direct native DXF fabrication; no automatic CAD launch.
- Keep timeout/modal recovery and unknown-result quarantine.
- Record native types, exact handles, dimensions, host evidence and reopen results.

## Tasks

- [ ] Probe native doors/windows, rectangular stairs, columns and slabs including holes; document observed prompts/properties.
- [ ] Add failing tests for supported argument contracts and verification; add minimal adapters/operations; run relevant tests.
- [ ] Build a new villa recipe with room circulation, compact stair placement, openings, aligned column locations and floor openings.
- [ ] Execute through real stdio MCP; inspect geometry and persist checkpoints; stop and inspect unknown outcomes.
- [ ] Save/reopen, verify object properties/host evidence/slab openings/stair fit; inspect a plan view; write deliverables and limitations.

## Execution ledger

- Native ordinary windows: created; width/height/sill and referenced-wall hole delta verified. User selected window panel mode.
- Rectangular stairs: TRStair creates TDbRectStair. Height3000/total18/tread260/widths1200 read back; compact placement ready.
- Slabs: TSlab outer and hole closed polylines consumed;120mm thickness verified. Independent hole visual/geometry still pending.
- Columns: raw bulk SendCommand (2D/3D/nested variants) produced zero. Native seed import succeeded, but second in-document Copy blocked and quarantined queue job144ad30f-3a07-49f8-af24-dba25de4f9b5. Do not replay.
- User explicitly authorized foreground keyboard input pattern. Built PID/view focus checks and per-character checks; interrupted staged commands request recovery. Hardware probe ready, awaiting user CAD restart because old session's COM remains unavailable although UI looks idle.
- Added wall.find to resolve current fragments. Added ordinary text and polyline operations for labels/railing.
- 55 unit tests pass. Stage A recipe142steps validated at <项目根>/别墅完善_20261117-200220/stage-a.json. No final villa revision has been executed yet.
