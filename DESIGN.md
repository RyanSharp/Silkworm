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

**Not every input is a task.** A booking confirmation asks nothing of you; it
is something a project should *know*. Routing it to the board would mean
clicking to dismiss a fact, which is how a board stops reaching empty. So mail
has two outputs and they go to different places: labelled mail becomes **facts
in the project's files**, and only the opt-in inbox triage produces tasks.

That distinction is what makes email worth having at all. As a task source it
largely duplicates reading your own inbox — you already do that, with better
tools. As a fact source it does something you would not do by hand:
consolidating logistics into a project the agents can see.

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

**Sessions are keyed to threads, not projects.** Each task gets its own session,
deliberately: a shared per-project session would accumulate useful context but
grow without bound, and unrelated tasks would pay for and be confused by each
other's history. Independent sessions are also what makes a task a reviewable
unit.

Accumulated knowledge is carried as **context, not conversation**. A repo-backed
project already gets learnings, scoped by git remote, and has a CLAUDE.md of its
own that belongs to you — Silkworm never writes there.

A project *without* a repo gets a directory of its own under
`~/workspace/projects/<slug>/`, which it needs anyway for its files, holding a
**CLAUDE.md** that Claude Code loads by itself. Tasks filed under the project
run in that directory, so the context arrives with no injection machinery at
all: no prompt assembly, no size accounting, no cache churn to reason about.
Binding a Slack thread with `!project` moves that thread into the directory for
the same reason.

The file still maintains itself: after a task completes, a cheap model rewrites
it from what happened. That is the one thing a hand-written CLAUDE.md doesn't
do, and the only reason any of this is Silkworm's business. Everything else —
storage, editing, git history, loading — is left to tools that already do it.

`!brief` reads or sets it; it is an ordinary file you can also just edit.

Alongside it, `logistics.md` holds facts drawn from mail. It is append-only and
never rewritten: a flight number has no shelf life, and a past trip is history
rather than clutter. Keeping it out of CLAUDE.md is what stops the brief's cap
and its wholesale rewrite from eating an itinerary.

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
   Read-only by construction: readonly select and BODY.PEEK, so watching mail
   never changes what the user sees. Off unless credentials are set.*

   *A Gmail label matching a project's name is the trigger **and** the project
   assignment in one gesture the user already makes, which is why there is no
   junk-filtering problem to solve: no classifier, no sender memory, no
   Primary-vs-Updates judgement. Labelled mail is read for durable facts and
   appended to the project's `logistics.md`, with attachments saved beside it.
   Deliberately append-only and separate from CLAUDE.md, which is capped and
   rewritten wholesale after every task — an itinerary put there would be
   summarised away within a turn. CLAUDE.md carries a structural pointer to it
   that a rewrite cannot drop.*

   *Inbox triage — a cheap model over sender/subject/snippet, everything it
   flags landing `proposed` — still exists but is opt-in behind `GMAIL_TRIAGE`.)*

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

## Liveness is not connectivity

The bot is a process *and* a socket, and only the process was ever watched.
launchd's `KeepAlive` restarts what exits; the local HTTP server answers
whether or not Slack can reach us. On 2026-08-24 the socket-mode link broke at
22:26, slack_sdk reconnected, each fresh session broke immediately, and the
loop ran for seventeen hours. Every health signal in the system said fine. The
only symptom was silence, and a person eventually restarted it by hand.

So the link is now sampled directly (`slack_health.py`). Sampling once is not
enough — in that loop a session really is established every few seconds, so a
well-timed sample honestly reports "connected". Health is a **fraction over a
five minute window**: a link up two percent of the time is down.

The remedy is deliberately blunt. Repairing slack_sdk's internal state from
outside is guesswork, and a restart is what actually worked when a person did
it; turns interrupted by one are already rescued by `recovery.py`. A bad window
asks for a reconnect, and a second bad window after that exits so `KeepAlive`
brings the process back with a clean client. The repair runs in its own thread,
because a `connect()` that blocks must not stall the sampler whose job is to
escalate when the repair does not take.

Both `silkworm status` and the dashboard now report the link separately from
the server, since "reachable" was true throughout the outage.

**Known limit:** this catches a link that visibly drops, not a half-open socket
that stays `is_connected()` while delivering nothing. A second signal (age of
the last received event) would catch that, but on an idle workspace it is
indistinguishable from quiet, so it is not worth the false alarms yet.

## Coming back is more than reconnecting

Socket Mode does not queue. A message sent while the link is down is not
redelivered when it returns — it is gone, and nothing records that it existed.
The 2026-08-24 outage swallowed five messages that way; four were noticed and
re-typed by hand, one was never answered.

So on startup, and after any reconnect, each known thread is re-read from the
Web API — which has the history the socket does not — and anything past the
thread's `last_msg_ts` goes through the ordinary prompt path. Replay needs no
new idempotency: that path already refuses anything at or behind the
watermark. Bounded on purpose — a thread with no watermark is skipped, because
"everything ever said" is not a backlog, and messages older than a few days
have been re-asked or stopped mattering.

Recovery also had to stop being a single startup pass. A turn still running
when that pass fired was left pending by design — only a finished child gives
a trustworthy reply — but nothing ever came back for it, so restarting during
a long turn silently swallowed the answer. A sweep now runs every two minutes
with `wait_s=0`, collecting only children that have already exited and skipping
turns this process is running: their marker belongs to a live handler, and
posting it here would deliver the reply twice.

The sweep is a **separate thread, not a tail on the startup pass**. That pass
waits up to an hour on a live child, so a sweep queued behind it would not
engage until the outage it exists to shorten was already over. Running the two
concurrently is safe because recovery **claims a thread** before resolving it —
released in a `finally`, including the still-running path, since a leaked claim
would lock that thread out of every later pass.

That sweep can rescue a turn a restart already recorded as failed, so
`failed → done` is now a legal transition. Leaving a delivered answer filed
under "needs you" is the noise that stops the board being trusted.

## "Get back to me when it's done"

A turn is request/response — one prompt in, one reply out — and the session is
dormant either side of it. "Monitor this and tell me when it finishes" is not
that shape, and had two ways to be lost, both reachable:

- **Hold the turn open.** It fights everything: the 15 minute cap kills it, the
  runaway reaper kills what survives, the thread lock means you cannot talk to
  that thread meanwhile, and a restart orphans it.
- **End the turn and background the work.** The answer arrives somewhere nobody
  is listening — nothing outside a turn was wired to speak.

The fix is not a longer timeout. It is to **stop holding the turn open**:
finish now, and schedule a short turn for later on the same session.

    silkworm defer 10m "check whether the deploy finished"

That is an ordinary task in `blocked` with a `retry_at`, which the queue runner
already requeues when its time arrives. So a watch costs nothing while it
waits, survives restarts because it is a durable record rather than a sleeping
process, and resumes the thread with its context intact — the note it left
itself is read by a session that remembers writing it.

**Silence is the point.** A wake-up with nothing to report replies with a
sentinel and its placeholder is deleted, so the thread stays quiet until there
is news; a watch that narrates every poll is worse than no watch. But "nothing
yet" with *no successor scheduled* is a watch that quietly stopped watching —
the exact silence this exists to prevent — so that lands in `needs_input`
instead of vanishing.

Chains are capped at 24 wake-ups. A model that keeps misjudging "is it finished
yet" would otherwise poll at your expense indefinitely.

## Two agents in one checkout

Thread locks are keyed by thread, which was fine while a thread was the only
way to start work. Filing a task under a project broke that: the task opens a
*new* thread pointed at the project's directory, so a queued task and an
ordinary conversation about the same repo run at once. Two agents in one
working tree do not merely race on files — one running `git checkout` moves
the ground under the other.

So a turn also holds its **checkout** for its duration. Only real repo roots
are serialised: the shared scratch directory holds a dozen unrelated projects,
and locking that would queue every thread behind every other for nothing. The
guard is taken inside the thread lock at both call sites, so the ordering is
consistent and cannot deadlock, and a turn that has to wait says so in its
progress message rather than appearing hung.

This trades throughput for safety, which is the right way round here: waiting
is visible and recoverable, and a half-applied edit from two agents is not.
Worktree isolation (`scope.worktree`, still a no-op) is the way to get the
parallelism back later.

**A project pointed at a real checkout is never ours to write.** `!project`
sets a project's cwd from the thread it was run in and leaves `repo` empty, so
keying "is this ours" on that field alone let a project pointed at a working
repo look repo-less — and the brief rewriter would have replaced a
hand-written CLAUDE.md with a generated paragraph. The check asks the
filesystem: a directory containing `.git` is yours, however the record was
filled in.
