# Architecture — Proof-First AI Coding Harness

## 1. Philosophy

Deterministic execution, auditable evidence, strict resource management.
One agent, one tool (`run_bash`), one linear-but-prunable history. No
multi-agent complexity — every ounce of engineering effort goes into making
the single loop reliable, cheap, and provable.

## 2. High-Level Diagram

```
┌────────────────────────────────────────────────────────────┐
│ CLI (make run)  →  Textual TUI (Mission Control)            │
└───────────────────────────┬──────────────────────────────────┘
                             ▼
                  ┌─────────────────────┐
                  │     Orchestrator     │  ← state machine, owns the loop
                  └──────┬───────┬──────┘
              ┌──────────┘       └──────────┐
              ▼                             ▼
   ┌─────────────────────┐        ┌──────────────────────┐
   │      LLM Client       │        │   Bash Tool Executor  │
   │ Anthropic API          │◄──────►│ run_bash(cmd)          │
   │ AI_API_KEY from env    │        │ stateless subprocess   │
   │ text-only, single model│        │ .patch + patch utility │
   └─────────────────────┘        └──────────┬───────────┘
                                              ▼
                                   ┌───────────────────────┐
                                   │     Sandbox Layer        │
                                   │ timeout · rlimits          │
                                   │ cwd confinement · min env    │
                                   │ best-effort net isolation      │
                                   └──────────┬───────────┘
                                              ▼
                                   ┌───────────────────────┐
                                   │      Repository         │
                                   │ git-stashed pre-edit     │
                                   └──────────┬───────────┘
                                              ▼
                     ┌────────────────────────────────────────┐
                     │ Verification Layer                       │
                     │ · env scrub  · test runner                │
                     │ · model-authored regression test           │
                     │ · conditional self-critique ensembler        │
                     └──────────────────┬─────────────────────┘
                                        ▼
                     ┌────────────────────────────────────────┐
                     │ Human-in-the-loop Gate                    │
                     │ ASK BEFORE COMMIT  ·  MANUAL ROLLBACK        │
                     └──────────────────┬─────────────────────┘
                                        ▼
                     ┌────────────────────────────────────────┐
                     │ Evidence & Contract                       │
                     │ TASK.md → status.json → OUTCOME.md          │
                     └────────────────────────────────────────┘
```

**Cross-cutting:** efficiency guardrails (iteration cap, log distillation,
incremental context reload, conditional self-critique) wrap every layer —
checked before each model call, not just at the end.

## 3. Component Breakdown

| Component | Responsibility |
|---|---|
| Orchestrator | Owns the state machine: plan → act → verify → confirm → commit/rollback |
| LLM Client | Single text-only model call surface, reads `AI_API_KEY` from env |
| Bash Tool Executor | `run_bash(cmd)` — stateless sandboxed `subprocess.Popen` per call: destructive-command blacklist, repo path confinement, process-group wall-clock timeout, rlimits, output caps |
| Sandbox Layer | Wraps every `run_bash` call: timeout, resource limits, cwd confinement, minimal env, best-effort network isolation |
| Repository | Working tree; git-stashed before every model-initiated edit |
| Verification Layer | Environment scrub → test runner → regression test → conditional self-critique |
| Human-in-the-loop Gate | Confirmation prompt before commit; rollback trigger available anytime |
| Evidence & Contract | `TASK.md`, `status.json`, `OUTCOME.md` written across the lifecycle |
| TUI (Mission Control) | Agent Manager plan view, Activity/Editor pane, Artifacts bar, metrics |

## 4. Control Flow (state machine)

```
INGEST_TASK → PLAN → ACT (bash/.patch) → VERIFY
   VERIFY pass → SELF_CRITIQUE? (conditional) → CONFIRM_GATE → COMMIT → DONE
   VERIFY fail → ROLLBACK → SCRATCHPAD_NOTE → retry (if iter < cap) → PLAN
   any state  → operator MANUAL_ROLLBACK → CHECKPOINT_RESTORE → PLAN
   iter == cap → GRACEFUL_DEGRADE → PARTIAL_REPORT → CONFIRM_GATE
```

Key point: **`CONFIRM_GATE` is a hard stop.** The orchestrator does not
auto-commit on a green test run — it writes the proposed outcome, surfaces
it in the TUI, and waits for operator approval. An `--auto-approve` env
flag exists for unattended evaluator runs (see `rules.md` §7).

## 5. Context & Environment Management

- **Tree-structured context branching:** on tool/test failure, prune back to
  the last stable state and fork with a synthetic scratchpad note
  (e.g. *"Attempt 1 failed: syntax error in test runner"*) instead of
  dragging the failed branch's full output forward.
- **Stateful environment scrubbing:** before every test-runner iteration,
  kill orphan processes and clear temp caches so no leftover state from a
  prior attempt corrupts the current validation cycle.
- **Incremental (changed-files-only) reload:** the orchestrator keeps a
  hash/mtime map of files already in context. On each turn it re-sends only
  files that changed since the last turn — never the whole repo again.
- **Checkpoint & rollback:** `git stash` before every model-initiated edit;
  both automatic (failed verification) and manual (operator-triggered)
  rollback restore this checkpoint.

## 6. Patch-Based Editing Workflow

LLMs reliably mangle quote escaping and indentation on direct file writes.
Instead:

1. Model inspects files via `read_file` — a range-based reader that
   prints line numbers, refuses minified/binary files, and caps output
   size so a 10k-line monolith or a minified JS bundle cannot blow the
   context budget.
2. Model emits a unified diff as the `apply_patch` tool argument. The
   fuzzy applier (`src/patcher.py`) parses it tolerantly (markdown
   fences, stale line numbers, whitespace drift, prefix-less context
   lines) and locates each hunk with staged matching: exact →
   whitespace-normalised → indentation-insensitive → difflib fuzzy
   (ratio >= 0.85). Failures return a concise, machine-parseable
   `PATCH_ERROR:` string naming the file, hunk and best match score, so
   the model can re-read and regenerate instead of burning iterations.
   Application is idempotent; the `patch` utility and `/tmp` scratch
   files are no longer involved.
3. Environment scrub.
4. Test runner.

Self-directed navigation (`grep`, `find`, `cat`) replaces a custom search
tool — one less thing to build, one less thing to break.

## 7. Optimization Layer

| Optimization | Mechanism |
|---|---|
| **Fewer second calls** | Self-critique ensembler is **conditional**, not automatic (see `rules.md` §6.1) — only fires on multi-file diffs, diffs over N lines, or after a prior failed attempt. Trivial single-line patches skip straight to testing. |
| **Log distillation** | Instead of blind "last 100 lines" truncation, a filter extracts only exceptions, failed test names/assertions, non-zero exit codes, and the stack-trace tail before feeding output back to the model. |
| **Incremental context reload** | Only changed files are re-sent per turn (see §5). |
| **Iteration cap** | Hard stop at 6–8 turns; graceful degradation writes a partial report instead of looping silently. |

## 8. Efficiency Guardrails

| Guardrail | Mechanism | Protects |
|---|---|---|
| Iteration cap | Hard stop after N loop turns | Token/cost runaway |
| Conditional self-critique | Skips the ensembler call on trivial patches | Wasted LLM calls |
| Log distillation | Signal-only extraction, not raw truncation | Context bloat, noisy signal |
| Incremental context | Reload only changed files | Redundant tokens |
| Checkpoint/rollback (auto + manual) | git stash before edit; revert on failure or operator request | Repo left broken |
| Environment scrubbing | Kill orphans + clear caches before each test run | Cross-attempt corruption |
| Context branching | Prune to last stable state on failure | Context bloat from dead ends |
| Ask-before-commit | Hard confirmation gate | Unreviewed changes landing |
| Sandbox isolation | Timeout, rlimits, cwd confinement, minimal env, best-effort net isolation on every `run_bash` call | Runaway/destructive commands, credential leakage into subprocess env |
| Graceful degradation | Best-effort commit + status note if budget runs out | Silent failure |

## 9. Metrics (live in TUI)

Tokens used · LLM call count (incl. self-critique calls saved) · iterations
(current/cap) · elapsed time · test pass rate · estimated cost · rollback
count (auto vs manual).

## 10. Production Hardening Checklist

- `AI_API_KEY` env-only, never committed.
- `make setup` installs into an isolated virtual environment.
- `make clean` removes `status.json` and temp `.patch` files.
- Stateless subprocess calls isolate failures per action.
- No commit lands without passing the confirmation gate.
- Every run — success, failure, or degraded — ends with a written
  `OUTCOME.md`.
