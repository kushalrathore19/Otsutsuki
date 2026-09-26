# Memory — Context, State, and Evidence

This harness has **no cross-run memory** — each evaluation task is a clean,
self-contained run (see `prd.md` Out of Scope). "Memory" here means how
state is managed *within* a single run: what the model sees, what the
orchestrator remembers, and what survives to disk as evidence.

## 1. Working memory (in-context, per run)

- **Not a flat, ever-growing transcript.** The orchestrator uses
  tree-structured context branching: on a tool or test failure, it prunes
  back to the last stable state and forks with a single synthetic
  scratchpad note — e.g. *"Attempt 1 failed: syntax error in test
  runner"* — instead of carrying the entire failed branch's raw output
  forward.
- **Log content is distilled, not replayed.** Subprocess output entering
  context is filtered down to exceptions, failed assertions, exit codes,
  and a short tail — never the full raw log (see `rules.md` §6.2).
- **File state is incremental.** The orchestrator keeps a hash/mtime map
  of every file already shown to the model. Only files changed since the
  last turn are re-sent; the full repo is never retransmitted after the
  initial exploration pass.

## 2. Checkpoint memory (filesystem, per attempt)

- Before any model-initiated edit, the working tree is `git stash`ed. This
  is a durable checkpoint, not an in-context memory — it survives even if
  the conversation state is discarded.
- Two paths read this checkpoint:
  - **Automatic rollback** — triggered by the orchestrator on failed
    verification.
  - **Manual rollback** — triggered by the operator from the TUI at any
    point, independent of verification outcome.
- Rollback count (auto vs manual) is itself recorded as a metric in
  `OUTCOME.md` — the harness remembers how often it needed to recover.

## 3. Contract memory (disk artifacts, persistent evidence)

These three files are the harness's actual long-term memory of a run —
they outlive the process and are what a judge or operator inspects:

| File | Written | Contents |
|---|---|---|
| `TASK.md` | Once, at ingestion | The original software-engineering task as given |
| `status.json` | Continuously, at every state transition | Ledger of states, tool calls, self-critique fire/skip decisions, rollback events (auto/manual), confirmation gate outcome |
| `OUTCOME.md` | Once, at completion (success, failure, or degraded) | Test pass/fail matrix, applied diff, token usage, LLM call count, rollback count |

`status.json` is the mid-flight memory — it lets someone inspect a run
that hasn't finished yet. `OUTCOME.md` is the settled memory — the final
account that must exist even on a degraded/partial run.

## 4. What the harness deliberately does *not* remember

- No memory of previous hackathon tasks or previous runs — each `make run`
  starts cold.
- No cached embeddings or long-term repository index — repo understanding
  is re-derived each run via `grep`/`find`/`cat`, by design (see
  `architecture.md` §6), to keep the system auditable and simple.
- No silent retry memory across process restarts — if the process dies,
  the last `status.json` checkpoint is the full extent of what's known;
  there is no hidden state elsewhere.
