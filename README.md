# Otsutsuki — Proof-First AI Coding Harness

An autonomous, single-agent coding harness built for the **AI Harness Hackathon 2026**. It takes a standardized, text-only foundation model and turns it into a software engineer that can navigate a repository, write and apply patches, run tests, recover from failures, and prove — not just claim — that a task is done.

Design philosophy: **deterministic execution, auditable evidence, strict resource management.** No hidden state, no unverifiable claims, no silent failures.

---

## Table of Contents

- [Quick Start](#quick-start)
- [Configuration](#configuration)
- [Using the Harness](#using-the-harness)
- [Features](#features)
- [Architecture](#architecture)
- [Evidence & Artifacts](#evidence--artifacts)
- [Multi-Agent Mode](#multi-agent-mode)
- [Security & Sandboxing](#security--sandboxing)
- [Testing & CI](#testing--ci)
- [Hackathon Compliance](#hackathon-compliance)
- [Project Structure](#project-structure)
- [Known Limitations](#known-limitations)
- [Further Reading](#further-reading)

---

## Quick Start

```bash
git clone <this-repo>
cd Otsutsuki
export AI_API_KEY="your-api-key"
make setup
make run
```

`make run` launches a chat-style terminal interface. Type a task and press **Enter** to send it — the agent starts working immediately.

For a fully non-interactive run (used by the hackathon evaluator):

```bash
export AI_API_KEY="<provided-key>"
export AUTO_APPROVE=1
export TASK="Fix the null pointer exception in the auth middleware"
make setup
make run
```

---

## Configuration

All configuration is via environment variables — nothing is hardcoded in source. Set these directly, or drop them in a `.env` file at the repo root (see `.env.example`).

| Variable | Default | Description |
|---|---|---|
| `AI_API_KEY` | *(required)* | Model API credential. Never committed, never logged, stripped from every sandboxed subprocess's environment. |
| `AI_MODEL` | `openai/gpt-oss-20b` | Model identifier, swappable without touching source code. |
| `AI_BASE_URL` | `https://api.groq.com/openai/v1` | OpenAI-compatible API endpoint. |
| `TASK` | *(none)* | Task description or GitHub issue text, used when `AUTO_APPROVE=1` or as a pre-fill in interactive mode. |
| `AUTO_APPROVE` | `0` | Set to `1` for non-interactive evaluation runs: skips all confirmation prompts, runs exactly one task, writes evidence, exits. |
| `MULTI_AGENT` | `0` | Set to `1` to enable the Architect → Implementer → Verifier pipeline (see [Multi-Agent Mode](#multi-agent-mode)). Default is the single-agent loop. |
| `ITERATION_CAP` | `8` | Hard cap on agent loop iterations per task/subtask before graceful degradation. |
| `TIMEOUT_SECONDS` | `30` | Per-command sandbox timeout. |
| `MEM_LIMIT_MB` | `1024` | Per-command memory limit (best-effort, via `resource.setrlimit`). |
| `TOKEN_BUDGET` | `50000` | Soft token budget per run; crossing thresholds shifts agent behavior (see below). |
| `SURGICAL_FRACTION` | `0.7` | At this fraction of `TOKEN_BUDGET` used, the agent is told to stop exploring and make the smallest viable change. |
| `FINALIZE_FRACTION` | `0.9` | At this fraction, the agent is told to finalize immediately — no new work. |
| `CREATE_PR` | `0` | Set to `1` to attempt `gh pr create` after a successful commit (requires `gh` installed and authenticated; silently skipped otherwise). |

An optional `harness.yaml` at the repo root can set `model`/`base_url` with `${VAR}` interpolation instead of raw env vars, if present.

**`.env.example`** ships with variable names only, no values — never commit a real key.

---

## Using the Harness

Launching `make run` (without `AUTO_APPROVE`) drops you into a single-screen chat interface: a scrolling transcript and one input box at the bottom. No separate screens, no popups.

**Sending a task:** type it and press Enter. Paste a GitHub issue's text directly if you have one.

**Mid-run:** typing anything while the agent is working is treated as a **nudge** — injected into the agent's context on its next turn, useful for steering it away from a bad approach without waiting for a full failure/rollback cycle.

**Confirmation gate:** before committing any change, the harness pauses and asks for approval inline in the chat. Reply `yes` to commit, `no` to roll back. (Skipped automatically when `AUTO_APPROVE=1`.)

**Slash commands**, typed into the same input box:

| Command | Effect |
|---|---|
| `/rollback` | Manually roll back to the last checkpoint, independent of automatic failure-triggered rollback. |
| `/new` | Start a fresh task (only when idle). The previous run's evidence is archived, not overwritten. |
| `/history` | List past runs with their outcome and iteration count. |
| `/quit` | Exit the harness. |

---

## Features

**Orchestration**
- Single-agent state machine: plan → act → verify → confirm → commit/rollback.
- Pre-flight critique: before starting, a cheap model pass checks whether the task implies a needlessly complex approach and can suggest a simpler alternative (accept or override, interactively).
- Escalating retry strategy: a 2nd consecutive failure on the same issue gets different guidance than the 1st ("try a different approach," not just "fix the error"); a 3rd failure triggers a fresh strategic reassessment.
- Budget-aware behavior: as token usage crosses `SURGICAL_FRACTION` / `FINALIZE_FRACTION`, the agent is told to narrow scope automatically.

**Context management**
- Static localization pass before the agent loop starts: extracts key terms from the task, greps the repo, and uses Python's `ast` module to identify which function/class contains each match — handing the model a ranked list of likely-relevant files instead of a blind starting point.
- Tree-structured context branching: on failure, the conversation is pruned back to the last stable state with a single scratchpad note, rather than accumulating every failed attempt's full transcript.
- Context hard-reset after repeated same-type failures: rebuilds context from just the task, localization summary, and a consolidated lessons-learned note.

**Tools & verification**
- Exactly one tool exposed to the model: `run_bash`. No custom file-edit or search tool — the model uses `grep`/`find`/`cat -n` itself.
- All code changes are unified diffs written to `/tmp/patch.diff` and applied via the `patch` utility — never inline file overwrites (avoids LLM quote/indentation mangling).
- `python -m py_compile` syntax pre-check on changed `.py` files before running the full test suite — catches a broken patch in milliseconds instead of burning a test cycle.
- The model must author and run a standalone regression test (via `pytest`) targeting the specific issue before claiming completion — verification by test exit code, not by `cat`/`print` inspection.
- Conditional post-patch self-critique: a second, low-temperature model pass reviews the diff against the task, but only fires on multi-file diffs, diffs over 15 changed lines, or retries — trivial single-file patches skip the extra call.
- Non-blocking reviewer pass: after a successful commit, one more pass leaves plain-text code-quality notes in `OUTCOME.md` — never blocks or triggers a rollback.

**Recovery & efficiency**
- Checkpoint via `git commit` before every modifying command; automatic rollback on failed verification, manual rollback available anytime via `/rollback`.
- Environment scrubbing (kill orphan processes, clear temp caches) before every test run.
- Log distillation: subprocess output fed back to the model is filtered to exceptions, failed assertions, and exit codes — not raw, full logs.
- Graceful degradation: hitting the iteration cap produces a partial status report, never a silent failure or infinite loop.
- Append-only audit trail (`events.jsonl`) alongside the current-state `status.json` snapshot.

---

## Architecture

```
Chat TUI (Textual) ── transcript + input, live metrics panel
        │
        ▼
   Orchestrator (state machine)
        │
   ┌────┼─────────────┬───────────────┐
   ▼    ▼             ▼               ▼
LLM    Bash Tool    Sandbox Layer   Repository
Client Executor     (timeout,       (git-checkpointed
                     rlimits,        before every edit)
                     cwd confinement)
        │
        ▼
Verification: py_compile → pytest → conditional self-critique
        │
        ▼
Human-in-the-loop: confirm-before-commit / manual rollback
        │
        ▼
Evidence: TASK.md → status.json → OUTCOME.md → events.jsonl
```

Full component-level detail, control-flow state diagrams, and design rationale live in [`architecture.md`](./architecture.md).

---

## Evidence & Artifacts

Every run — success, failure, or degraded — writes:

| File | Contents |
|---|---|
| `TASK.md` | The task as given, unmodified. |
| `status.json` | Live state: iteration count, tokens, commands run, rollback count, critique fire/skip events, current mode. |
| `OUTCOME.md` | Final report: status, iterations used, rollback count, full command history, reviewer notes. |
| `events.jsonl` | Append-only audit log of every significant event (task start, checkpoint, rollback, critique decisions, confirm-gate outcome, run completion). |
| `runs/<timestamp>/` | Archived evidence from previous runs in interactive/session mode — nothing is silently overwritten. |
| `PLAN.md` | (Multi-agent mode only) the Architect's subtask breakdown and live status. |

This is deliberate: anyone — a judge, a teammate, future-you — should be able to reconstruct exactly what happened in a run from disk alone, without re-reading scrollback.

---

## Multi-Agent Mode

Set `MULTI_AGENT=1` to enable a three-role pipeline instead of the single-agent loop:

- **Architect** — one call, breaks the task into an ordered list of concrete subtasks with dependencies and expected touched files.
- **Implementer** — the same single-agent loop described above, run once per subtask.
- **Verifier** — after all subtasks complete, reviews the *full* accumulated diff against the *entire* original task and can send a specific failed subtask back for a bounded number of retries.

Independent subtasks (no shared files, no dependency relationship) run in parallel using isolated `git worktree`s, merged back one at a time; a merge conflict falls back to a sequential retry against the updated base rather than failing the run.

`MULTI_AGENT=0` (the default) is the safety net — a fully working single-agent loop that behaves identically whether or not the multi-agent code exists.

---

## Security & Sandboxing

- `AI_API_KEY` is read from the environment only, stripped from every sandboxed subprocess's own environment, and never written to any evidence file or log.
- Every `run_bash` call runs with a wall-clock timeout and best-effort memory/process-count limits (`resource.setrlimit`), applied non-fatally — a resource-limit failure never blocks legitimate command execution.
- The working tree is checkpointed (`git commit`) before any modifying command, with automatic rollback on failed verification.
- `.gitignore` excludes `.env`, `status.json`, `OUTCOME.md`, `TASK.md`, and OS artifacts.

See [`rules.md`](./rules.md) for the full, itemized constraint list.

---

## Testing & CI

```bash
make test        # runs pytest tests/
make lint         # runs ruff, if installed — skips cleanly otherwise
make typecheck    # runs mypy, if installed — skips cleanly otherwise
```

`lint` and `typecheck` are optional and never required for `make setup`/`make run`/`make test` to succeed — they degrade gracefully if the tools aren't present, so they can never be the reason a clean evaluator environment fails.

---

## Hackathon Compliance

| Requirement | How it's met |
|---|---|
| `make setup` / `make run` work with zero manual steps | Isolated venv created and populated entirely by `make setup`. |
| Credential via `AI_API_KEY` only | Read from env at runtime; never hardcoded, never committed. |
| Text-only model | No image/audio/video input or processing anywhere in the pipeline. |
| Model swappable without code changes | `AI_MODEL` / `AI_BASE_URL` env vars, or `harness.yaml`. |
| TUI launches via `make run` | No separate discovery command needed. |
| No evaluator modification required | Everything needed is declared and installed by `make setup`. |

---

## Project Structure

```
Otsutsuki/
├── Makefile
├── README.md
├── .env.example
├── .gitignore
├── src/
│   ├── main.py            # entry point
│   ├── config.py          # env-driven configuration
│   ├── orchestrator.py     # core state machine
│   ├── llm_client.py       # model API wrapper + retry logic
│   ├── sandbox.py          # command execution + resource limits
│   ├── context_manager.py  # tree-structured context pruning
│   ├── evidence.py         # TASK.md / status.json / OUTCOME.md
│   ├── audit.py            # append-only event log
│   ├── architect.py        # multi-agent: task planning
│   ├── verifier.py         # multi-agent: full-diff verification
│   ├── logging_setup.py    # structured logging
│   ├── exceptions.py       # HarnessError hierarchy
│   └── tui.py              # chat-style Textual interface
├── tests/
├── architecture.md
├── rules.md
├── phases.md
├── design.md
├── memory.md
└── prd.md
```

---

## Known Limitations

- The harness currently operates on its own checked-out directory as the working repository. Pointing it at a separate target codebase (as a real evaluation issue would require) needs an explicit `TARGET_REPO` configuration path — tracked as an active fix, not yet merged as of this writing.
- Rate limits on free-tier model providers (e.g. Groq) can throttle multi-agent mode, which issues more calls per run than single-agent mode.
- Parallel multi-agent execution (git worktrees) falls back to sequential execution if `git worktree` is unavailable or a merge conflict occurs — this is by design, not a crash, but it does mean parallelism isn't guaranteed on every environment.

---

## Further Reading

- [`prd.md`](./prd.md) — product requirements and success metrics
- [`architecture.md`](./architecture.md) — full system design
- [`rules.md`](./rules.md) — hard constraints and compliance rules
- [`phases.md`](./phases.md) — build plan and definitions of done
- [`design.md`](./design.md) — TUI design specification
- [`memory.md`](./memory.md) — context, checkpoint, and evidence memory model
