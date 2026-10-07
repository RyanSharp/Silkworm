<p align="center"><img src="assets/logo.svg" width="160" alt="Silkworm logo"></p>

# Silkworm 🪱

*Spins its own threads.*

A Slack bot that runs on your own machine and gives **every Slack thread its own
Claude Code session**.

- DM the bot, or @-mention it in a channel → it replies **in a thread**.
- The first message in a thread starts a headless Claude session (`claude -p`);
  every later message resumes the same session (`claude --resume`), so each
  thread keeps independent context.
- Sessions persist in `sessions.json`, so threads survive bot restarts.

## Features

- **Live progress** — a placeholder message updates in place with what Claude is
  doing (`Bash: npm test…`, `Edit: bot.py…`) and becomes the final reply.
- **Cost footer** — each reply ends with `⏱ 42s · $0.0312 · thread total $0.45`.
- **Per-thread models** — `!model haiku` inside a thread switches just that thread.
- **Interrupt** — `!stop` kills the running turn in that thread.
- **Files both ways** — attach files (screenshots included) and Claude gets
  them on disk, told which are images so it looks at them; files Claude
  produces are uploaded back into the thread.
- **Thread context** — @-mention the bot inside an existing conversation thread
  and it reads the prior messages first, so "summarize this thread" works.
- **Approval buttons** — optional `CLAUDE_APPROVAL_MODE=slack` gates tool calls
  behind Approve/Deny buttons in the thread (via a Claude Code PreToolUse hook).
- **Per-channel working dirs** — map `#myapp` → `~/code/myapp` so threads in
  that channel run Claude inside that repo.
- **Status reactions** — ⏳ while a turn runs, ✅ when it lands, ‼️ if it fails,
  on both the thread parent and the message being answered.
- **Context summaries** — each thread gets a 1–3 sentence "what is this about",
  refreshed after every turn and shown in the visualizer, with auto-generated
  thread names (`silkworm titles`).
- **Turn health** — the dashboard shows how long a turn has been running and
  whether a `claude` process is actually behind it, flags stalled threads and
  cost spikes at the top of the page, and can release a wedged thread in one
  click.
- **Restart recovery** — if the bot is restarted mid-turn, the next start
  recovers the reply from the session transcript and delivers it.
- **Session hygiene** — stale thread sessions are swept after 30 days;
  `!sessions` lists active ones.
- **Runs as a service** — launchd plist included (starts on boot, restarts on crash).
- **User allowlist** — restrict who can drive the bot (and click approvals).

## Commands (inside a thread)

| Command | Effect |
|---|---|
| `!help` | Show commands |
| `!reset` / `!new` | Start this thread's session over |
| `!model <alias>` / `!model reset` | Switch this thread's model |
| `!stop` | Kill the currently running turn |
| `!learn do\|avoid\|note <text>` | Add a learning (prefix `global` for all threads) |
| `!learnings` / `!unlearn <id>` | List / remove learnings |
| `!terminal` | Get the command to continue this thread in your terminal |
| `!stats` | This thread's session info (model, turns, cost) |
| `!sessions` | List all active thread sessions |

## Prerequisites

- Claude Code installed and logged in (`claude` works in your terminal)
- Python 3.10+
- A Slack workspace where you can install apps (free tier is fine — a custom
  app counts as 1 of the 10 free-plan integrations)

## 1. Create the Slack app

1. Go to <https://api.slack.com/apps> → **Create New App** → **From a manifest**.
2. Pick your workspace, paste the contents of `manifest.json`, and create the app.
3. **Basic Information → App-Level Tokens** → *Generate Token* with the
   `connections:write` scope. Copy the `xapp-...` token (Socket Mode — no
   public URL needed).
4. **Install App** → install to your workspace → copy the **Bot User OAuth
   Token** (`xoxb-...`).

> Already created the app from an older manifest? Paste the new `manifest.json`
> into **App Manifest** on the app page and **reinstall** so the new scopes
> (files, history, users) and interactivity take effect.

## 2. Install

```sh
git clone https://github.com/RyanSharp/Silkworm.git ~/workspace/Silkworm
cd ~/workspace/Silkworm
./bin/silkworm setup
```

`setup` checks prerequisites, creates the venv, copies `.env.example` → `.env`
(it stops and tells you exactly where to paste the two Slack tokens, then you
re-run it), installs the session hooks, and installs **two login services**
(`com.silkworm.bot`, `com.silkworm.viz`) so the bot and visualizer start at
login and restart on crash. Service definitions are generated from your actual
paths at install time — clone anywhere.

Day-to-day management (macOS today; Linux/systemd planned):

```sh
silkworm status      # services, bot, visualizer, hooks, revision — one health check
silkworm deploy      # load new code without killing work: drain, wait, restart, confirm
silkworm restart     # bounce both services now, killing whatever turn is running
silkworm logs        # tail the bot log
silkworm uninstall   # remove the services (repo and sessions untouched)
```

`silkworm deploy` is the one to load new code with. It refuses unless the
checkout is clean on its base branch and `silkworm test` passes, then asks the
bot to drain (the queue runner claims nothing new; running turns carry on),
waits until nothing has been running for 30s, restarts, and checks the bot is
back, connected to Slack, and running the checkout's HEAD. It gives up without
restarting after `--max-wait` (default 2h); `--now` skips the wait. A queued
task a restart does interrupt is resumed in its own session and checkout when
it next runs, rather than started over.

Tip: put it on your PATH — `ln -s ~/workspace/Silkworm/bin/silkworm /usr/local/bin/silkworm`.

`silkworm status` reports the git revision the bot is *running*, not the one in
the checkout, and fails when the two differ. Silkworm auto-merges onto its own
main and nothing restarts it, so a landed fix can sit in the tree for days
while the live process serves the revision it booted with — which is not
otherwise visible from anywhere. The dashboard's alert bar says the same thing.

Reboots are a non-event: sessions and thread mappings live on disk, the
services come back at login, and the bot releases any terminal checkouts that
predate the boot (those terminals are gone).

The plist reads config from `.env` (loaded by the bot itself), so no secrets
live in the plist.

## Configuration

All via `.env` — see `.env.example` for the full annotated list. Highlights:

| Variable | Default | Purpose |
|---|---|---|
| `SLACK_ALLOWED_USERS` | everyone | Comma-separated user IDs allowed to use the bot |
| `CLAUDE_CWD` | `./workspace` | Default directory Claude runs in |
| `CLAUDE_CHANNEL_DIRS` | — | JSON map of channel name/ID → directory |
| `CLAUDE_MODEL` | CLI default | Default model (per-thread override with `!model`) |
| `CLAUDE_APPROVAL_MODE` | `skip` | `skip` = full autonomy, `slack` = approval buttons, `gated` = plain permission mode |
| `APPROVAL_AUTO_ALLOW` | read-only tools | Tools that never need approval in `slack` mode |
| `CLAUDE_TIMEOUT` | `0` | Absolute per-turn cap in seconds; `0` = none |
| `CLAUDE_IDLE_TIMEOUT` | `1800` | Stop a turn after this long with **no output at all** |
| `SESSION_MAX_AGE_DAYS` | `30` | Forget *empty* idle thread records after this long. Anything with a title, summary, cost history, files or a task pointing at it is kept and retired by hiding instead |
| `TASK_COMPACT_AFTER_DAYS` | `14` | Drop the reply text and event log from finished tasks after this long; the record, its cost and anything still needing you are kept |
| `TASK_WORKERS` | `1` | How many queued tasks run at once (each is a Claude session). Reviews are not counted: they have their own lane |
| `REVIEW_WORKERS` | `1` | How many reviews run at once, alongside the task workers; `0` hands reviews back to the task workers |
| `DIGEST_AT` | `08:00` | Local time to post the daily digest (last 24h per project: landed, released, refused, failed or held, reran, stuck, waiting on you, unmerged, cost) to your DM; once a day, caught up after a restart; `off` to stop |
| `MAX_OPEN_PROPOSALS` | `4` | Untriaged proposals a project may hold before its nightly review pauses |

## Authentication for headless turns

Claude Code keeps OAuth credentials in **two** places — the login Keychain when
a process can reach it, and `~/.claude/.credentials.json` when it cannot — and
nothing keeps them in step. An interactive login refreshes only the file, so a
Keychain copy ages out unnoticed and then every headless turn fails with
`OAuth session expired and could not be refreshed`, while `claude` in your
terminal keeps working. That happened on 2026-08-30.

`silkworm status` now reports how long the refresh token has left and warns
below a day, and flags a Keychain copy existing at all, since its existence is
what allows the drift.

The durable fix bypasses both stores:

```sh
claude setup-token                 # interactive; needs a browser
read -rs TOKEN && printf '\nCLAUDE_CODE_OAUTH_TOKEN=%s\n' "$TOKEN" >> .env && unset TOKEN
silkworm restart
```

It goes in `.env`, not the plist: `.env` is gitignored, is already loaded into
the environment the bot hands each turn, and this repo keeps secrets out of
service definitions. `read -rs` keeps the token off your screen and out of
shell history.

Note that a shell test will not reproduce this class of bug — a terminal and a
launchd agent differ precisely in whether they can reach the Keychain. Trust
whether real turns are succeeding, or test from a throwaway launchd job.

## How long a turn may run

**As long as it is still working.** A turn used to be capped at 900 seconds,
which killed real work — a strategy backtest, a long refactor — because elapsed
time cannot distinguish progress from a wedge.

What ends a turn now is **silence**. Anything arriving on the process's output
resets the clock, including lines Silkworm does not parse; the question is
whether the process is doing anything, not whether it said something legible.
Only after `CLAUDE_IDLE_TIMEOUT` (default 30 minutes) with nothing at all is the
turn stopped, and the error says so rather than blaming the clock.

The limit is generous on purpose: a single tool call is legitimately quiet while
it runs, though Claude Code caps Bash at 10 minutes, so half an hour of total
silence means something is genuinely stuck. Set `CLAUDE_TIMEOUT` if you also
want a hard ceiling; it is off by default.

Some backstop is necessary rather than optional. A turn holds its thread's lock,
so a wedged one that never dies makes that thread unusable forever — which is
exactly the failure that once left a child running for 24 hours.

## How approval mode works

With `CLAUDE_APPROVAL_MODE=slack`, the bot registers a Claude Code
**PreToolUse hook** (`approval_hook.py`). Before each tool call, the hook POSTs
to the bot on `127.0.0.1:8787`; the bot posts Approve/Deny buttons in the
thread and blocks until someone clicks (default-deny after `APPROVAL_TIMEOUT`).
Read-only tools in `APPROVAL_AUTO_ALLOW` skip the buttons. Fails closed: if the
bot or hook is unreachable, the tool is denied.

## Learnings

Teach Silkworm things to **always do**, **never do**, or just **keep in mind**,
and they're injected into the system prompt of matching sessions:

```
!learn avoid force-push to main            # applies to threads working in this dir
!learn do run pytest, never unittest
!learn global do reply concisely in Slack  # applies everywhere
!learnings                                 # what applies to this thread
!unlearn lrn_ab12cd                        # remove one
```

Mostly, though, you don't write these — a **harvester** does. Every few hours
(`HARVEST_INTERVAL_H`) it reads each session's *new* activity since it last
looked and distills durable do/avoid/note learnings with a model
(`HARVEST_MODEL`), deduped against what's already stored. Run it on demand with
`silkworm harvest` or the ✨ button in the 🧠 panel.

**Scope is by repository, not path.** A learning is either **global** or scoped
to a **repo identity** (the normalized `git remote origin`, e.g.
`github.com/you/app`). Because that identity is shared across every worktree and
clone of a repo, a learning harvested in one workspace applies to all of them —
spin up five worktrees of the same repo and they share one body of knowledge.
Directories without a git remote fall back to path scope.

**Auditing:** the 🧠 **Learnings** panel in the visualizer lists every learning
with an `auto`/`manual` badge, a link to its source session, and an enable
toggle — flip one off to stop injecting it without losing the record.

**Sharing across machines (git).** Point `LEARNINGS_FILE` at a file in a git
repo and Silkworm can sync learnings:

```sh
silkworm learnings init git@github.com:you/silkworm-learnings.git
# set LEARNINGS_FILE=~/silkworm-learnings/learnings.json in .env, then:
silkworm learnings sync        # commit + pull + push (or the ⇅ button)
```

Concurrent edits from two machines are reconciled by a **union merge** (dedup by
learning id), so appending on both sides never produces a conflict to resolve by
hand. Set `LEARNINGS_AUTOSYNC=1` to push automatically after each harvest.

## Thread summaries

Every thread carries a short **context summary** — one to three sentences on
what the conversation is about and where it got to — written by a cheap model
(`SUMMARY_MODEL`) from the session transcript and refreshed in the background
after each turn. It shows under the thread name in the visualizer's sidebar and
in full at the top of the transcript, so a list of nine threads reads as nine
descriptions instead of nine timestamps.

A watermark means a thread is only re-summarized when it actually has new
activity. Backfill or rebuild them all:

```sh
silkworm summarize            # only threads that don't have one yet
silkworm summarize --force    # rebuild every summary
```

The ↻ button on a thread regenerates just that one, and ✎ renames it (blank
input generates a name from the summary). `silkworm titles` backfills names for
every untitled thread.

A summary written before later turns is marked **stale** in the UI, so you can
tell a current description from one the summariser missed.

## Surviving a restart mid-turn

A turn runs inside the bot, but Claude runs in its own process group — so
restarting the bot (a deploy, a crash) leaves the child working with nobody
reading its output: the reply is lost and the placeholder message stays frozen.

Silkworm records a marker on the session before each turn. On the next start it
walks those markers, waits for any still-running child, and **recovers the reply
from the session transcript**, posting it into the frozen placeholder with a
note that it was recovered. If the turn produced nothing, it says so and clears
the ⏳ instead of leaving the thread looking busy forever. Either way no thread
is left stuck, and `silkworm restart` is safe to run at any time.

The other half of a restart is Slack **redelivering** an event whose ack died
with the old process. Each thread records the timestamp of the last message it
picked up, and since timestamps within a thread only move forward, anything at
or behind that mark is a redelivery and is ignored — so a restart can't run the
same message twice. The two work together: the watermark suppresses the
duplicate run, recovery supplies the reply the interrupted run owed you.

## When a turn goes wrong

The dashboard is the place to notice it. Each thread reports how long its
in-flight turn has been going and whether a `claude` process is really behind
it, so the sidebar reads `running 3m` or `stalled 24h`, and a banner at the top
of the page counts stalled threads and cost spikes (a turn costing far more than
that thread's own median — how a prompt-cache regression shows up). **Release
thread** kills the child, clears the marker, finalizes the frozen placeholder
and drops the stale ⏳ — `!stop` can't, because a turn orphaned by a restart is
no longer owned by the bot.

Threads also keep a short event log — recovered, interrupted, timed out, reaped,
released — so an interrupted thread doesn't look merely quiet.

## Projects

Tasks can be filed under a **project** — "Asia Trip", "Trader", whatever the
body of work is. Projects are created the first time you name one; there is no
setup step.

```
!project Asia Trip     # in a Slack thread: its tasks are filed here from now on
!project none          # unfile
!project               # what this thread is filed under, and what exists
```

A project that is a codebase starts in one step:

```
!project new Widget -- makes widgets             # ~/workspace/widget
!project new "Silk Swing" ~/code/swing --adopt   # an existing directory
!project new Widget --github -- makes widgets    # ...and a PRIVATE GitHub repo
!project github create                           # that repo, later, for this thread's project
```

It makes (or, when you name its path or say `--adopt`, adopts) the directory,
`git init`s it with a first commit if it is not a repo, starts a CLAUDE.md
holding the title and the purpose, registers it, and files the thread under it
and moves the thread there. A GitHub repository is only created when asked —
`--github`, the dashboard form's box, or `!project github create` — and its URL is
reported. A name that is already a project, a directory that already exists
(without `--adopt`), or a path inside another repository's working tree is
refused. `SILKWORM_GITHUB_OWNER` picks the account; blank means `gh`'s own.

**Filed by directory.** A thread or task working inside a registered project's
directory is filed under that project without being told: on each new turn,
and once at startup for existing threads and tasks. The most specific project
wins (`~/workspace/x` over `~/workspace`), Silkworm's scratch folder does not
count as Silkworm's, and a thread you unfiled with `!project none` stays unfiled.

**Unregistered repos.** Git repositories directly under `~/workspace` and
Silkworm's scratch folder that no project covers (by directory, or by sharing
a registered remote) are listed in `silkworm status`, the daily digest and the
dashboard's task panel, so a new one never goes unnoticed. `.archive`,
`.worktrees` and other hidden directories are not scanned.

A project is **what a task belongs to**; `scope` is **where it may act**. They
are separate deliberately — two projects can share a repo, one project can span
several, and plenty ("plan the trip") have no repo at all. A project may carry a
default scope that its tasks inherit, so work on a codebase lands in the right
directory without repeating it.

Each project carries a short **brief** — what's been decided, constraints,
preferences — injected into every task filed under it. Tasks get their own
sessions, so without this each one would start cold; the brief is how a project
remembers without tasks sharing a conversation. It rewrites itself as tasks
complete, and `!brief` reads or sets it by hand.

This matters most for projects with no repo: a codebase project already
accumulates **learnings** scoped by git remote, but "plan the Asia trip" has no
remote to scope to.

In the dashboard the Tasks panel gets a project filter, with the count of what
needs you per project. It remains a **lens, not a new default**: the panel still
opens on what needs you across everything.

**Each project keeps a brief.** A project without a repo gets a directory under
`~/workspace/projects/<slug>/` — where its files live anyway — containing a
`CLAUDE.md`. Tasks filed under the project run there, so Claude Code loads it
automatically; there is no injection step. It writes itself: after each task, a
cheap model *rewrites* it (never appends) from what happened, so it stays a
living paragraph rather than a growing log. `!brief` reads or sets it, and it is
an ordinary file you can edit or put in git.

Repo-backed projects are left alone — they already have learnings and a
`CLAUDE.md` of your own.

## Mail

Set `GMAIL_USER` and `GMAIL_APP_PASSWORD` in `.env` (an
[app password](https://myaccount.google.com/apppasswords), not your account
password) and Silkworm polls every `GMAIL_POLL_MIN` minutes. `silkworm email`
runs a pass on demand.

It is **read-only**: mailboxes are opened readonly and bodies fetched with
`BODY.PEEK`, so nothing is ever marked read, moved or deleted.

### Labelled mail becomes project facts

Label a message in Gmail with the name of one of your projects and Silkworm
reads it for **durable facts** — flights, hotels, reservations, tickets,
appointments, confirmation numbers — and appends them to that project's
`logistics.md`, saving any attachments alongside. The project's `CLAUDE.md`
points at the file, so a task working on that project has it available without
carrying an itinerary in every prompt.

The label does two jobs at once: it says *look at this* and it says *this
belongs to that project*. There is no classifier to tune and no guessing about
which project a message is for, which is why there is no junk problem — you
only see what you labelled.

    !project asia-trip        file this thread under a project
    !project mail Travel/Asia  draw from a differently-named label

The label defaults to the project's title, so a label you already keep needs no
setup. Only projects with a Silkworm-owned directory take part; a repo-backed
project's files are yours. Extraction fails closed, and ordinary correspondence
files nothing — a booking is never turned into a task, because a confirmed
flight asks nothing of you.

### Inbox triage (opt-in)

`GMAIL_TRIAGE=1` additionally watches `GMAIL_MAILBOX` and **proposes** tasks for
mail that looks like it needs you. Off by default: you already read your inbox,
so re-surfacing it mostly duplicates work you do anyway. Only sender, subject,
date and a ~400 character snippet go to the model. Everything it finds arrives
as **`proposed`**, never queued, so a mediocre guess costs an accept/dismiss
rather than unwanted work.

## Tidying the thread list

Threads accumulate, especially the one-off threads task runs narrate into. Each
card in the dashboard has a **×** to hide it, a `hidden` button brings them
back, and **tidy** hides task runs untouched for a fortnight in one go.

Hidden, never deleted: the record keeps its title, summary, cost history and
files, so putting a thread away costs you nothing and is one click from undone.
The bulk tidy leaves conversations alone — a quiet one may still be one you come
back to — and never touches a thread with a turn in flight.

## Nightly review

Point it at a project and it will look the project over out of hours. From the
dashboard, the 🌙 strip in the 📋 Tasks panel lists every project and what it is
scheduled for — click one to set a time or turn it off. Or from Slack:

    !project ideate 02:00      # or 2am; `!project ideate off` to stop

Each night it reads the code, history and tests, and files what it finds as
**proposed** tasks — on your board, waiting. Accept the ones worth doing and
they queue like any other work, running in their own checkouts with a reviewer
before they can complete. Dismiss the rest.

The reviewer is read-only by enforcement, not instruction: it can read and file
proposals, and cannot edit, commit or push. It's told that finding nothing is a
good outcome, and capped at two proposals — refused at the third, not merely
asked for. A pass that must produce something produces busywork, and busywork
costs you a decision.

It never acts on its own findings. Acceptance is always a person.

## Isolated checkouts for queued work

A task filed against a repo-backed project runs in **its own `git` worktree**,
not your checkout — its own files, its own branch, sharing history. So it can
never leave your working tree dirty or on another branch, and it doesn't queue
behind a conversation about the same repo.

Its commits go on `silkworm/<task-id>`, and the reply names the branch and how
many commits it made. By default that branch is where the work stays for you to
review. A project with auto-merge on can also fast-forward its base branch
without asking you. See [Verifying and landing work](#verifying-and-landing-work). A worktree holding
uncommitted changes is left on disk and reported rather than removed, so
unfinished work is never tidied away — and ones orphaned by a restart are swept
periodically.

Conversations are deliberately *not* isolated: a worktree can't see uncommitted
changes in your main tree, so "fix what I'm working on" needs to run where you
are actually working. That's recorded on the task when it's created, not
inferred from who happens to run it — a message orphaned by a restart is handed
to the runner so it isn't lost, and it still runs in your checkout.

## Verifying and landing work

A queued implementor task does not finish when it says it has. What happens next
depends on two per-project settings, both off by default: a **test command**
and **auto-merge**. With neither, the task ends with its branch and nothing else.
With both, Silkworm can fast-forward your base branch **on its own**, with nobody
clicking anything. Nothing else in Silkworm writes to your base branch.

The whole chain, for one implementor task:

    implement → verify (your tests) → review → land (auto-merge only)

### 1. Give the project a test command

In a Slack thread filed under the project (`!project <name>` first):

```
!project test ./bin/silkworm test    # set it
!project test                        # show it
!project test off                    # clear it
```

Or from anything that can reach the bot on localhost (port `APPROVAL_PORT`,
default 8787):

```
curl -s 127.0.0.1:8787/projects -H 'Content-Type: application/json' \
  -d '{"action":"test-cmd","slug":"trader","cmd":"make test"}'
```

What `verify.py` does with it:

- It runs the command in the task's own checkout once the implementor's turn
  ends, **before** the review and before that checkout is released. No model is
  involved: this is a subprocess and its exit code.
- The command is split like a shell would split it (`shlex`) but is **not run
  through a shell**. Pipes, `&&` and redirects won't work. If you need them, put
  them in a script and point `test_cmd` at the script.
- **Exit 0 means pass.** Any other exit code, or no result within 30 minutes,
  means fail. The reply includes `🧪 Tests pass.` or `❌ Tests fail (exit N).`
  A timeout shows as `exit None`.
- **Failing work goes back to the implementor** with the last 2000 characters
  of output attached and an instruction to fix the change rather than the
  tests. It is sent back at most `MAX_VERIFY_ATTEMPTS` times (default 2). If it
  still fails after that, it goes to **awaiting_approval** for you to look at.
  Approving it then closes the task but does not merge it (see below).
- A missing binary or an unparseable command counts as **could not run**, not as
  a failure. The reply says `❔ Not verified — could not run …`.

**A project with no test command is never verified and never auto-merged.** Its
replies say `❔ Not verified — no test command is configured for this project`,
and its work still goes to the reviewer. The reviewer is read-only and can't run
anything, so without a test command nothing has shown that the code works.
Having no suite is not the same as passing one, and Silkworm doesn't treat it
that way.

### 2. Turn on auto-merge

There is no Slack command or dashboard button for this. Set it on the bot's
local `/projects` route:

```
# on
curl -s 127.0.0.1:8787/projects -H 'Content-Type: application/json' \
  -d '{"action":"auto-merge","slug":"trader","on":true}'

# off
curl -s 127.0.0.1:8787/projects -H 'Content-Type: application/json' \
  -d '{"action":"auto-merge","slug":"trader","on":false}'
```

The dashboard proxies the same route at `http://127.0.0.1:8790/api/projects`.
Turning auto-merge on is **refused unless the project already has a test
command** (`set a test command first — nothing may land unproven`). Clearing the
test command afterwards doesn't turn auto-merge off, but every landing will then
refuse at `no-test-command`.

**Only Silkworm lands.** In every ready project (test command *and* auto-merge),
the bot installs two git hooks into the repo's shared hooks directory at startup
and whenever the project becomes ready (`git_guard.py`). An implementor task's
turn runs with `SILKWORM_TASK_ROLE=implementor` and `SILKWORM_TASK_ID` set, and
under that role git refuses every push (`pre-push`) and any ref update except
the task's own `silkworm/<task id>` branch, `HEAD`, remote-tracking refs and
tags a fetch follows (`reference-transaction`). Nothing else carries the role,
so your own git and the bot's landing, verification and releases are
unaffected. A hook already in place is kept and chained as
`<name>.silkworm-chained`. The installer refuses, and `silkworm status` reports
the project unguarded, for a relative or in-tree `core.hooksPath`, or for an
existing hook that dispatches on `$0` (husky). This stops a well-meaning agent,
not a determined one: `git push --no-verify` skips `pre-push`.

Before relying on it, check that:

- **The project points at the repo.** `!project <name>` sent from a thread
  whose working directory is the repo records that directory as the project's
  `scope.cwd`. That checkout is the one landings merge into. Running
  `!project <name>` again **replaces** the project's whole scope with the
  thread's directory, which also clears a base set with `!project base`.
- **The checkout is on the base branch and has no uncommitted edits to tracked
  files.** Silkworm won't switch branches for you, and it won't merge over your
  work in progress. Untracked files don't count.
- **The base is the one you want.** By default it's the remote's default branch
  (`origin/HEAD`), else `main`, else `master`. `!project base <branch>` sets
  another one, and `!project base default` puts it back. A task copies the
  project's scope when it is created, so a change applies to tasks filed after
  it, both when they start and when they land. Tasks already on the board keep
  the base they were filed with.

`!project test` is the quickest way to check that the command is set. To see
every setting, list the projects: `-d '{"action":"list"}'`.

### 3. What a landing does

A landing runs when **the review passes**. It also runs when you **Approve** a
task that is in `awaiting_approval`, from the dashboard or the Slack Home tab,
because approving flagged work counts as a decision it is fine. Approving a task
that is still `blocked` on its review doesn't land it.

Before it touches git, the work must pass four checks. The project has
auto-merge on, the project has a test command, **this task's tests passed**, and
the task has a checkout to land from. If any of those is missing, the work was
never a candidate.

After those checks, the order is fixed (`merge.land()`). Each step runs only if
the previous one succeeded:

1. **Check the base checkout.** Refuse if it has uncommitted changes to tracked
   files, if no base branch can be resolved, or if the checkout is on a
   different branch. Otherwise read the exact commit the base is on.
2. **Rebase** the task's branch onto that commit, in a separate `land` checkout.
   A conflict aborts the rebase and refuses.
3. **Retest** the rebased branch with the project's test command.
4. **Recheck** that the base is still on the commit it was rebased onto. The
   suite takes minutes, and something else may have moved the base meanwhile.
5. **Fast-forward only** (`git merge --ff-only`). A merge commit is never made,
   and neither is a conflict resolution nobody reviewed.
6. **Retest the base itself.**
7. **Revert if that broke it.** If the post-merge tests fail, the base is
   `reset --hard` to exactly the commit it was on before, and the landing
   refuses.

A landing takes the same checkout lock a conversation turn does, so it never
runs alongside a turn in that repo.

A successful landing replies `:shipit: Landed on main (a1b2c3d4, local only, not
pushed)` and completes the task. The new commit is stored on the task record as
`result.landed`. **Landing moves your local branch only.** Nothing is pushed
unless the project also has `publish` on:

```
curl -s 127.0.0.1:8787/projects -H 'Content-Type: application/json' \
  -d '{"action":"publish","slug":"trader","on":true}'
```

That is refused unless auto-merge is on. With `publish` on, Silkworm pushes the
base to `origin` last, after the post-merge tests. If the push fails, the landing
stays and the reply says it wasn't pushed, with git's error underneath. To check
whether a push that reported failure actually took, Silkworm asks `origin`
instead of trusting the exit code.

### When a landing refuses

**Every refusal leaves the branch in place.** The task's commits stay on
`silkworm/<task-id>`, and the base is unchanged (with one exception,
`interrupted`, below). That branch is named in two
places:

- **The Slack reply** in the task's thread:
  `✋ Not landed (rebase). Branch silkworm/tsk_… is waiting for you.`, followed
  by the reason and git's or the suite's output.
- **The task record**, under `result.landing`: `stage`, `detail`, `branch`,
  and whether it was `eligible`. The dashboard's task row shows it as
  `not landed (rebase) — silkworm/tsk_… is waiting for you`. It survives task
  compaction.

If the landing refused **after a passing review**, the task is parked in
**awaiting_approval**, with the reason `review passed but the landing refused
(<stage>)`. It is not marked done. After fixing the cause (committing your
edits, switching the checkout back, resolving the conflict), **Approve** retries
the landing. Approving always completes the task, whether or not it lands. The
outcome goes on the record.

The stages, and what each means:

| Stage | Meaning |
|---|---|
| `base-dirty` | Your checkout has uncommitted edits to tracked files |
| `base-unresolved` | No base branch could be found: no `main`, no `master`, no remote. Set one with `!project base` |
| `base-branch` | Your checkout is on another branch than the base, or the base's commit couldn't be read |
| `attach` | The task's branch couldn't be checked out, e.g. the task made no commits or wasn't isolated |
| `rebase` | Rebasing onto the base conflicts. The rebase is aborted |
| `tests-after-rebase` | The change passed alone but fails on top of the current base |
| `base-moved` | The base moved while the tests ran. The reply names both commits |
| `merge` | The branch isn't a fast-forward of the base |
| `tests-after-merge` | The base failed its tests after the merge, and was put back |
| `errored` | The landing itself raised an error. See the bot log |
| `interrupted` | A restart killed the landing. The record says nothing was merged, but if the restart came during the post-merge tests, the base may already be fast-forwarded and not reverted. Check it |
| `in-progress` | Not a refusal: an Approve-started landing is running. The dashboard shows `landing…` |

A rebase changes the branch itself. If a landing refuses after step 2, the
branch you find has already been rebased onto the base.

A task that was **never a candidate** is different. Its project has a test
command but the tests never passed, or it has no checkout. That task completes
normally, and the reply says `✋ Not landed: <reason>.` with the same reason
stored on the record. A project that never turned auto-merge on gets neither the
reply nor the record: its branch is simply the deliverable.

## Watching something over time

Ask for "keep an eye on the deploy and tell me when it's done" and Silkworm
does not sit in a turn waiting — that would hit the 15 minute cap, lock the
thread meanwhile, and die on the next restart. It finishes the turn and
schedules a later one on the same conversation:

    silkworm defer 10m "check whether the deploy finished"

The wake-up waits as a `blocked` task with a `retry_at`, costing nothing and
surviving restarts, then resumes the thread with its context intact. If there
is nothing to report it re-schedules and stays **silent** — no message at all.
When something has actually happened, it says so.

Chains cap at 24 wake-ups, and delays run from 30s to 24h. If a check reports
nothing but fails to schedule the next one, the watch surfaces in `needs_input`
rather than quietly ending.

Because a silent watch is indistinguishable from a forgotten one, you can ask
what it is actually waiting on:

```sh
silkworm watching
#      in 8m  tsk_6733ab6f4d  check whether /tmp/job-done exists
#              thread D0BH336V73L:1787708667.829329 · check 1 of 24
```

Only scheduled wake-ups — a quota-blocked retry is the system waiting on
itself, not a promise made to you.

## Moving a thread to the terminal

Every thread is a normal Claude Code session, so it works both ways: send
`!terminal` in a thread and Silkworm replies with the exact
`cd … && claude --resume <session-id>` to continue it interactively. Turns you
take in the terminal become part of the thread's history — the next Slack
message picks up right where you left off.

`!terminal` also *checks the thread out*: while checked out, Slack messages are
held with a warning instead of running (an interactive session loads history
once at launch, so Slack turns would be invisible to it). The checkout releases
itself automatically — run `python3 install_hooks.py` once to add
SessionStart/SessionEnd hooks to your `~/.claude/settings.json`, and the bot
gets notified when your terminal session opens and exits, reclaiming the thread
(with a note in Slack) the moment you quit. Escape hatches: `!back` reclaims
manually, `!takeover` force-closes a live terminal session (SIGTERM — completed
turns are already saved; only an in-flight generation is lost). Set
`HANDOFF_SLACK_WINS=1` to skip the warning entirely and have any Slack message
auto-close the terminal and take over.

## Starting a session from the terminal

The reverse direction also works — start in the terminal, continue in Slack:

```sh
./bin/silkworm "refactor auth" --dir ~/code/myapp
# add to PATH for convenience: ln -s ~/workspace/Silkworm/bin/silkworm /usr/local/bin/
```

The CLI registers the session with the bot, which posts an anchor message in
`SILKWORM_HOME_CHANNEL` (default: your most recent DM with the bot) — so the
session has a Slack thread and shows in the visualizer from the moment it
starts. Exit the terminal and the thread reclaims itself; reply in Slack to
keep going. If the bot is down, the session starts untracked.

## The task board in Slack

Away from the house, the dashboard is out of reach -- and it is where decisions
wait. The app's **Home tab** carries the same "what needs me" board: work
awaiting approval (with the reviewer's findings), tasks that asked you
something, failures and proposals, then what is running and what is scheduled
to wake. Open it by clicking the app's name in Slack, on any device.

Its buttons are the dashboard's, state for state, and go through the same
route, so Approve still lands the branch and Cancel still stops a running
agent. Approve and Dismiss ask first. A button only acts if the task is still
in the state it was drawn for; if it has moved on since, the tab says so and
does nothing. People off `SLACK_ALLOWED_USERS` see none of it.

It is the attention view, not the backlog: a large board is cut to fit Slack's
limits and says how many are left for the dashboard.

Needs `home_tab_enabled` and the `app_home_opened` event, both in
`manifest.json`; an app created from an older manifest needs the new one pasted
in (below).

## Reaching the dashboard from another machine

The visualizer binds `127.0.0.1` only, and it should stay that way: it has no
authentication, it can relay prompts into threads, and the bot trusts any
localhost caller as the machine owner (bypassing the Slack allowlist). On a host
running `--dangerously-skip-permissions`, exposing that port to a LAN hands
arbitrary command execution to anything on the network.

So tunnel instead. On the machine you browse *from*:

```sh
./bin/silkworm-tunnel install ryansharp@your-mini.local
```

That installs a launchd agent holding an SSH tunnel to the dashboard port, which
re-dials on sleep, Wi-Fi changes and reboot, so `http://127.0.0.1:8790` keeps
working without you re-running anything. `silkworm-tunnel status` checks it,
`uninstall` removes it. It needs key-based SSH that works without prompting —
the installer verifies that first and tells you how to fix it, because a launchd
agent has no terminal to answer a passphrase prompt.

**The host is resolved on every dial, not fixed at install time.** A DHCP lease
change used to break the tunnel permanently and silently, because the plist
baked one address into the ssh command line. The agent now runs a small dialer
that tries, in order: the host you installed with, the mDNS `.local` name,
whatever answered last time, and finally an ARP lookup by hardware address.

### If the host's address keeps moving

A DHCP reservation on your router is the real fix, but there is a trap on
macOS: **Private Wi-Fi Address** means the router never sees the machine's
hardware MAC, only a randomised one — so a reservation made against the address
`networksetup` reports will silently never match. Check which is actually in
use before reserving anything:

```sh
networksetup -listallhardwareports | grep -A2 Wi-Fi   # hardware MAC
ifconfig en1 | grep ether                             # what the network sees
```

If they differ, either turn Private Wi-Fi Address off for that network (System
Settings → Wi-Fi → Details) and reserve against the hardware MAC, or reserve
against the randomised one and accept that it may rotate. For an always-on
host, Ethernet sidesteps the whole thing — no randomisation, stable MAC.

For phone access, or from outside the network, Tailscale is a better fit than a
tunnel: install it on both ends and the host gets a stable private address.

If you would rather bind the dashboard directly, it requires a token and
refuses to start without one:

```sh
VIZ_BIND=0.0.0.0 VIZ_TOKEN=$(python3 -c 'import secrets;print(secrets.token_urlsafe(32))')
# then open http://host:8790/?token=... once; it sets a cookie and remembers.
```

## Session visualizer

```sh
python3 visualizer.py    # http://127.0.0.1:8790
```

A local web dashboard for every thread session (localhost-only):

- **Cost & cache analytics** — stat tiles (total spend, 14-day tokens, cache
  hit rate) and a daily stacked bar chart of cache-read / fresh-input / output
  tokens, with hover tooltips and a table view. Cache misses show up as blue
  bars — if the gold disappears, something is invalidating your prompt cache.
- **Live status badges** — running / checked out / in-terminal per thread,
  polled from the bot; a dot in the header shows whether the bot is up.
- **Transcripts** — markdown-rendered messages with per-turn token usage and
  cache-hit %, collapsible tool calls, results, and thinking.
- **Full-text search** across all thread transcripts.
- **Files** — everything exchanged with a thread (Slack uploads in, generated
  files out) listed with download links; outbound files are archived under
  `artifacts/`.
- **Reply from the web** — a composer that relays messages (and `!commands`,
  wired to the Stop / Take over / Model / Reset buttons) through the bot, so
  everything also lands in the Slack thread.

## Notes & limits

- The bot runs Claude as *you*, on *your* machine. In the default `skip` mode
  anyone who can message the bot can run arbitrary commands as your user — set
  `SLACK_ALLOWED_USERS`, or use `CLAUDE_APPROVAL_MODE=slack`, in any workspace
  you don't fully trust.
- Slack's free plan hides messages after 90 days, but Claude session history
  lives locally in `~/.claude`, so old threads still resume.
- Messages within one thread run serially (a session is single-turn-at-a-time);
  different threads run in parallel.
