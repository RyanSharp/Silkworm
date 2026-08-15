# Task system — design

Status: agreed, partially built. `tasks.py` implements the record, store and
lifecycle. Execution and UI are not wired yet.

## What this is

A **task management system that happens to execute**, not an executor that
happens to store tasks. Work items live here, arrive from a growing set of
sources, and move through a lifecycle with the user in the loop at defined
points. Silkworm is the **source of truth** for the state of that work.

The existing Slack behaviour is a special case: a thread is a task with one
role, no parent, and no successor.

## The rule that keeps it alive

The default view is **"what needs me?"** — never "everything." Personal task
systems die of upkeep; a board you must groom stops being opened within weeks.
If the board cannot reach empty, it is the wrong board.

This matches the real constraint: for a one-person system the bottleneck is
**review capacity**, not throughput. Every feature is judged by whether it
reduces attention spent, not work produced.

## Lifecycle

    proposed ─accept→ queued ─→ running ─→ done
       │                          │  ↑ ↓
       └─dismiss→ cancelled       │  │ └→ awaiting_approval ─approve→ running
                                  │  │ └→ needs_input ──answer──────→ running
                                  │  │ └→ blocked ────dependency────→ queued
                                  │  └───────────────retry───────────┘
                                  └─→ failed

**States that require the user** — and only these appear in the default view:

| State | Meaning |
|---|---|
| `proposed` | auto-ingested; accept or dismiss before it becomes work |
| `awaiting_approval` | mid-flight, needs a yes/no |
| `needs_input` | asked a question and stopped |
| `failed` | ended badly; retry or dismiss |

Everything else (`queued`, `running`, `blocked`, `done`, `cancelled`) is the
system's business and is not surfaced by default.

`proposed` exists specifically to gate auto-ingestion. Tasks the user creates
start at `queued`; ingested ones start at `proposed`, so turning on a new source
can never flood the actionable list.

## Why durable state matters

`awaiting_approval` is the concrete win over today. Approval buttons currently
block a live thread and die with the process, so an approval arriving after a
restart is lost. As a record, a waiting task survives anything — which is what
makes gated autonomy real rather than best-effort.

## Sources

Day one: **user-created (UI)** and **Slack**. Later: GitHub, email, recurring
schedules, failures from other systems. Deliberately stingy — an ingest surface
with five adapters recreates the review burden it was meant to remove.

Silkworm owns the state of the work. For an item owned elsewhere (a GitHub
issue), the task carries a `source_ref` pointing at it and sync is **one-way
outward**; the external item is never treated as a second state machine.

## Record

See `tasks.py` for the authoritative field list, in the same style as
`schema.py`. Shape:

    id, v, title, goal, state, role, source, source_ref,
    scope {repo, cwd, branch, worktree, paths},
    thread, session_id, checkpoint, parent, root, blocked_on,
    result {text, artifacts, cost}, events, attempts,
    created, updated

`checkpoint` is `recovery.py`'s `pending` marker generalised — the restart
recovery already built carries over rather than being rewritten.

## Roles

A role is a reusable template a task references by name: model, extra system
prompt, allowed/denied tools, permission mode, isolation (`none` | `worktree`),
expected output, and limits. Only `assistant` exists at first.

The **broker is a role, not a topology** — its output is a validated list of
`{role, goal, scope}` to enqueue. Routing stays adaptive; there is no drawn
graph to maintain.

## Build order

1. **Record + store + lifecycle** — no execution. *(built)*
2. **Executor**: one `assistant` role reproducing today's Slack behaviour
   exactly. Nothing user-visible should change. *(built: every Slack turn is a
   task, driven queued -> running -> done/failed/cancelled. Control is still
   message-driven; a queue runner for tasks with no live message is next.)*
3. **"Needs me" view** in the dashboard, plus create-a-task.
4. **`reviewer` role** and one gate on implementor output.
5. Ingestion adapters, worktree isolation, per-role tool scoping.

Steps 1–3 are what make it a management system; 4–5 are what make it an agent
system. In that order, because a queue nobody looks at is worse than no queue.

## Rejected

- **A drawn agent graph.** Real tasks do not decompose the same way twice; a
  fixed topology forces one-line edits down the same path as a refactor, and a
  graph editor mostly reimplements a routing decision the model makes for free.
- **LangGraph.** Its node is a function that calls a model; ours is a whole
  Claude Code session with its own tool loop, permissions and resumable
  transcript. Wrapping that as an opaque subprocess forfeits tool binding, the
  agent loop, streaming and step-level tracing — most of what the framework is
  for — while adding a second persistence layer that does not know about the
  session transcripts recovery already reads. Worth stealing as patterns:
  explicit state schema, checkpoint per step, interrupt/resume, dynamic fan-out.
