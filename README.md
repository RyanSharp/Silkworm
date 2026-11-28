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
| `!operator` / `!operator where` | Make this thread the operator's home / say which thread is (see [The operator](#the-operator)) |
| `!approve <what> <item>` / `!approve` | Let the operator do one held-back thing once / list what is approved |

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
login and restart on crash, plus the **watchdog** (`com.silkworm.watchdog`,
below). Service definitions are generated from your actual paths at install
time — clone anywhere.

Day-to-day management (macOS today; Linux/systemd planned):

```sh
silkworm status      # services, bot, visualizer, hooks, revision — one health check
silkworm deploy      # promote HEAD without killing work: drain, wait, restart, confirm — or roll back
silkworm restart     # bounce both services now, killing whatever turn is running
silkworm logs        # tail the bot log
silkworm uninstall   # remove the services (repo and sessions untouched)
```

`silkworm deploy` is the one to load new code with. It refuses unless the
checkout is clean on its base branch and `silkworm test` passes, then asks the
bot to drain (the queue runner claims nothing new; running turns carry on),
waits until nothing has been running for 30s, promotes HEAD (below), and
checks the bot is back, connected to Slack, and running HEAD. It gives up
without restarting after `--max-wait` (default 2h); `--now` skips the wait. A
queued task a restart does interrupt is resumed in its own session and
checkout when it next runs, rather than started over.

### Promotion: what runs is a pinned commit, and a bad one rolls itself back

On 2026-10-26 a deploy restarted the bot onto a commit whose `bot.py` raised
`NameError` at import (a route registered before its handler was defined).
The suite passed — it lifts functions out of `bot.py`, nothing booted it — the
deploy saw "not answering after 90s" and stopped, and its failure note sat in
`outbox/` because the bot is what posts. Silkworm was down for 18 days. So:

- **Every change must boot before it lands.** `silkworm test` starts `bot.py`
  and `visualizer.py` as real processes (`smoke.py`): a scratch state
  directory (`SILKWORM_HOME`), spare ports, a loopback stand-in for Slack
  (`SILKWORM_OFFLINE=1`, which refuses anything but loopback) and tokens that
  are not tokens. It passes only when `/status` answers with the revision it
  booted and the dashboard answers; an import error, crash or timeout fails
  with the tail of stderr. The landing gate and the deploy preflight both run
  the suite, so both run this. A static check also refuses any name the
  module level reads before it is defined. `python smoke.py [dir]` runs the
  boot on its own.
- **The services run a pinned checkout, not this one.** `bot.py` and
  `visualizer.py` run from a detached git worktree beside the install —
  `~/workspace/.Silkworm-live` (`SILKWORM_LIVE_DIR` overrides) — that only a
  promotion moves. Landings, turns and hand edits in the main checkout change
  nothing that runs. launchd passes `SILKWORM_HOME` so state files, `.env`,
  `logs/` and `workspace/` stay in the main checkout. `live.json` records what
  is pinned and the **last known good** revision.
- **deploy promotes, and rolls back.** After the drain, deploy first boots
  HEAD exactly as committed in a throwaway worktree (so an untracked file in
  this tree cannot make it pass) and refuses — nothing restarted, drain lifted
  — if that does not come up. Then it checks HEAD out in the pinned checkout
  and restarts onto it — the boot on the real port,
  once the old process has let go. If it answers `/status`, reconnects to
  Slack and reports HEAD within 90s it becomes the last known good. If not,
  the pin goes back to the last known good (or, the first time, whatever was
  running before), which is restarted and confirmed in turn. Both outcomes are
  printed and posted, and the outage — start, end, the bot's stderr tail, what
  was rolled back — is written to `outages.json`. A deploy's note to its
  thread goes straight to Slack with the bot token (and to your phone) when
  the bot cannot post it; only failing that, to `outbox/`.
- **The watchdog notices what nothing else would.** `com.silkworm.watchdog`
  is a launchd `StartInterval` job (every 2 minutes, not `KeepAlive`, not part
  of `bot.py`, stdlib only) running `watchdog.py` from the main checkout. When
  `/status` has not answered for 3 checks in a row (`WATCHDOG_DOWN_CHECKS`),
  or answers with a new pid every check (a crash loop), it records the
  outage, rolls the pin back to the last known good (or, when the last known
  good is what is failing — it passed its deploy and broke later — the one
  before it) and kickstarts the services — once per outage, never onto the revision already running, never
  twice in a day, and never while a deploy holds its lock — then tells you
  directly: `chat.postMessage` to your home DM with the bot token, plus a web
  push. It repeats at most every 6 hours while still down, and says when the
  bot is back. Its own log is `logs/watchdog.log`.
- **Outages are reported once the bot is up.** The daily digest opens with
  any outage in its window, `silkworm status` shows the pin, the watchdog and
  the last outage (read from files, so it works with the bot down), and
  `status --json` carries `live`, `outages` and `outage_open`. The mini's
  `revision` health check warns when the bot runs uncommitted code, code that
  is no commit, or anything other than the pin.

The first `silkworm deploy` (or `silkworm install`) after this creates the
pinned checkout at HEAD and rewrites the service definitions to run it; the
restart also adds the watchdog job if it is missing. That first deploy has no
known good revision to fall back on (the bot running before it booted from an
uncommitted tree, whose commit cannot boot), so the clean boot of HEAD is its
only guard. `deploy --skip-tests` skips the suite's boot test, not the clean
boot of HEAD, and the rollback still applies.

Tip: put it on your PATH — `ln -s ~/workspace/Silkworm/bin/silkworm /usr/local/bin/silkworm`.

`silkworm status` reports the git revision the bot is *running*, not the one in
the checkout, and fails when the two differ. Silkworm auto-merges onto its own
main and nothing restarts it, so a landed fix sits in the main checkout until
`silkworm deploy` promotes it — which is not otherwise visible from anywhere.
The dashboard's alert bar says the same thing.

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
| `OPERATOR_SWEEP_AT` | `08:30` | Local time of the operator's daily sweep, once the digest has gone out; caught up after a restart; `off` to stop (see [The operator](#the-operator)) |
| `HEALTH_LEAD` | `15m` | Project health checks run once a day, this long before `OPERATOR_SWEEP_AT` (08:15 by default), so the sweep's brief reads that morning's results; still at that time when the sweep is off (anchored to the default sweep time); caught up after a restart, and the resolved schedule is logged at startup. `0` runs them at the sweep's own time, `off` stops them. Replaces `HEALTH_EVERY`, which is no longer read (a leftover one is warned about at startup). Checks (health.py; first `ci`: the latest GitHub Actions run of each workflow on the base branch, via `gh`): a failing one is one needs-you item per project and check, closed when it passes; `unknown` (gh missing, offline, rate-limited) never pages. Project-specific probes (health_probes.py, mapped by the explicit table in `health.toml`): trader `dispatching`, `gateway`, `healthcheck`; Supabase `backend-drift` (also flags production schema no base-branch migration creates, and skipped migration numbers -- a hand-applied, unrecorded migration); App Store `app-store`, `testflight`; and the project `mini` (Mac mini, registered at startup if absent, home Silkworm's checkout)'s `credentials`, `disk` and `tailscale` (the phone's way to the dashboard: tailscaled running and logged in, MagicDNS and HTTPS on, serve forwarding this machine's current name to the visualizer, Funnel off; the same tailnet.py `silkworm tailscale status` reads). Results in the sweep's brief, the digest, the dashboard, the operator's context and `silkworm health` (`--probe` runs them now without recording, refused to restricted roles; `--brief` prints the sweep's brief) |
| `HEALTH_WARN_DAYS` | `3` | A check warning this long may get one fix proposal from the operator (`silkworm task --propose --health <project>/<check>`); a failing one may at once. Once per (project, check) and trouble, never while one is open, and through the ordinary proposed gate, caps and dedup. Checks whose trouble is the owner's decision (a backend release, App Review, a credential, a login, disk, Tailscale on the mini) are stated as decisions and never filed |
| `SILKWORM_BOARD_CHANNEL` | `silkworm-board` | Private channel the task board is kept in, by name or id (see [The task board in Slack](#the-task-board-in-slack)); must be private with only the bot and allowlisted members, or the board is taken down |
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

**Projects board.** The dashboard's 🗂 **Projects** button opens one card per
active project — tasks by state, what is running, the last landing, unmerged
branches, readiness (test command, auto-merge, publish), this week's cost at API
list price, and, with a `.silkworm/release.toml`, what each target has ready to
release (read only; no preview runs). A card opens the project's board:
Backlog, Running, In review, Needs you and Done (14 days), each card carrying the
task panel's own actions and opening to the full goal, events, review and
landing record. Filters cover project, role, state and text; an **Unfiled** lane
holds tasks and conversations under no project. The branch survey and release
plans behind it are cached for a minute (`board.py`), so polling does not walk
git. 📋 Tasks still opens on what needs you.

**Conversation turns stay with their threads.** Every message a thread takes —
a Slack or dashboard chat message, a scheduled wake-up, the operator's sweep —
is recorded as a task (restart recovery, cost and the turn lock need it), but it
is the conversation, not work. The board, the overview, 📋 Tasks, the Slack
board, the digest and `silkworm tasks` leave those out and count without them;
a running turn shows as its thread's running indicator. One waiting on you — a
step it handed you, or a turn that failed — is still in Needs you. What counts
as a turn is one predicate, `tasks.is_turn`: source `slack`, `defer`, or `ui`
with a message ts, and not an implementor, reviewer, researcher or ideator
(anything filed with `silkworm task` is work). **Show conversation turns** on
the board lists them too (remembered per device, off by default);
`silkworm tasks --all` does the same on the command line.

**Releasing in one click.** A project with release targets has a
**Release…** button on its card and its board header: per target what is
pending and its first commits, a patch/minor choice each (major or an exact
version only when one target is released on its own, as `!release` has it),
the dependency order, and each target's `preview` commands run as it opens (a
migration dry run — never a deploy command). **Release…** then shows exactly
what will reach production — every command that runs, every ref and tag that
is pushed, and the target's optional `describe` line from `release.toml` — and
only **Yes, release** starts it, through the same `run_release` as `!release`;
progress is shown in the modal and posted to a thread in your DM. On the Slack
board, a project with something ready gets a menu with **Release…** (the same
plan and previews in a Slack window, everything pending at a patch) and
**Preview only…**. A release is refused, with the reason, from a dirty
checkout, behind or ahead of origin, with nothing pending, while another
release of that project runs, or for someone off the allowlist; and the start
carries a fingerprint of the plan that was confirmed, so if anything lands in
between, nothing ships and the plan is shown again.

The board also edits, through one kind of modal — labelled fields, Cancel or
Esc to back out with nothing sent, and the route's own reason shown inside it
when something is refused. **+ New task** in a project's board header files work
in that project (the project decides the directory): an optional title, what it
should do (the same length rules as filing from a conversation, said as you
type), Implementor or Assistant, and Queue now or Propose for later; it says up
front when an implementor task would be held because the project is not ready.
**+ New project** in the overview header (and in 📋 Tasks) makes one: name, what
it is for, `~/workspace/<slug>` or an existing folder, and optionally a private
GitHub repo, then opens its board. **Edit…** changes a task's title, goal,
project or role while it is still proposed or queued — a new project brings its
scope with it, and the change is recorded as an event; once work has run, Send
back with notes is the edit. **Run next** pins a queued task so the runner
claims it before older work (spent by that claim; reviews keep their own lane),
and Backlog lists the queue in the order it will run. **⚙ Settings** covers a
project's name, test command, auto-merge, publish, nightly review time and base
branch — which must exist, and is flagged if it is already merged into the
default branch — and the two ways to put a project away. **Archive…** hides it
from the overview and pickers, and its tasks and threads from every
all-projects view — the board, the inbox (which says how many it is leaving
out), the thread list (under **hidden**), the Slack board and the digest — while
running and queued work still shows (archiving does not stop the queue);
nothing is deleted, its settings stay, and its own
page and board still list everything. **Retire…** is for a project you are done
with: it archives it and dismisses its open tasks, listing them first, and is
refused while one of them runs; done tasks and their branches are left alone.
Archived projects sit in a collapsed **Archived** section to be brought back
(dismissed tasks stay dismissed). In Slack, `!project archive|unarchive [name]`
and `!project retire [name]` (then `… confirm`) do the same. Anything
destructive or that changes unattended behaviour asks first.

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

Each night looks through one lens, in turn: **features** (new capabilities
grounded in the project's brief and docs, each saying who it is for, how big it
is and what it costs to run), **quality** (refactors, tests on risky code,
tooling, drifted docs) or **correctness** (bugs, security, data integrity). The
rotation follows the date, so a skipped night moves on rather than repeating.
To look through only some of them:

    !project ideate lenses features,correctness   # `!project ideate lenses default` for all

The lens is in the pass's title, on each proposal it files, and in the digest.

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

## Research

File a question as role **researcher** (the dashboard's New task form,
`silkworm task --role researcher "<question>"` from a turn, or ask in a thread
to have it filed) when you want findings to read and decide on rather than a
change. It runs as `assistant` does -- nothing is reviewed or merged -- but
in its own worktree, unattended, so git holds it to its own branch as it does
an implementor (no push, no tag, no moving the base), and a branch it leaves
commits on can only be dropped: Land and Approve refuse work from a role that
is never reviewed. It may still write files and run things, such as caches
and backtests. When it finishes it waits in **Needs you** instead of closing as done, with
its findings in a fixed shape: a short answer, confidence and why, key
evidence, and 2-4 next steps written as standalone task goals.

Those next steps are filed as **proposed** children of the research task, in
its project, through the same gate as the nightly review: the per-pass budget,
the project's open-proposal cap and duplicate check all apply, and a run cannot
file them as anything but proposals. Any it could not file are listed on the
research task with the reason, not dropped. The task's row lists both.

On the board (dashboard, and the Slack board's menu):

- **Close** -- you have read them; it is done and nothing further runs.
- **Dig deeper…** -- asks for a follow-up question and sends it back with the
  question appended; it parks again with new findings. Cancelling the prompt
  sends nothing.
- **Discuss** -- opens its thread in Slack.

Each refuses a task that is running or has already moved on.

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

#### Files the build regenerates

If building the project rewrites tracked files (Cadence's xcodegen build
injects keys into both `Info.plist`s, which must never be committed), list them
so they never count as uncommitted work:

```
!project generated ios/Cadence/Info.plist ios/CadenceWidget/Info.plist
!project generated                   # show them
!project generated off               # clear them
```

The dashboard's project settings have the same field. With them set:

- A task checkout holding only those files is released when the task ends, and
  by the orphan sweep. The files are put back as committed first. Untracked
  files and any other change still keep the checkout, as before.
- A landing whose branch is still held by the task's own checkout releases that
  checkout and goes on, if it holds only generated files. If it holds real
  changes, the refusal names the checkout's path and the changed files.
- The base checkout doesn't count as dirty over them, and a landing never
  writes them there: your local copies, keys included, stay byte-for-byte as
  they were. If the branch itself changes one of them while your checkout holds
  it modified, the landing is refused (`base-generated`) instead.

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
and whenever the project becomes ready (`git_guard.py`). An implementor or
researcher task's turn runs with `SILKWORM_TASK_ROLE` set to its role and
`SILKWORM_TASK_ID` set, and under either role git refuses every push (`pre-push`) and any ref update except
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
task that is in `awaiting_approval`, from the dashboard or the Slack board,
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
runs alongside a turn in that repo (except while it waits for GitHub CI, below).

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

### 4. Gating on GitHub CI (`ci_gate`)

The project's test command runs on this machine. A project whose GitHub
Actions run elsewhere can also make **those** the gate, so main moves only once
GitHub's runs are green on the exact commit being landed. Cadence is the case
this exists for: its iOS `Tests` and `Backend tests` workflows used to run
after main had already been pushed, and in November 2026 three kinds of red
reached main that way.

```
!project ci all                      # every workflow that runs for the change
!project ci Tests, Backend tests     # just these, by name: or file name
!project ci                          # show it
!project ci off
```

or `{"action":"ci-gate","slug":"cadence","gate":"all"}` to `/projects`. It is
on (`all`) for Cadence and off everywhere else by default; turning it off sticks.

With it on, step 3 above is followed by:

- **Push the rebased commit to origin as `silkworm/<task-id>`**, never as the
  base. It is pushed with `--force`, and only ever under that name.
- **Wait for the required workflow runs on that exact commit**, polling
  `gh api` every 30 s for up to 45 minutes. This happens in the bot's landing
  thread, never inside a Claude turn, and the landing **lets go of the
  checkout lock while it waits** (another landing in the same checkout still
  waits its turn). The record says `waiting for CI` meanwhile, and the CLI
  (`silkworm tasks`, `tasks show`, `status --json`), the dashboard and the
  daily digest show it as that, not as `landing…`. Once the wait is over the
  checkout is checked again for uncommitted edits and for being on the base.
- **Only if every one is green** does the landing carry on to step 4: the
  fast-forward, the post-merge tests, and pushing main.

Which workflows count is read from the commit being landed, under
`.github/workflows/`, with GitHub's own rules for `branches`, `branches-ignore`,
`paths` and `paths-ignore` (needs `ruby`, which macOS ships, to read the YAML):

- A workflow whose path filter matches nothing the landing changes **counts as
  passed**, and the landing record (`result.landing.ci.skipped`) and its reply
  say so by name, with the reason.
- A workflow that **would run on pushes to the base but is not triggered by
  `silkworm/<task-id>`** refuses the landing as `ci-config`. Passing it would
  be the after-the-fact red all over again. `branches: ["*"]` is the usual
  cause: `*` does not match a `/`, so use `"**"`. A branch that fixes the
  trigger is judged by the workflow file it fixes it to.
- With `all`, a run that GitHub started for the push which the filters did not
  predict must pass too.

A pass on that commit is a pass whichever attempt it was, so a re-run on
GitHub counts. A failed run that was already there before this landing pushed
(an earlier attempt's flake) does not refuse the retry; the fresh run decides.
The branch must still be on the commit CI passed when the wait ends, or the
landing refuses as `branch-moved`. A refusal for a workflow that is also red
on the base's own commit says so (`ci.base_red`), since then the change may not
be what broke it.

After the landing, or a refusal other than a timeout, `silkworm/<task-id>` is
deleted from origin; dropping the task deletes it too. A copy an earlier
attempt left at another commit is deleted before the next push, so GitHub
filters paths against the task's own change rather than the base's.

The outcome is on the record under `result.landing.ci`: `commit`, `required`,
`passed`, `skipped`, the `failure` if any (workflow, job, step, test, URL),
and whether the pushed branch was `deleted`; `result.landing.ci_pushed` names a
branch still on origin.

**Auto-merge on a CI-gated project closes the task first.** The ordinary
auto-merge lands inside the review lane before closing the task, and parks it
in `awaiting_approval` if the landing refuses. Waiting up to 45 minutes there
would hold every project's reviews, so a gated project's landing starts on its
own thread once the review has passed and the task is closed, the way Approve's
does. A refusal is then on the record, the board (`not landed (ci) — … waiting
for you`, with Land and Drop) and the digest, not in the task's state. GitHub
reads only the first 300 changed files for `paths:` filters; a bigger change is
judged on all of them here and may time out if GitHub chose not to run.

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
| `ci-wait` | Not a refusal: the rebased commit is on origin as `silkworm/<task-id>` and the landing is waiting for GitHub CI. Shown as `waiting for CI` |
| `ci` | A required GitHub workflow failed on the rebased commit. The detail names the workflow, the failing job and step, the first failing test read off the job's log, and the run's URL. Nothing was merged |
| `ci-timeout` | CI had not finished after 45 minutes, or `gh` could not be reached (its last error is quoted). **Retryable**: the pushed branch is kept, so Land picks up the same runs |
| `branch-moved` | The task's branch moved while CI ran, so what passed is not what would merge |
| `ci-config` | The gate cannot be evaluated: origin is not on GitHub, a workflow can't be read, a named one doesn't exist, or one runs on the base but not on `silkworm/<task-id>` |

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

## Acting on the board from the command line

The dashboard's buttons are also commands, so a script or an agent can act on
the board with commands it can be allowlisted for rather than raw `curl`:

```sh
silkworm accept <id>                       # queue a proposal
silkworm dismiss <id> [--round R]          # cancel it
silkworm approve <id>                      # approve reviewed work, and land it (Approve)
silkworm cancel <id>                       # end unfinished work, stopping it if it runs
silkworm land <id>                         # land a finished task's branch
silkworm drop <id>                         # delete that branch, tagging its tip first
silkworm edit <id> [--goal G] [--title T]  # change work that has not run yet
silkworm edit <id> --recommend <action> --why W   # a suggestion on its Needs you card
                                           #   (accept, dismiss, approve, land, drop, close,
                                           #   dig, rework, retry, resolve, release; `clear`
                                           #   takes it off). It decides nothing, so the
                                           #   operator sets it without an approval
silkworm rework <id> --notes N             # send back with your notes (Send back)
silkworm close <id> [--round R]            # research: close the findings
silkworm dig <id> --notes N [--round R]    # research: Dig deeper with a question
silkworm retry <id>                        # requeue failed or cancelled work (Retry)
silkworm run-next <id>                     # put queued work first (Run next)
silkworm tidy <checkout path>              # remove a finished task's checkout (an hour
                                           #   old+) holding only generated files
silkworm release <project> [<target>|all] [--level patch|minor]
                                           # the Release button: its plan, then a start
                                           #   with that plan's confirmation
silkworm status --json                     # the board as one JSON document
```

Each is one POST to the bot's `/tasks` route with the action the button sends,
so it runs the same handler and the same `transition()`, and is refused for
exactly the same reasons — the CLI checks nothing of its own beyond its
arguments. `--round` is the research round a findings action was read for
(`round` in `status --json`), so a stale command is refused like a stale click.
`--by NAME` (default `cli`) is who the task's event log says acted, and
`--notes` what was decided (the operator must give it to act on your word; see
[The operator](#the-operator)). `release` makes the button's two requests, so it
is refused for the button's reasons too — a release already running, a deploy
draining, nothing pending — under the same lock; major and exact versions stay
with `!release` and the button, where the plan is read first.

Exit codes are stable: **0** done (one line on stdout, e.g.
`ok: accept tsk_… -> queued`), **1** refused (the bot's reason on stderr, e.g.
`refused: land tsk_…: only finished work; this one is queued`), **2** usage
error, **3** the bot could not be reached. An unknown id is a refusal
(`unknown task`). One caveat on 1: a handler that raised answers with nothing,
which is reported as `refused: … the bot gave no reason` -- check the task
before retrying, since the change may have been made before it failed.

These names are subcommands now, so a session titled with one of them needs
`--dir` first: `silkworm --dir . land the parser fix`. `status --json` lists at
most 200 queued, running or blocked tasks each, conversation turns included (the oldest 200: the bot's `list`
cap); needs-you items are not capped.

`silkworm status --json` prints one line of JSON:

```json
{"ok": true, "errors": [],
 "bot": {"online": true,
         "revision": {"state": "current|stale|unknown", "running": "<sha>",
                      "head": "<sha>", "behind": 0, "message": "…"},
         "drain": {"draining": false, "seconds": 0, "reason": ""},
         "runner_hold": {"seconds": 0, "reason": ""}},
 "needs_you": {"<project>": {"<state>": [{"id": "…", "title": "…", "state": "…",
                                          "role": "…", "round": "…", "step": "…"}]}},
 "tasks": {"queued": [{"id", "title", "state", "project", "role"}],
           "running": […], "blocked": […]},
 "unlanded": {"<project>": [{"id", "title", "branch", "base", "commits",
                             "stage": "<last landing's stage, or empty>",
                             "detail": "<what it said>"}]},
 "watching": [{"id", "in_s", "goal", "thread"}]}
```

Unfiled work is under the project `""`; `round` and `step` appear only on
research findings and on tasks that left you a step. A part the bot could not
answer is named in `errors` and makes `ok` false and the exit code 1 — "could
not ask" never reads as "nothing there".

## The operator

The operator is Silkworm's head agent: one Claude session with a view of every
project, which manages tasks, routes work and runs deploys so you only make the
decisions that are yours. It is not in front of every message. Send
`!operator` in a DM thread to make that thread its home; messages there go to
it, and every other thread is unchanged. `!operator where` says which thread
it is (claiming another moves it).

**What it knows.** Its context is rebuilt from live state at the start of every
turn and wake-up, not remembered from the conversation: each project's brief
(or CLAUDE.md excerpt), readiness and settings (`auto_merge`, `publish`,
`test_cmd`, `generated`, base branch); `silkworm status --json`; landings from
the last two days; bases failing their own tests; releases tagged in the last
day; recent `deploy --after-turn` log lines; and a notes file it keeps itself
(`operator_notes.md`, at most 4KB, replaced with `silkworm operator notes
--set "…"`). The whole block is held to 16,000 characters; the notes are always
kept, and anything cut says so.

**What it can run.** An allowlist, not `--dangerously-skip-permissions`: the
action commands above, `silkworm operator contained <id>` and `operator notes`,
`deploy --after-turn`, `task` (which files proposals) and `defer`, the named
ops (`silkworm ops run|list|log`, below), and Read/Grep/Glob. No `curl`, so it reaches the bot only through `bin/silkworm`,
which stamps every request with the role and thread the bot gave the turn. No
raw git either — `git log --output=<file>` writes any file, its own approvals
included, and an allowlist prefix cannot exclude a flag — so it reads history
through `silkworm operator git [--in <repo>] <log|diff|show|status|…>`, which
runs only read-only subcommands and refuses options that write a file or run
a program. Its git also carries the implementor guard: no pushes, no ref moves.

**What it does on its own:** land or retry refused landings; drop branches
proven to be on their base already — by content, not patch-id (`operator
contained` asks whether merging the branch into its base would change
anything, so a squashed or rebased landing counts, and a change the base took
and later reverted does not; a merge that conflicts proves nothing, so that
drop waits for you); retry failed work, such as quota and overload failures;
rework a landing git refused onto current main; Run next to unblock; tidy a
finished task's checkout (an hour old or more) whose only changes are
generated files; and, when
Silkworm's main is ahead of the running revision and nothing else is running,
`silkworm deploy --after-turn` (with the full suite — the CLI refuses it any
other deploy).

**What it may not do, enforced by the CLI and again by the bot:** accept a
proposal or approve flagged work — including by the side doors: Retry or Send
back on a proposal, Retry of one you dismissed, an edit that rewrites a goal,
a send-back of anything but a refused landing (that is a new goal), and Land
on anything not `done`; start a release; switch publish on (it
cannot push either); change trader's settings, or land a trader branch that
touches its live configuration (`scripts/paper_trade.py`, `trader/config.py`,
`trader/research/deployed_universe.py`, `data/research/liquid_universe.json`,
`ops/`, `.env`); or drop a branch it has not proven redundant. Each refusal
names the approval that lifts it. You give one with your own Slack message:

```text
!approve accept tsk_…        !approve release cadence     !approve publish cadence
!approve delete tsk_…        !approve trader-config tsk_… (or trader, for its settings)
!approve                     # what is approved and unused
```

An approval covers that one item, is used up by the action it allows, and
lapses after a day. Only an allowlisted person's Slack message makes one — not
the web chat, not the bot, and there is no route for it.

**Or just tell it.** In a turn you start yourself — your own Slack message in
its home thread — the operator may carry out what you decided: accept,
approve, dismiss, cancel, close, dig, rework, land, edit, drop, file straight
to the queue, and release (`silkworm release <project> [<target>|all]
[--level patch|minor]`), on the items you named. Each action must carry
`--notes` quoting or paraphrasing what you said (refused without it), and the
task records it: an event naming your Slack message, and `result.decided_by`
= `user via operator` with `decided_ts`, kept across reruns and compaction. It
reports each action with its id, and asks when it is unclear which items you
mean. This rests on a grant the bot makes for that turn alone and forgets when
it ends, handed to the turn's CLI in its environment and checked against the
turn and thread it was made for — so the daily sweep, a wake-up, a deploy
note or the web chat, none of which you started, carries none, and is refused
as before. Even on your word a running task is cancelled only if your message
named it, and dismissing never deletes a branch.

**trader's live configuration and publish, one item at a time.** Your word in
general does not lift these two; your word on the one item does, once. The
operator records them as plan entries beside any ops — `silkworm ops plan
--land tsk_…` (or `--approve tsk_…`, `--publish <project>`) — each printed
with the guard it needs and what changes live (the branch, its base and the
live-config files it touches; or that landings will be pushed). Your answer
to the plan in its thread, from Slack or the dashboard ("yes", "do 2", "all
but 1"), lifts exactly the entries it approves, and the operator then runs
`silkworm land tsk_… --notes …` or `silkworm publish <project> on --notes …`
in that turn. Or your message names the action and the item in a clause that
says nothing else ("land tsk_b921b84c73", "please approve tsk_…", "publish
cadence"); a question, a quote, or any other word in it ("I don't think we
should land tsk_…", "land tsk_… once the backtest passes") names nothing.
`land` and `approve` are different requests: approving work still under
review closes it unlanded, so an entry or a message for one never lifts the
other. When a plan holds such an entry, Silkworm posts the entries to the
thread itself, as it recorded them.
The task records `result.decided_lift`: the plan id and entry, or that your
message named it, with your message's ts and where it came from. A sweep or
wake-up never lifts them, a plan lapses after a day, and anything neither
planned nor named is refused with the advice to record a plan. Nobody needs
to type `!approve` for these any more; it still works. trader's project
settings (its base, its publish switch) remain `!approve trader-config
trader`.

Work you ask it for in so many words it files with `silkworm task
--requested-by-user --notes "<your request>" "<goal>"`: straight to the queue
(or as a proposal, with `--propose`), past the project's `MAX_OPEN_PROPOSALS`
cap, which rations ideas nobody asked for. Only in a turn you started in its
thread (the grant), with `--notes` quoting your message word for word and a
goal that shares at least two of its own words (the asking and filing words
aside), so a real quote cannot carry an unrelated goal past the cap; the task
records `requested_by_user` — who, which message,
your words and how full the board was. Its own ideas, the sweep's, ideation's
and a review's follow-ups keep the cap.

**Named ops: what it runs instead of telling you to.** Where the fix is a
command on the mini, the operator runs a named op rather than handing you a
shell step: `silkworm ops run <name> [args] --by operator --notes "…"` (its
allowlist has `ops run`, `ops list`, `ops log` and `ops plan` — still no shell). The ops
are a fixed registry in code (`ops.py`); each names its project, the directory
it runs in, the exact argv it runs, a timeout and whether it changes anything.
The bot builds the argv and runs it with no shell; arguments are validated per
op, and any argument with a character outside `A-Za-z0-9._,:=@+/-` is refused
outright.

| Op | Runs | Changes things? |
| --- | --- | --- |
| `tailscale-status` | `silkworm tailscale status` | no |
| `tailscale-on` | `silkworm tailscale on` — serve to the tailnet under this machine's current name, never Funnel | yes; your message must name it ("tailscale on") |
| `trader-launchd-reinstall <job>` | `plutil -lint ops/<job>.plist`, copy it to `~/Library/LaunchAgents`, `launchctl bootout gui/<uid>/<job>` (may fail: not loaded), `launchctl bootstrap gui/<uid> …` — only one of trader's own jobs (`com.traderjoe.*`), from its own `ops/` (no symlinks), committed and unchanged, whose `Label` is `<job>`; refused for a job with a copy in trader's `data/retired-launchd/` (a retired job stays retired until you move that copy out by hand) | yes; your message must name the op or the job (its label, or a last part of six letters or more: "pooldata") |
| `trader-launchd-retire <job>` | `launchctl bootout gui/<uid>/<job>` (may fail: not loaded), `launchctl print …` (must fail: the job is gone; its output is not kept), then move `~/Library/LaunchAgents/<job>.plist` to trader's `data/retired-launchd/<job>.<date-time>.plist` — never deleted. Only an installed `com.traderjoe.*` job. `trader-launchd-reinstall` refuses it afterwards, though its plist is still in `ops/` | yes; your message must name the op or the job's full label (`com.traderjoe.shadowoptions` — not a last part like "restart", which is said in passing) |
| `trader-env-set <KEY> <VALUE>` | backs trader's `.env` up to `data/env-backups/env.<date-time>.bak` (mode 0600), then changes only that key's line (or adds it) and prints `KEY: old -> new` — never another line of the file. Only keys and values in `ops.ENV_KEYS`: `MOMENTUM_UNIVERSE` in {`static`, `pit`}. Takes effect at the trading daemon's next restart (the weekly Sunday one); it does not restart it | yes; your message must name the op or the key, and the value ("momentum universe to pit"); a `.env` with line breaks other than `\n`, or a BOM, is refused untouched |
| `trader-refetch-revisions --symbols S … [--dividends] [--apply]` | trader's `.venv/bin/python3 scripts/refetch_revisions.py` with those arguments; its own guards stay (refuses while the market is open, takes `update_data`'s lock, backs up first) | only with `--apply`; your message must name it ("refetch revisions") |
| `project-test <project>` | the project's `test_cmd`, split as verification splits it, in its live checkout, under that suite's lock (refused, not queued, while another run holds it) | yes: a suite can write into the checkout |

One that only reads runs in any turn, the daily sweep included. One that
changes something runs from inside a turn only on your word — the grant of a
turn you started in its home thread, with `--notes` — and never in a sweep or a
wake-up. Trader's (its live configuration) and `tailscale-on` (exposure) also
need your message to name them, as whole words — or to approve them as entries
of a plan.

**Plans: say yes, not the command.** When the operator proposes ops (a sweep's
*Needs you*, or a reply here), it records them with `silkworm ops plan <op>
[args] [--op <op> [args]]… --by operator` and numbers them in its reply as that
prints them. A plain answer in its thread runs them:
"yes", "go", "do it", "run them" (all), "do 1 and 3", "just 2", "all but 2".
Phone typing reads the same: curly quotes, an ellipsis, dashes, 👍/✅, trailing
punctuation and case are normalised first, and you may open by naming the plan
("for trader… “yes”", "trader: do 1 and 3", "re the ops: yes") — a lead-in
naming anything but the plan's own ops leaves the message refused.
The operator runs each with `ops run <op> <args> --plan-entry N`, and the bot
checks that it is exactly that entry's op and arguments, that the entry has not
run, that the plan was recorded before your message and is still the live one
(a day at most, replaced by the next), that your message is the first one
after it that answers it, and that your words approve
entry N — read by `operator_role.plan_approval`, fixed rules with tests, never
a model. A question ("what does 2 do?"), a no, wait or later, a number that is
not an entry, or any word beyond yes, entry numbers and the plan's own op
words ("do it in 2 hours", "all except the trader ones") approves nothing; the
operator answers, records the plan again and asks. Only an answer spends a plan:
a yes, entry numbers, or a plain no ("no", "not now", "skip it", "cancel the
plan", "absolutely not", 👎 — any word that sounds like no, `plan_declines`).
A message that is none of those ("file it", "what does 3 do?") leaves it
waiting, so your yes after it still runs it (`plan_answer`), and the operator
says in its reply that the plan is still waiting; from then on a bare "ok" or
"sure" (likely about that reply) no longer answers it — say "yes" or the
entries. Once answered, a later "ok thanks" or "yes" answers nothing.
**Or press it on the plan's card.** The dashboard draws the live plan as a
card in the operator's thread and on *Needs you*: each entry numbered, with
its command, a plain description and what it changes, a checkbox, and **Run
all**, **Run selected** and **Not now**. A press is your answer, exactly as a
typed reply is — the first answer only (after it no message answers the
plan, and after a typed one the card is disabled), only while the plan is
live (a day, not replaced), each entry once — and goes through its own route
(`/api/plan` → the bot's `/plan`, `handle_plan`), past the dashboard's token
and cross-site checks and only when the dashboard verifies it is you (the
same `dashowner.verify` a typed dashboard "yes" needs to carry your grant;
the bot takes that only from the visualizer's own connection). Never from a
turn: a request a sweep, a wake-up or any turn stamped is refused, and so is
one while a deploy drains. The bot then runs what you chose, in order, itself
— an op as `ops run` would (logged, with "dashboard (login on device)" as
who decided), a land, approve or publish entry as its own dashboard button
would (the task records the plan and entry) — and writes each entry's
outcome onto the plan: the card shows it running, then ok or failed with the
exit code and the output's tail, and the operator's thread is told what ran.
Not now declines the plan; nothing in it runs, and the card goes at once
(and stays gone after a reload). A plan that can no longer be pressed —
spent, answered, declined, expired or replaced — is never drawn as a card
with buttons: in its thread it collapses to one line saying which (a run
you pressed keeps its full card for ten minutes, so its results can be
read), and Needs you lists only a plan still waiting. Every card and line
has an × that clears it for good (declining it first if it was still
waiting); `silkworm ops plan --decline [<plan id>]` does the same from a
terminal, so a stuck card can always go.

An op not in the plan still needs naming. Each run
records the plan id, the entry and your message's ts; `silkworm ops plan`
alone shows the pending plan. Your own terminal and the dashboard carry no turn and
are you: `silkworm ops run tailscale-on` works there as it is. The CLI refuses
first, and the bot again. Every run and refusal is appended to
`logs/ops.jsonl` — who, on whose Slack message, each step's argv, exit code and
the tail of its output (credentials scrubbed) — which `silkworm ops log` reads;
the operator quotes the same in its reply. `silkworm ops list` shows the
registry. A long op (a test suite, a refetch) keeps running in the bot if the
CLI gives up waiting, and lands in the log.

**The daily sweep.** Once a day at `OPERATOR_SWEEP_AT` (default `08:30`, or
`off`), and only after the digest has gone out (unless the digest is off), the
operator wakes in its thread — a wake-up like any other, so it runs in the wake
lane and catches up after a restart — works the routine pile, and posts exactly
one message: what it did, then what needs you, with the question to answer or
the `!approve` to send for each. It also waits for that morning's health pass
(`HEALTH_LEAD` earlier), with the same three-hour grace as the digest.

**Chief of staff.** The sweep's message opens with a per-project health brief
Silkworm writes from the store (`silkworm health --brief` prints it): one line
per project — `🟢 *cadence* — CI green, backend current, 1.1.0 live` — and,
under a project, one line per check that is not ok saying how long it has been
so and what changed since yesterday, then either what is being done (a fix
proposal, `proposal tsk_x open since Tuesday`; a proposal held by the caps,
and why) or the exact decision it needs from you (`decide: release cadence
backend`). Then the operator's own report: routine work, then needs you — all
in one Slack message, the brief cut by whole lines if it must be. For code
work (CI, the trader's dispatch and healthcheck) the operator may file one
fix proposal per check and trouble (`task --propose --health`), which waits
for you like any proposal; the check records the link, so it is never filed
twice.

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
wait. The bot keeps the same "what needs me" board as **one message in a private
channel**, edited in place rather than reposted: work awaiting approval (with
the reviewer's findings), tasks that asked you something, failures and
proposals, then what is running and what is scheduled to wake. Pin the channel
and it is a tap away on any device.

Setting it up:

1. Create a **private** channel named `silkworm-board` (or set
   `SILKWORM_BOARD_CHANNEL` to another name, or to the channel's id).
2. Invite the bot to it. Until it is in a channel by that name, it logs a
   warning and posts nothing.
3. Keep the members to the bot and people on `SLACK_ALLOWED_USERS`.

A channel message is read by everyone in the channel, and the board carries
task goals and review findings. So the bot checks, every pass (once a minute)
and again the moment anyone joins (the `member_joined_channel` event), that the
channel is private and that every member is the bot or on the allowlist. If
either stops being true -- the channel is made public, or someone off the
allowlist joins -- it **deletes the board** and posts nothing until it is true
again, logging why. A public channel is never used: anyone in the workspace can
read one without joining. (With `SLACK_ALLOWED_USERS` empty everyone is
allowed, so only the private check applies.) If the bot is removed from the
channel, it can no longer delete its last board message; delete that by hand.

Its buttons are the dashboard's, state for state, and go through the same
route, so Approve still lands the branch and Cancel still stops a running
agent. Approve and Dismiss ask first. A button only acts if the task is still
in the state it was drawn for; if it has moved on since, it says so and does
nothing. A click's result shows at the top of the board for about a minute and
a half, rather than as a reply under it. Someone off the allowlist who clicks
gets only a private (ephemeral) refusal, visible to them alone.

It is the attention view, not the backlog: a large board is cut to fit Slack's
50-block message limit and says how many are left for the dashboard. It is
redrawn when what it shows changes, and every ten minutes so relative times
stay right. The daily digest and other bot messages never go to this channel.

**The app's Home tab must stay off** (`home_tab_enabled: false` in
`manifest.json`). The board used to live there, and with the Home tab on,
Slack's Threads view sent **Reply** to the app's Home instead of to the thread
-- turning it off is what fixed that. Don't re-enable it or subscribe to
`app_home_opened`. An app created from an older manifest needs the new one
pasted in (above): it turns the Home tab off and adds `member_joined_channel`.

## Reaching the dashboard from another machine

The visualizer binds `127.0.0.1` only, and it should stay that way: it can relay
prompts into threads, and the bot trusts any localhost caller as the machine
owner (bypassing the Slack allowlist). On a host running
`--dangerously-skip-permissions`, exposing that port to a LAN hands arbitrary
command execution to anything on the network.

### Tailscale (recommended — phone and laptop)

`tailscale serve` gives the dashboard an HTTPS address on your tailnet
(`https://<host>.<tailnet>.ts.net/`) with no open ports and no change to the
bind: serve runs on the host and proxies tailnet traffic to `127.0.0.1:8790`.
This supersedes the SSH-tunnel advice below for phone access and for reaching
the host from outside your network.

Because serve's requests arrive from loopback, the dashboard does not trust
them the way it trusts a local caller. Serve stamps each request it proxies
with the requester's identity (`Tailscale-User-Login`), stripping any copy a
client sent, and the dashboard admits a proxied request only when that login
equals `VIZ_TAILSCALE_LOGIN` — on every route, `/api/send` and the action
endpoints included. Everything else gets a 403 with the reason:

- a different tailnet user, or a node shared in from someone else's tailnet;
- a request with no identity (tagged devices get none);
- anything at all while `VIZ_TAILSCALE_LOGIN` is unset — so turning serve on
  before configuring the login exposes nothing.

Serve vouches for the device, not the web page, so a proxied POST must also
look like the dashboard's own page: `Content-Type: application/json`, and no
`Origin` or `Sec-Fetch-Site` saying it came from another site. That stops a
hostile page open in your phone's browser from posting tasks as you.

Funnel (the public internet) is never used and is refused if it reaches the
dashboard. Direct loopback requests — the CLI, an SSH tunnel — work as before.

Setup, on the host:

1. Install the [Tailscale Mac app](https://tailscale.com/download/mac) and sign
   in. Install it on your phone/laptop with the same account.
2. In the [admin console](https://login.tailscale.com/admin/dns) enable
   **MagicDNS** and then **HTTPS Certificates**.
3. Add your Tailscale login (as shown in the admin console's Users page) to
   `.env`: `VIZ_TAILSCALE_LOGIN=you@example.com`. It has to be `.env` — the
   installed service gets no other environment — and the dashboard re-reads it
   on every request, so no restart is needed.
4. Run:

```sh
silkworm tailscale on       # tailscale serve --bg 8790
silkworm tailscale status   # installed, logged in, HTTPS on, serve -> :8790 only,
                            # Funnel off, VIZ_TAILSCALE_LOGIN matches this login
silkworm tailscale off      # tailscale serve reset (clears all serve config)
```

`silkworm tailscale` prints these install steps instead if Tailscale is not
installed. `status` also fails if serve exposes anything besides the
dashboard — never serve the bot's port (8787), which trusts every local caller
and has no identity gate.

**On the phone.** With Tailscale on, open `https://<host>.<tailnet>.ts.net`
in Safari, then Share → Add to Home Screen: the dashboard installs as a
standalone app named Silkworm (`/manifest.webmanifest`, icons under `/icons/`).
Below 700px wide the session list and the open thread take turns on screen,
and the Tasks/Projects/Learnings panels fill it, each with its own ✕. The
cost and cache stats sit in a collapsed 📊 line at the top of the list. Its
service worker (`/sw.js`) caches nothing — no API answer is ever served stale —
and only replaces a failed page load with "Can't reach Silkworm — is Tailscale
connected?". The icons are drawn from `assets/logo.svg` by
`python3 assets/make_icons.py`; rerun it after changing the logo (the suite
fails while they differ).

**Notifications.** In the installed app, tap 🔔 in the header and allow
notifications (iPhone needs iOS 16.4+ and only offers them in the home-screen
app; in Safari the button says *Add to Home Screen first*). The phone is then
told when a task enters **awaiting approval**, **needs input** or **failed** —
once per task and state, so a retry loop cannot spam — and when a thread
(dashboard- or Slack-born) has a finished reply. A quota/overload failure that
parks to retry is not news, nor is a scheduled check that found nothing.
Tapping one opens the app on that thread, or on the task in the needs-you
list. A notification carries a short title and the task's or thread's title
only — never a goal, a reply or an error. Slack's own notifications are
unchanged.

This is web push (RFC 8291/8292, `push.py`, which needs `cryptography` from
`requirements.txt`). The VAPID keypair is made on first use in
`~/.silkworm/vapid/` (override with `SILKWORM_VAPID_DIR`), mode 600, outside
every repository, and is never printed; subscriptions live in `push.json`.
Set `PUSH_CONTACT` (a `mailto:` or `https:` URL) to change the contact push
services are given. Sending runs on its own thread and can never fail or delay
a turn or a task's move; a device the push service reports gone (404/410) is
dropped.

```bash
silkworm push status        # keys present, cryptography installed, devices subscribed
silkworm push test          # send a test notification to every device now
silkworm push forget-all    # drop every subscription (re-enable from 🔔)
```

A message sent from the dashboard carries an id of its own. If the dashboard
gives up waiting on the bot (a slow Slack post), the page does not assume it
failed: it sends it again under the same id, which the bot answers from the
first send — one thread, one turn — and if it still cannot tell, shows the
message as *not confirmed* with **Check again** rather than putting the text
back to be sent twice. (The ids are held in the bot's memory for an hour; a
restart between the two sends forgets them.)

### SSH tunnel (laptop on the same network)

On the machine you browse *from*:

```sh
./bin/silkworm-tunnel install you@your-mini.local
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
that tries, in order: the host you installed with, the mDNS `.local` name
(`SILKWORM_HOST`, if you installed against an IP), whatever answered last time,
and finally an ARP lookup by the address in `SILKWORM_MAC`, if you set one.
Nothing about your machine is built in; with neither an argument nor
`SILKWORM_HOST`, install refuses.

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

For phone access, or from outside the network, use Tailscale (above) instead.

### Binding wider with a token

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

- **Needs you, the home screen** — the page opens on one list of everything
  waiting on you (inbox.py): proposals, work waiting for approval, steps a run
  left you and research findings, failures, landings git refused, unlanded
  branches nothing will land on its own (a researcher's or assistant's
  commits, a dismissed or superseded task's, a dropped branch's leftover
  remote copy — each with its reason, and **Refile to land**, which hands the
  branch to a new implementor task that is reviewed and lands it, or
  **Drop**), the operator's recorded ops plan, and health checks that name a
  decision of yours. Grouped by project, the most urgent group and item first, newest
  first among equals. Each card says what it is and why in a line, shows the
  operator's recommendation when it set one (`silkworm edit <id> --recommend
  … --why …`; only for the item as it stands — a task that has moved on, or
  research dug deeper, drops it), and offers exactly the buttons the item
  allows, each the same /tasks action the task panel sends: Accept, Dismiss
  and Retry go at once with Undo (as a swipe does); Approve, Land, Drop, Refile
  to land, Close, Done ask first; Send back / Answer and Dig deeper ask for your words. The
  operator's plan is its own card — Run all, Run selected, Not now (see
  *Plans* above) — and stays on the list while what you chose runs, showing
  each step's result. On a laptop it sits in
  the main pane while no thread is open (**📥 Needs you** above the list comes
  back to it); on a phone it is the first of two tabs, **Needs you** and
  **Threads**. `?view=inbox` / `?view=threads` open either.
- **On a laptop or monitor (1024px and wider)** — an app's layout; nothing
  below 1024px changes (the phone's page is pixel-for-pixel the same). A
  sidebar that never goes away: **Needs you** (its count), **Threads** (unread
  count; opens the newest thread if none is), **Projects**, **Trader**,
  **Tasks** (the badge), **Spend**, **Learnings**, and at its foot Search,
  Settings (theme, notifications, quiet hours, naming untitled threads) and
  Shortcuts. The header is the search field. Tasks, Projects, Trader,
  Learnings and Spend are pages beside the sidebar rather than sheets over a
  dimmed page, centred at a reading width; the thread list sits beside the
  open thread, and a task opened from Tasks (its **Open ›**, or Enter) sits
  beside the list it came from. Both lists are resizable: drag the line
  between, ←/→ on it once focused, double-click for the default; the width
  is kept in the browser. What is open is kept in the address
  (`?thread=`, `?task=…&view=tasks`, `?view=board&slug=…`, the deep links
  below), so a reload or a bookmark comes back to it. Forms are centred
  dialogs; a card's detail and the attachments are panels at the right.
  Keys: **j/k** move through the list in front (the open panel's tasks or
  cards, the threads while one is open, else Needs you; the pointer's last
  list on the home screen), **Enter** opens, **e** accepts or approves, **d**
  dismisses (each presses the row's own button), **/** or **⌘K** searches,
  **Esc** closes what is on top, **?** lists them. None fires while a field
  has the keyboard. Right-click a thread, task row, board or project card,
  or Needs-you card for that row's own buttons — what the phone does by
  swiping. `?modal=keys` and `?modal=prefs` open the shortcut list and
  Settings.
- **Cost & cache analytics** — stat tiles (total spend, 14-day tokens, cache
  hit rate) and a daily stacked bar chart of cache-read / fresh-input / output
  tokens, with hover tooltips and a table view. Cache misses show up as blue
  bars — if the gold disappears, something is invalidating your prompt cache.
- **Live status badges** — running / checked out / in-terminal per thread,
  polled from the bot; a dot in the header (beside the name in the laptop's sidebar) shows whether the bot is up.
- **Transcripts** — markdown-rendered messages with per-turn token usage and
  cache-hit %, collapsible tool calls, results, and thinking.
- **Search threads and tasks** — one field (⌘K / Ctrl-K jumps to it; 🔍 on
  a phone) finds threads by title, summary and the words of their messages,
  and tasks by id (`tsk_…`, whole or its first characters), title, goal and
  result. Results come in two groups, the better first, each showing its
  first six (the rest on a tap) with the words around the match and the match
  marked; at most 50 come back, each group sure of ten. ↑ ↓ and Return pick
  one; a thread opens in place, a task as its card on the board. Answered
  from an index in memory (visualizer.SearchIndex): the stores are re-read
  only when they change, and a transcript only from where it was last read —
  about 10ms a search on the real stores, the first after startup built in
  the background. Everything indexed is scrubbed of credentials first
  (health_probes.scrub), so a secret can be neither shown nor found.
  `?q=<words>` opens a search.
- **Files** — everything exchanged with a thread (Slack uploads in, generated
  files out) listed with download links; outbound files are archived under
  `artifacts/`.
- **Conversations from the web** — the dashboard is a full place to talk, not
  only to read. **+ New thread** starts one (optionally filed under a project,
  which the bot does with `!project <slug>` before the first turn, so it
  inherits it exactly as from Slack); the composer continues any thread,
  Slack-born or not. A thread is still its Slack thread — same key, session,
  task records and one-turn-at-a-time lock — so it carries on from either
  side. Your message shows at once, then *queued* (behind the turn holding the
  thread) or *working*, and the reply streams in from the transcript while a
  turn runs (the open thread is read every 1.5s then, 4s otherwise). 📎
  attaches files and photos (25 MB each, ten per message): they are saved in
  `uploads/<thread>/` like Slack uploads, and the turn is told to Read an
  image. Files the bot produces are listed with download links. Videos
  (`.mp4`, `.mov`/`.m4v`, `.webm`), sent or produced, play where they were
  sent -- in chat, the attachments drawer and a task's page -- in a player with
  a Quick Look still, its Download link kept: served inline as their own type,
  never sniffed, in byte ranges (206) so iPhone Safari can play and seek them.
  Anything else (an `.html`, an `.svg`) is only ever a download. `!commands`
  (`!project`, `!stop`, `!reset`, `!brief`, …) go through the bot's own
  handler and their replies are shown under them.
  In Slack, a message sent from the dashboard appears in the thread posted by
  the bot as `🌐 via visualizer: <text>`, with a 📎 line naming any files; a
  new thread's first message is the thread's parent in the home channel
  (`SILKWORM_HOME_CHANNEL`, else your latest DM with the bot). Tool approvals
  (`CLAUDE_APPROVAL_MODE=slack`) are still answered in Slack only.
- **On a phone** (under 700px wide) the page is its own layout: a one-row
  header (🔍 opens search, ⋯ holds Learnings, Name untitled and Notify), the
  list and the open thread taking turns, filter chips in one sideways-scrolling
  row, the board one column at a time behind a row of column tabs, and panels
  and forms as full-screen sheets with their buttons pinned to the bottom.
  Everything tappable is at least 44px and every field 16px, so iOS does not
  zoom in on focus. The laptop layout is untouched.
- **Chat on a phone.** The list opens on *Conversations* (task runs are
  reached from 📋 Tasks, or the chips), and ✏️ in the header starts a new
  thread. An open thread is a chat with the screen to itself: a sticky header
  (‹ back, its name, project, running time with ■ Stop, ⓘ for the resume
  command, Slack link, summary and events), your messages and Claude's
  replies as bubbles, and each run of tool calls folded into one line
  (`⚙ ran 6 tools · Bash, Read`) that opens onto the full log. A reply's
  tokens, cache and model are behind its time. It follows the newest message
  while you are at the bottom -- the keyboard opening, the box growing a line,
  a thumbnail or a tools line unfolding included; scrolled up, the message you
  are reading stays where it is whatever is drawn or loaded above it, and
  *↓ New messages* appears. The box grows to six lines and sits on the
  keyboard. Alerts (stale revision,
  Slack down, stalled threads, cost spikes) are never a bar on a phone: the
  header shows `⚠ <count>`, which opens them as a sheet.
- **Unread** — a thread with replies you have not read shows a dot and how
  many on its row, and the list has an *Unread* filter and an *unread first*
  order (kept on the device). The total is on the home-screen app's icon
  (`navigator.setAppBadge`: iPhone home-screen apps on iOS 16.4+, and most
  desktop browsers), cleared when nothing is unread, and in the tab's title
  as `(3) Silkworm`. A reply is anything in the thread not written by you —
  Claude answering, the operator, a task's run, a wake-up reporting back — that
  arrived after you last read the thread here at its bottom, or after you last
  wrote in it from Slack or the dashboard (writing means you have read what
  came before). Your own messages never count. Read state is kept on the mini,
  in `seen.json` beside `sessions.json` (`unread.py`), so reading on the phone
  clears the laptop and back: the page posts `/api/seen` when an open thread is
  at its bottom, behind the same token, tailnet and cross-site checks as every
  other dashboard write. It counts from the transcripts and polls the dashboard
  already makes; pushes are unchanged. **Slack's own read state isn't visible
  to bots** — Slack gives apps no read receipts — so a thread you read only in
  Slack stays unread here until you open it on the dashboard or reply. Tracking
  starts when `seen.json` is first made: nothing before that is unread.
- **A task's page** — `/?task=<id>` (and *Full page* on a card's detail, the
  address a notification opens) shows one task top to bottom: its title and
  state said in plain words, the step it left for you, prominently, with exactly
  the buttons *Needs you* has for it (the same `/tasks` actions); its reply drawn
  as markdown, with its review and landing; what it changed in git — its branch
  or the landed `before..head`, commits with subjects, files with lines added
  and removed — read through the operator's vetted read-only git command
  (`taskchanges.py`: no pager, textconv, external diff or repo-named programs)
  and cached a minute; the images it produced, its thread's own from the window
  it ran in; what it cost, each run and each review; and a timeline from filing
  (who accepted it and how) through runs, reviews, landing, the release tag that
  shipped it and, for Silkworm itself, whether the running bot has it. Links go
  to its thread, its project's page, its parent and what it filed.
  `board.task_page` builds it from named fields only — a record's scope,
  environment and session never reach the page — served by the bot's `/tasks`
  `page` action.
- **Spend** — `/?view=spend` (💰 Spend on the projects overview, the stats
  tile, or *by week* on the tasks panel's spend line): what was spent per
  project per week over the last twelve seven-day weeks as stacked bars (a
  table under them), this week against the four before it, the calendar
  month's ten dearest tasks (each with its reviews, each opening its page) and
  the split by role. `spend.py` builds it from the per-run cost records (the
  delta-corrected ones, `turncost.py`), dated by when the money was spent,
  through the same windowing as the digest and the week line
  (`costs.by_project_between`), so for one window they agree. Figures are API
  list-price equivalents (the bot runs on a subscription); a run with no
  recorded cost is unknown — `$?`, or a `+` on a floor — never zero. Five
  projects get their own colour (`--series-1..5`, slots by name so a colour
  stays with its project), the rest fold into *other*. Read-only; served by
  the bot's `/tasks` `spend` action.
- **Deep links** — every view opens from its address, which is how a
  notification lands on a thread and how the screenshots below are taken:
  `/?thread=<key>`, `/?task=<id>` (the task's own page; `&sec=<section>` scrolls
  to one), 
  `/?view=stats|tasks|projects|learnings` (`&tab=all` for every task),
  `/?view=project&slug=<slug>` (the project's own page: readiness, health,
  App Store and TestFlight state, the fortnight's landings and release tags,
  what waits on you, the queue and unmerged branches; `&sec=<section>` scrolls
  to one), `/?view=board&slug=<slug>` (its board; `all`, `unfiled`),
  `/?view=spend` (what was spent; `&sec=weeks|top|roles`),
  `&card=<task id>` (a task's detail), `/?modal=new-thread|new-project|new-task|settings|release|alerts`
  (`&slug=` for the last two), `/?q=<words>` (search), `/?list=unread`, `&theme=dark|light|auto`
  (that view only). The address is cleared once read, so a reload opens the list.
- **Look and feel** — one design system: CSS custom properties at `:root` for
  spacing, type, radii, elevation, one accent (the silk gold) with ok / warn /
  bad / info, and motion (120–240ms). Nothing below them writes a value out, and
  a test holds that. Dark is the default; ⋯ › Theme picks Light or Auto (follows
  the system), kept on the device. Motion is transform and opacity only, and is
  absent under *Reduce Motion*: panels rise in over a dimmed backdrop (a sheet
  sliding up on a phone) and leave the same way; a thread slides in, and the list
  slides back. Lists fade in. A card or row that changes column or place glides
  there, and one that leaves fades where it stood. Grey shimmering shapes stand
  in while anything loads. Every button gives under a press.
- **Touch** — swipe a proposal card or row right to accept or left to dismiss.
  It goes at once, a toast offers *Undo*, and the action is sent five seconds
  later, or at once if the app is put away. A refusal puts the card back and
  says why. A hidden thread goes at once too, and its toast's *Undo* shows it
  again. Irreversible actions
  (Stop agent, Drop, Reset, settings that change what lands) still ask first.
  Pull a list down past its top on a phone to refresh it.

### Screenshots of every view

```sh
bin/dashshots                          # artifacts/dashshots/<view>-<width>.png
bin/dashshots --widths 375 --views board,card
bin/dashshots --widths 1280,1440,1920 --views inbox,task-split,keys
bin/dashshots --source /path/to/other/checkout --out /tmp/before
bin/dashshots --scroll-probe --widths 430,375 --before /tmp/main-export --out /tmp/probe
bin/dashshots --perf --before /tmp/main-export --perf-runs 5 --out /tmp/perf
bin/dashshots --video-probe --widths 375,1032 --out /tmp/videos
```

Mobile Safari in an iOS Simulator at 375px (iPhone SE (3rd generation)) and
430px (iPhone 16 Plus), and WebKit's own snapshot for the laptop and desktop:
1280px by default, and `--widths 1280,1440,1920` (or any width of 1024+) for
more. To prove a change left the phone alone, shoot `--before` an export of
main at 375,430 and compare each pair's pixels with `bin/pngdiff.swift`
(`swiftc -O bin/pngdiff.swift -o pngdiff; ./pngdiff a.png b.png --skip-top 60`:
the top rows are the simulator's clock).
The dashboard shot is a copy of the code run from a temp directory against a
copy of `sessions.json`, and the bot behind it is a stand-in that passes only
reads through to the real one (board, projects, roles, learnings, status) and
refuses every action, so nothing it does can change Silkworm's state. Its
simulators are its own (`Silkworm Shots 375` / `430`, made on first use) and are
shut down afterwards; it never touches any other device. Needs Xcode (for
`simctl` and `swiftc`). What the simulator cannot show: the home-screen app's
standalone mode (Add to Home Screen needs a tap), so the safe-area insets there
are checked by the CSS alone. The thread views are a long
conversation (`thread`), a turn running now (`thread-running` -- run from a
Silkworm turn, its own), and a new thread with nothing in it yet
(`thread-new`).

`--scroll-probe` checks the chat keeps the reader's place. The copy's page
gets `bin/scrollprobe.js`, which opens threads it makes up itself and puts
each thing that can move the reader through the page's own code -- the
keyboard opening and closing (the simulator's real one: focusing the composer
opens it), new messages by a poll, a tools line updating and unfolding, a
thumbnail arriving, the working indicator coming and going, switching thread,
something above growing late -- once read at the bottom and once scrolled up.
It prints `ok`/`JUMP` per case, writes what it measured to
`probe-<width>[-before|-after].json`, and photographs every phase (a burst of
frames right after each trigger) into `frames-<width>…/`. `--runs` picks triggers.

`--perf` measures instead. `bin/perfprobe.js` reads the browser's paint timing
and when the session list's first card was drawn. It counts frames while the
list, the task panel, a board and the longest thread are scrolled along a fixed
path, and while a panel or a thread opens. With `--before`, both copies run
`--perf-runs` times each into `perf-<width>.json`. Phone widths only: the
laptop's offscreen WebKit view runs no animation clock.

`--video-probe` plays videos. `bin/videoprobe.js` opens the drawer of the
thread with the most videos on disk and, in Safari's own player, plays the
first `.mp4`, `.mov`, `.m4v` and `.webm`, seeks each to 60% and checks it plays
on from there -- through the dashboard's Range route. It writes
`videoprobe-<width>.png` (and a frame as each finishes) and `.json`, and exits
1 unless every one played and seeked. Widths 375, 430 and 1032: 1032 is an
iPad Pro held upright (`Silkworm Shots 1032`), the desktop layout in a Safari
that runs it live -- the laptop's WebKit snapshot is a window never on screen,
where WebKit stops the page's timers and nothing would play (it still draws
the players, without their pictures). The `thread-videos` and
`thread-videos-drawer` views photograph the players.

## Notes & limits

- The bot runs Claude as *you*, on *your* machine. In the default `skip` mode
  anyone who can message the bot can run arbitrary commands as your user — set
  `SLACK_ALLOWED_USERS`, or use `CLAUDE_APPROVAL_MODE=slack`, in any workspace
  you don't fully trust.
- Slack's free plan hides messages after 90 days, but Claude session history
  lives locally in `~/.claude`, so old threads still resume.
- Messages within one thread run serially (a session is single-turn-at-a-time);
  different threads run in parallel.
