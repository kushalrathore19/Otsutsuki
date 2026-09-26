# Design — Mission Control TUI (Textual)

Inspired by Google Antigravity's Agent Manager / Editor / Artifacts split,
rebuilt terminal-native. The core idea we're borrowing: **the plan and the
proof are always visible, not buried in scrollback.**

## 1. Layout

```
┌─ AI HARNESS — MISSION CONTROL ──────────────────────────────────────────┐
│ Task: Fix null pointer in auth middleware        Status: ● RUNNING       │
├───────────────────────────┬───────────────────────────────────────────┤
│ AGENT MANAGER (left, 30%) │ ACTIVITY / EDITOR (right, 70%)             │
│                           │                                            │
│ ▸ Plan                    │ [Tabs: Live Log | Diff | Test Output]     │
│  1. ✓ Read issue          │                                            │
│  2. ✓ Locate auth.py      │ $ grep -rn "user.token" src/               │
│  3. ⟳ Write patch          │ src/auth.py:42: if user.token is None:    │
│  4. ○ Run tests            │                                            │
│  5. ○ Self-critique (skip*)│ Applying patch...                         │
│  6. ○ Confirm & commit     │ ✓ patch applied cleanly                   │
│                           │                                            │
│ Iteration: 3 / 8          │ * self-critique auto-skipped: trivial diff │
│ ████████░░░░░░ 42%        │                                            │
│                           │                                            │
│ [R] Rollback   [Q] Quit   │                                            │
├───────────────────────────┴───────────────────────────────────────────┤
│ ARTIFACTS: [TASK.md] [status.json] [OUTCOME.md]  4.2k tok · 3 calls · 38s │
└──────────────────────────────────────────────────────────────────────┘
```

## 2. Confirmation gate (modal, blocks progress)

```
┌─ CONFIRM COMMIT ──────────────────────────────────────────┐
│ Tests: 6/6 passed · Regression test: PASSED                │
│ Diff: 1 file changed, 4 insertions(+), 1 deletion(-)         │
│                                                              │
│   [ view full diff ]   [ view OUTCOME.md ]                   │
│                                                              │
│              (Y) Commit        (N) Rollback                  │
└──────────────────────────────────────────────────────────┘
```
This modal is the literal implementation of "ask before committing." It
does not auto-dismiss. In `AUTO_APPROVE=1` mode it is skipped and logged
in `status.json` as an auto-approved transition, so the evidence trail
still shows it happened.

## 3. Widget → data mapping

| Panel | Textual widget | Driven by |
|---|---|---|
| Header (task + status dot) | `Header` custom, reactive `status` | Orchestrator state |
| Plan list | `ListView` with icons (✓ ⟳ ○) | Live plan steps; annotates when a step (e.g. self-critique) is auto-skipped and why |
| Progress bar | `ProgressBar` | `current_iteration / cap` |
| Activity tabs | `TabbedContent` + `RichLog` (Live Log) / `Syntax` (Diff) / `RichLog` (Test Output) | Distilled log output (not raw), `.patch` content, test results |
| Rollback control | `Button` / keybinding `R` | Triggers `orchestrator.manual_rollback()` at any time, independent of auto-rollback |
| Confirm modal | `ModalScreen` | Shown only when orchestrator reaches `CONFIRM_GATE` |
| Artifacts bar | Buttons swapping right pane to `Markdown` widget | Reads `TASK.md` / `OUTCOME.md` off disk directly; `status.json` shown pretty-printed |
| Metrics footer | `Static`, reactive, timer-refreshed | Tokens, LLM call count, elapsed time |

## 4. State → color convention

| State | Color |
|---|---|
| Running / in progress | Amber |
| Verified / passed / committed | Green |
| Failed / rolled back | Red |
| Pending / not yet reached | Gray |
| Auto-skipped (optimization) | Dim / muted text with a footnote |

## 5. Why these choices

- **Plan always visible** — the operator sees intent before action, not
  just a stream of completed steps.
- **Diff rendered, not dumped** — `Syntax`-highlighted patch content is the
  reviewable artifact a judge or operator actually reads.
- **Rollback is a first-class control**, not a hidden recovery path — it
  sits next to Quit in the left panel, always reachable.
- **The confirm modal is unmissable** — it's the one place in the UI that
  blocks, by design, because it's the one decision that shouldn't be
  automatic.
