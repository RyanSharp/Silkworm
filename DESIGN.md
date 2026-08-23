# Task system — design

Status: steps 1-4 built. Every Slack turn is a task, a queue runner executes
tasks with no live message, the dashboard opens on what needs you, and implementor work is
gated behind an independent review. Next: ingestion adapters (step 5).

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

A turn that dies on **quota exhaustion or API overload** is not a failure a
person can act on — there is nothing to fix, only a time to wait. Those are
classified, parked in `blocked` with a `retry_at`, and requeued automatically
(at the stated reset time for quota, on a capped backoff otherwise), giving up
after a handful of attempts. Real failures still stop and ask. Filing "the API
was busy" under "needs you" is exactly how a board stops being trusted.

A failed turn also has no way to notice that its thread recovered without it.
When a task completes, earlier **failed conversational turns on the same
thread** are superseded: if the conversation carried on and produced answers,
you either re-asked or moved on, so the old failure is not an open action item.
Restricted to conversational turns — a queued task that failed is work that did
not happen, and an unrelated success elsewhere says nothing about it.

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

## Projects

A **project** is what a task belongs to; **scope** is where it may act. They
are separate on purpose: two projects can share a repo, one project can span
several, and plenty of projects ("plan the Asia trip") have no repo at all.
Grouping by `cwd` would work for the first case and fall apart on the last.

Projects are created on first use, never set up in advance — a system that
makes you define a project before filing anything is one you stop using. A
project may carry a default scope that its tasks inherit, so "work on the
trader" lands in the right directory without repeating it.

Project is a **lens, not a new default**: the dashboard still opens on what
needs you across everything, with a filter to narrow. A per-project inbox that
you must visit N times to see your work would undo the rule above.

Binding a Slack thread with `!project <name>` makes every task from that thread
inherit it.

Sessions are keyed to the **thread**, never to the project: each task gets its
own, so tasks stay independent, reviewable units and one does not pay for
another's history. Accumulated knowledge is carried by **context injection**
instead — learnings scoped by repo, and a per-project **brief** injected into
every task filed under it. The brief is *rewritten* after each task rather than
appended, since it rides on every prompt for that project; a log would become a
tax. Projects with no repo get no learnings, so the brief is the only thing
keeping them from starting cold each time.

**Sessions are keyed to threads, not projects.** Each task gets its own session,
deliberately: a shared per-project session would accumulate useful context but
grow without bound, and unrelated tasks would pay for and be confused by each
other's history. Independent sessions are also what makes a task a reviewable
unit.

Accumulated knowledge is carried as **context, not conversation**. A repo-backed
project already gets learnings, scoped by git remote. A project without a repo
got nothing — so every task under "plan the Asia trip" started cold and re-asked
what was already settled. Each project now carries a short **brief** that is
injected into every task filed under it, and rewritten (never appended) after a
task completes, so it stays a living document rather than a log that taxes every
prompt. `!brief` reads or sets it by hand.

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
   task, driven queued -> running -> done/failed/cancelled.)*
   A **queue runner** executes tasks nobody is driving — the ones created in
   the UI rather than by a Slack message. It claims work atomically, so a
   runner and a Slack turn can never both execute the same task, and runs
   serially: for one person, parallel agents multiply the reviewing, which is
   the actual bottleneck. *(built)*
3. **"Needs me" view** in the dashboard, plus create-a-task. *(built: a 📋
   Tasks panel opening on what needs you, with accept/dismiss/retry/cancel and
   a header badge counting them. Recovery reports each thread's outcome so a
   rescued reply closes its task as done rather than filing a false failure.)*
4. **`reviewer` role** and one gate on implementor output. *(built: an
   implementor cannot complete on its own say-so — its result goes to a
   reviewer with a fresh session and read-only tools, and the task waits in
   `blocked`. A passing verdict completes it silently; a flagged one lands in
   `awaiting_approval` with the findings. An unreadable verdict fails closed.)*
5. Ingestion adapters, worktree isolation, per-role tool scoping.
   *(email: built. Gmail over IMAP with an app password — the API needs a
   Cloud project and a browser consent flow, awkward on a headless host.
   Read-only by construction: readonly select and BODY.PEEK, so watching the
   inbox never changes what the user sees. A cheap model triages
   sender/subject/snippet only — full bodies never leave the machine — and
   everything it flags lands `proposed`, so a wrong guess costs two clicks
   rather than flooding the list. Off unless credentials are set.)*

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
