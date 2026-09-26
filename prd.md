# PRD — Proof-First AI Coding Harness

**Event:** AI Harness Hackathon 2026 (LCC × DevClub)
**Status:** Locked for build
**Stack:** Python, Textual (TUI), Anthropic API

## 1. Overview

A single-agent, bash-only coding harness that turns a fixed, text-only foundation
model into an autonomous software engineer. The design philosophy is
**Proof-First**: every claim of "done" must be backed by a written, auditable
evidence trail — not a console log the evaluator has to trust.

The harness deliberately rejects multi-agent complexity in favor of one
optimized state machine, because the hackathon scores **correctness,
evidence, and efficiency** — not architectural sophistication.

## 2. Problem Statement

> Build an autonomous coding-agent harness that can understand a
> software-engineering task, navigate an existing repository, intelligently
> use tools, manage context, orchestrate model interactions, recover from
> failures, and produce correct, verified changes — with efficient use of
> resources.

## 3. Goals & Success Metrics

| Goal | Metric | Priority |
|---|---|---|
| Correctness | Issue resolved; model-authored regression test + existing suite pass | P0 |
| Reproducibility | `make setup && make run` succeed on a clean machine, zero manual steps | P0 |
| Evidence over claims | `TASK.md` / `status.json` / `OUTCOME.md` fully populated every run | P0 |
| Efficiency | Tokens, LLM call count, wall-clock time per resolved task | P0 |
| Failure recovery | % of runs that self-correct or cleanly rollback without human help | P1 |
| Human control | User can rollback or halt before a commit at any point | P0 |
| Security | Zero hardcoded credentials anywhere in the repo | P0 |

## 4. Users & Stakeholders

- **Evaluator (primary):** clones repo, runs the standardized Makefile flow,
  feeds the prescribed GitHub issue. Never touches source or config.
- **Organizing Committee:** defines model, environment, scoring policy.
- **The operator (you, during a run):** watches the Mission Control TUI, can
  manually rollback, and must approve the final commit.

## 5. Functional Requirements

### 5.1 Mandatory hackathon interface
- `make setup` — installs all deps into an isolated venv, zero manual steps.
- `make run` — launches the Textual TUI; reads `AI_API_KEY` from env only.
- `make test` — runs the harness's own test/evaluation procedure.
- `make clean` — removes `status.json`, temp `.patch` files, generated artifacts.
- Model is text-only; no image/audio/video processing anywhere in the pipeline.
- Model identity is declared in config and swappable without code changes.

### 5.2 Core agent capabilities
- Single tool exposed to the model: `run_bash(command)`.
- Self-directed repo navigation (`grep`, `find`, `cat`) — no custom search tool.
- All code changes applied as unified `.patch` files via the `patch` utility.
- Model authors a standalone regression test targeting the specific issue
  before the harness accepts a fix as complete.

### 5.3 Human-in-the-loop controls (new)
- **Ask before committing:** the harness pauses at a confirmation gate and
  requires explicit operator approval before finalizing (git commit) any change.
- **Manual rollback:** the operator can trigger a rollback to the last clean
  checkpoint at any time from the TUI, independent of automatic
  failure-triggered rollback.

## 6. Non-Functional Requirements

- **Efficiency-first optimization** (see `rules.md` §6): conditional
  self-critique (fewer second calls), log distillation instead of raw log
  replay, incremental (changed-files-only) context reload.
- **Reproducibility:** fixed seed/temperature where supported; iteration cap
  and tool set documented in README.
- **Environment independence:** no undocumented dependencies, no manual
  source/config edits by the evaluator.
- **Security:** `AI_API_KEY` read from environment only, never committed.

## 7. Out of Scope

- Multimodal input handling.
- Multi-agent orchestration / agent debate or voting.
- Cross-run persistent memory (each evaluation run is self-contained; see
  `memory.md` for what "memory" means *within* a single run).
- Substituting a different model than the one prescribed, without
  Committee authorization.

## 8. Risks & Mitigations

| Risk | Mitigation |
|---|---|
| Harness fails to launch in a clean environment | Full clean-VM test of `make setup && make run` before submission |
| Confirmation gate blocks unattended evaluator runs | Provide a `--auto-approve` env flag for the evaluator's non-interactive path; default remains interactive for demo/judging |
| Over-engineering eats build time | Ship the working loop first; treat everything in `phases.md` Phase 5+ as stretch |
| Accidental credential commit | `.gitignore` for `.env`; `.env.example` ships empty |

## 9. Timeline

Results expected ~7–10 days after evaluation. Promising repos may continue
through LCC × DevClub post-hackathon — build for maintainability, not just
demo day.
