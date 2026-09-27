# Rules & Constraints — Proof-First AI Coding Harness

These are hard constraints. Anything marked **[HACKATHON]** is a scored
compliance requirement from the official rules — breaking it can zero the
submission. Anything marked **[ENGINEERING]** is our own guardrail and can
be tuned, but should not be removed without a documented reason.

## 1. Hackathon Compliance Rules [HACKATHON]

1. Repo root **must** contain a `Makefile` exposing `setup`, `run`, `test`,
   and (where applicable) `clean`.
2. `make setup` and `make run` must both succeed with **zero manual steps**
   on a clean checkout — this is the minimum bar for scoring.
3. TUI, if implemented, must launch entirely through `make run`. The
   evaluator never discovers a team-specific command.
4. The evaluation team will **not** modify the submission to make it run.
   If it doesn't launch via the standard interface, that's the score.

## 2. Credential & Security Rules [HACKATHON]

1. The only source of the model credential is the `AI_API_KEY` environment
   variable, read at runtime.
2. No API keys, tokens, passwords, or secrets in: source code, Makefile,
   `.env` files, documentation, or any committed config.
3. `.env.example` may exist with an **empty** value only.
4. Nothing in this harness may write a credential to disk, log it, or
   include it in any artifact (`TASK.md`, `status.json`, `OUTCOME.md`).

## 3. Model & Modality Rules [HACKATHON]

1. Text-only input and output. No image, audio, video, or multimodal
   processing anywhere in the pipeline.
2. The model/model family in use is clearly declared in config.
3. If the Organizing Committee prescribes a model, it must be used;
   swapping models requires the credential/config path only, never a
   source-code change.

## 4. Agent Behavior Rules [ENGINEERING]

1. The model is given **exactly three tools**: `run_bash(command)`,
   `read_file(path, start_line, end_line)` and `apply_patch(diff)`. No
   custom editor tool beyond the diff applier.
2. Every code change **must** be expressed as a unified diff applied via
   `apply_patch` — never an inline shell overwrite, and never a
   hand-rolled `patch -p0` invocation from `run_bash`.
3. The model navigates the repository itself: range-based `read_file`
   for source inspection (minified/binary files are refused by the
   reader), `grep`/`find` via `run_bash` for search — the orchestrator
   does not pre-fetch or summarize the repo for it.
4. Every tool call is a fresh, independent `subprocess.run` — no persistent
   shell session, no hidden state between calls.

## 5. Orchestrator Rules [ENGINEERING]

1. `git stash` the working tree **before** applying any model-proposed
   patch. No edit lands on a dirty checkpoint.
2. Run environment scrubbing (kill orphan processes, clear temp caches)
   **before every** test-runner invocation, not just the first.
3. Hard iteration cap (default: 8). On reaching it, stop and produce a
   partial report — never loop silently or crash.
4. On any failed verification, automatically roll back to the last
   checkpoint before retrying.

## 6. Efficiency Rules [ENGINEERING]

### 6.1 Conditional self-critique (fewer second calls)
The self-critique ensembler pass is **not automatic**. It fires only when
**any** of the following is true:
- The patch touches more than one file, **or**
- The patch exceeds a line-count threshold (default: 15 changed lines), **or**
- This is a retry after a previously failed verification.
Trivial, single-file, small patches on a first attempt skip straight to
the test runner. This is the primary lever for reducing total LLM calls
per task without reducing correctness coverage where it matters.

### 6.2 Log distillation, not raw replay
Subprocess `stdout`/`stderr` is never fed back to the model in full. Before
re-injection into context, a filter extracts only:
- Exception/traceback lines
- Failed test names and assertion messages
- Non-zero exit codes and the command that produced them
- The final ~20 lines of otherwise-successful output (for confirmation)
Everything else is dropped. This replaces blind "last N lines" truncation
with signal-only extraction.

### 6.3 Incremental context reload
The orchestrator maintains a hash/mtime map of files already sent to the
model. On each turn, only files that changed since the last turn are
re-sent. The full repo is never re-transmitted after the first exploration
pass.

## 7. Human-in-the-Loop Rules [ENGINEERING]

1. **Ask before committing:** after verification passes (and any
   self-critique gate clears), the orchestrator does **not** auto-commit.
   It presents the proposed diff + `OUTCOME.md` summary in the TUI and
   blocks on operator approval (Y/N).
2. **Manual rollback:** the operator can trigger a rollback to the last
   checkpoint at any point during a run via a dedicated TUI action —
   independent of, and in addition to, automatic failure-triggered
   rollback.
3. **Unattended evaluator path:** because the hackathon's standard
   evaluation flow is non-interactive, `make run` must support an
   `AUTO_APPROVE=1` (or equivalent) environment flag that bypasses the
   confirmation gate for scoring runs, while defaulting to interactive
   mode for demos. This must be documented in the README so it is never
   mistaken for a hidden bypass.

## 8. Sandbox & Execution Isolation Rules [ENGINEERING]

Every `run_bash` call executes inside an isolation wrapper, not a bare
`subprocess.run`. This is defense-in-depth around an agent that is, by
design, allowed to run arbitrary shell commands.

1. **Timeout:** each command is killed if it exceeds a hard wall-clock
   limit (default: 30s). A hang counts as a failed action and feeds a
   scratchpad note back, same as any other failure.
2. **Resource limits:** CPU time, memory, max processes, and max file size
   are capped via `resource.setrlimit` in the subprocess `preexec_fn`.
   Runaway commands are killed by the OS, not by hoping the model behaves.
3. **Working-directory confinement:** `cwd` is locked to the repo root for
   every call. Any path argument that resolves outside the repo is
   rejected before execution, not after.
4. **Minimal environment:** the subprocess receives a stripped-down `env`
   — never the harness's own process environment. `AI_API_KEY` is never
   passed into a shell the model controls.
5. **Best-effort network isolation:** attempt `unshare --net` (Linux) to
   deny network access during test/edit commands. If unavailable on the
   evaluator's machine, fall back to unrestricted networking rather than
   failing `make setup` — this must never become a hard dependency.
6. **Shallow deny-list (backstop only):** obviously destructive patterns
   (`rm -rf /`, `sudo`, `mkfs`, arbitrary `curl`/`wget`) are blocked before
   execution. This is a safety net, not the primary control — the real
   containment is timeout + rlimits + cwd confinement.
7. **No sandbox dependency may block the hackathon minimum bar.** If any
   isolation primitive (e.g. `unshare`) is missing on the evaluator's
   environment, the harness degrades gracefully and logs the degradation
   in `status.json` — it never causes `make setup` or `make run` to fail.

## 9. Evidence Rules [ENGINEERING]

0. Any sandbox degradation (e.g. network isolation unavailable) is recorded
   in `status.json` as part of the run's evidence trail, not hidden.
1. `TASK.md`, `status.json`, and `OUTCOME.md` are written on **every** run
   — success, failure, or degraded — never only on the happy path.
2. `status.json` is updated at every state transition, not just at the end,
   so a run can be inspected mid-flight.
3. `OUTCOME.md` must include: test pass/fail matrix, applied diff, token
   usage, LLM call count (including calls saved by conditional
   self-critique), and rollback count (auto vs manual).
