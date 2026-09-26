# Build Phases — 24-Hour Execution Plan

Hard rule if you fall behind: **cut TUI polish before you cut the
verification loop.** A working CLI harness that passes tests beats a
beautiful TUI that doesn't.

## Phase 0 — Makefile + env wiring (Hour 0–2)
- Write `Makefile`: `setup`, `run`, `test`, `clean`.
- Confirm `AI_API_KEY` is read from env only — grep your own repo for
  hardcoded strings before moving on.
- **Definition of done:** `make setup && make run` succeeds on a clean venv
  and prints "harness ready" with no agent logic behind it yet.

## Phase 1 — Bare agent loop (Hour 2–7)
- Single Python script: task in → one `run_bash` tool call → apply result →
  exit. No loop, no TUI, no patch workflow yet — just prove the plumbing.
- **Definition of done:** the model can run one shell command against the
  repo and you see the output.

## Phase 2 — Patch workflow + tool set + sandbox (Hour 7–11)
- Model writes `.patch` to `/tmp/`, orchestrator applies via `patch`.
- Self-directed navigation via `grep`/`find`/`cat` — no custom tools.
- Environment scrubbing before test runs.
- Wrap `run_bash` in the sandbox layer: timeout, `resource.setrlimit` via
  `preexec_fn`, cwd confinement, stripped env, best-effort `unshare --net`
  with graceful fallback (see `rules.md` §8). This wraps the same call
  site you're already building — do it now, not as an afterthought.
- **Definition of done:** the model can propose a real patch and it applies
  cleanly to the working tree, and a deliberately runaway/destructive test
  command (e.g. an infinite loop or `rm -rf` on a scratch dir) gets killed
  by the sandbox instead of the harness.

## Phase 3 — Verification + recovery loop (Hour 11–15)
- Test runner wired in; model authors a regression test targeting the
  issue.
- `git stash` checkpoint before every edit; automatic rollback on failed
  verification.
- Retry loop with hard iteration cap (default 8) and scratchpad notes on
  failure (tree-structured context branching).
- **Definition of done:** a failing test triggers a clean rollback and a
  retry with a useful failure note, not a repeat of the same mistake.

## Phase 4 — Optimization layer (Hour 15–18)
- Conditional self-critique gate (multi-file / >15 lines / retry-only —
  see `rules.md` §6.1).
- Log distillation filter (exceptions, failed assertions, exit codes —
  not raw truncation).
- Incremental context reload (hash/mtime map, changed files only).
- **Definition of done:** re-running the same task a second time visibly
  uses fewer tokens and fewer LLM calls than the first pass.

## Phase 5 — Human-in-the-loop gate (Hour 18–20)
- Confirmation prompt before commit (`AUTO_APPROVE` env flag for
  unattended evaluator runs).
- Manual rollback control, independent of automatic rollback.
- **Definition of done:** you can watch a run reach "ready to commit," say
  no, and see it roll back cleanly — and separately, trigger a rollback
  mid-run on demand.

## Phase 6 — TUI: Mission Control (Hour 20–22)
- Wrap the working loop in Textual per `design.md`: Agent Manager plan
  panel, Activity/Editor tabs, Artifacts bar, metrics footer.
- This is a cosmetic layer on already-working logic — not a rewrite.
- **Definition of done:** the plan list updates live, the diff tab renders
  the actual patch, and the confirmation gate appears as a modal.

## Phase 7 — Evidence artifacts + clean-env test (Hour 22–23)
- Wire `TASK.md` / `status.json` / `OUTCOME.md` writes at every state
  transition.
- Fresh clone, fresh venv, run `make setup && make run` with zero manual
  steps. Fix whatever breaks. Write the README (include the
  `AUTO_APPROVE` flag docs).
- **Definition of done:** `OUTCOME.md` alone is enough for a judge to
  understand what happened, without reading logs.

## Phase 8 — Buffer / final polish (Hour 23–24)
- Only if everything above is done. Trim prompts, double-check no
  credentials leaked into git history, confirm `make clean` works.
- Do **not** start this early by skipping Phase 7.
