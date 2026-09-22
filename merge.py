"""Land a task's branch on the project's base, or explain why it did not.

Everything here is evidence rather than judgement. A branch lands only when it
has been shown to work *after* being brought up to date -- not when it worked
alone, and not when an agent believes it is fine.

The order matters, and each step exists because of something that actually
happened:

  rebase   -- eight branches accumulated off one base, so "it passed on my
              branch" says nothing about whether it passes on today's main.
  retest   -- two agents once implemented the same fix independently. Each
              passed alone. Only running the suite after combining them could
              have caught that.
  recheck  -- the base must still be on the commit the branch was rebased
              onto, or the thing that passed is not the thing being merged.
  merge    -- fast-forward only, so history stays linear and the merge itself
              cannot introduce a resolution nobody reviewed.
  retest   -- again, on the base, because a fast-forward can still break a
              repository whose tests depend on files outside the diff.
  revert   -- if that fails, put the base back exactly where it was. A broken
              main is much worse than an unlanded branch.
  publish  -- if the project asks for it, push the base, so origin and the
              local branch cannot drift apart. A push that is refused undoes
              the landing: a base that did not publish has not really landed.

It refuses rather than guesses: a dirty checkout, a conflicting rebase or a
missing test command all stop the landing and hand it back with a reason.
"""

import logging
import subprocess

import worktrees

log = logging.getLogger("silkworm.merge")


def _git(cwd, *args, timeout: int = 300):
    return subprocess.run(["git", *args], cwd=str(cwd), capture_output=True,
                          text=True, timeout=timeout)


def _fail(stage: str, detail: str) -> dict:
    return {"landed": False, "stage": stage, "detail": detail.strip()[-600:]}


def _on_origin(repo, base_name: str) -> str:
    """What commit origin has the base on, or "" if it cannot be asked.

    Deliberately the remote itself rather than the remote-tracking ref: this is
    used to settle whether a push that reported failure actually took, and a
    stale `origin/main` is exactly what cannot answer that.
    """
    try:
        r = _git(repo, "ls-remote", "origin", f"refs/heads/{base_name}",
                 timeout=120)
    except subprocess.TimeoutExpired:
        return ""
    out = r.stdout.split() if r.returncode == 0 else []
    return out[0].strip() if out else ""


def land(worktree, repo, branch: str, base: str, run_tests,
         publish: bool = False) -> dict:
    """Rebase, retest, fast-forward, retest, and revert if that broke it.

    `run_tests(cwd)` is injected so the caller owns what "the tests" means and
    this stays testable without one.

    `publish` pushes the base to origin once everything else has passed. It is
    the project's decision, not this module's: see the note on the push itself.
    """
    # Tracked changes only. Running the suite leaves build droppings --
    # __pycache__, .pytest_cache, coverage files -- in the very checkout it
    # tested, so counting untracked files meant the first landing succeeded and
    # every one after it refused as "dirty". Untracked files are safe anyway:
    # a fast-forward that would overwrite one is refused by git itself.
    #
    # Uncommitted edits to tracked files are a different matter: they are
    # someone's work in progress, and merging over them makes a revert
    # ambiguous.
    dirty = _git(repo, "status", "--porcelain", "--untracked-files=no")
    if dirty.stdout.strip():
        return _fail("base-dirty",
                     "the checkout has uncommitted changes, so nothing was merged")

    # Best-effort, and only so the base can be *named* from an up-to-date
    # remote: what gets merged is decided below, from the local checkout.
    _git(repo, "fetch", "--quiet", "origin", timeout=120)

    # A project that has not named a base gets the default branch resolved the
    # same way a worktree does. Passing the empty string through meant
    # `git rebase ''` -- "fatal: invalid upstream" -- which refused every
    # landing on every project that had not set one, which is most of them.
    ref = worktrees.base_ref(repo, fetch=False, prefer=base)
    # `origin/HEAD` is a symbolic ref: splitting it on "/" gives "HEAD", not
    # the branch it points at, so the checkout-is-on-the-base check compared
    # "main" against "HEAD" and refused. Resolve it to the real name first.
    resolved = _git(repo, "rev-parse", "--abbrev-ref", ref).stdout.strip()
    base_name = (resolved or ref).split("/")[-1] or "main"
    on = _git(repo, "rev-parse", "--abbrev-ref", "HEAD").stdout.strip()
    if on != base_name:
        return _fail("base-branch",
                     f"the checkout is on {on!r}, not {base_name!r}")

    # *The* commit, read once: what gets rebased onto and what gets
    # fast-forwarded have to be the same thing, or landing locks itself out
    # after its first success.
    #
    # `base_ref` prefers the remote-tracking ref, which is right for *starting*
    # work -- fresh origin/main is the base a new branch should be cut from --
    # and wrong here. A landing is published only if the project asked for it,
    # so the first branch to land leaves the local base ahead of origin; every
    # later branch, rebased onto origin/main, could then no longer fast-forward
    # a local base that had moved past it, and refused at `merge` for ever.
    # That is not a hypothesis: measured in this repository, local main twelve
    # commits ahead of origin and thirteen `silkworm/tsk_*` branches unmerged
    # behind it.
    #
    # So the base is the local branch this checkout is on, pinned to its exact
    # commit. That holds whether or not the project publishes: publishing makes
    # the two refs agree *usually*, which would only turn a permanent lockout
    # into an intermittent one.
    before = _git(repo, "rev-parse", "HEAD").stdout.strip()
    if not before:
        return _fail("base-branch",
                     f"could not read the commit {base_name!r} is on")

    # 1. bring the work up to date with the commit it will be merged into
    reb = _git(worktree, "rebase", before)
    if reb.returncode != 0:
        _git(worktree, "rebase", "--abort")
        return _fail("rebase", (reb.stderr or reb.stdout) +
                     "\nrebase onto the base conflicts; it needs a person")

    # 2. prove it still works *after* being brought up to date
    after_rebase = run_tests(worktree)
    if not after_rebase.get("ok"):
        return _fail("tests-after-rebase",
                     "the change passes alone but not on top of the current base:\n"
                     + str(after_rebase.get("output", "")))

    # 3. the tested commit must be the commit that gets merged. Running the
    # suite takes minutes, and another landing (or a person) moving the base in
    # that window means what was proven is not what would ship. `--ff-only`
    # would refuse that, but only in git's own words, under a message that also
    # read as "the base moved" when the real cause was the mismatch above. Name
    # both commits instead, so the two cannot be confused again.
    now = _git(repo, "rev-parse", "HEAD").stdout.strip()
    if now != before:
        return _fail("base-moved",
                     f"{base_name!r} was at {before[:8]} when the branch was "
                     f"rebased onto it and is at {now[:8]} now, so the tested "
                     f"commit is not the one that would be merged")

    # 4. fast-forward only: no merge commit resolving anything unreviewed
    ff = _git(repo, "merge", "--ff-only", branch)
    if ff.returncode != 0:
        return _fail("merge", (ff.stderr or ff.stdout) +
                     f"\n{branch} is not a fast-forward of {base_name!r} at "
                     f"{before[:8]}, so nothing was merged")

    # 5. and prove the base itself is still sound
    after_merge = run_tests(repo)
    if not after_merge.get("ok"):
        _git(repo, "reset", "--hard", before)
        log.error("landing %s broke %s; reset back to %s", branch, base_name, before[:8])
        return _fail("tests-after-merge",
                     "merging broke the base, so it was put back:\n"
                     + str(after_merge.get("output", "")))

    head = _git(repo, "rev-parse", "HEAD").stdout.strip()

    # 6. and, if the project asks for it, publish the base.
    #
    # Landing only ever moved the *local* branch, which left origin behind by
    # one commit per landing. That is not only a tidiness problem. A task's
    # worktree is cut from `origin/HEAD`, so an implementor could not see work
    # that had already landed -- and two tasks independently implemented the
    # same fix, at roughly four and a half dollars each plus a reviewer each.
    # Publishing is what keeps the two in step, and so what stops that.
    #
    # Deliberately last, and deliberately after the post-merge suite. A base
    # that turns out to be broken is reset; publishing before proving it would
    # mean undoing a push, which is a force-push on a shared branch and a much
    # worse position than an unlanded branch.
    #
    # Off by default, per project. Pushing to a shared remote is an outward act
    # and the only step here that cannot be taken back quietly. This is not the
    # pull-request question DESIGN.md rules out -- nothing is proposed to
    # anyone -- but it is still a decision about someone else's repository, so
    # it is theirs to make.
    if publish:
        try:
            push = _git(repo, "push", "origin", base_name, timeout=300)
            failed, why = push.returncode != 0, (push.stderr or push.stdout)
        except subprocess.TimeoutExpired:
            # A push that timed out is genuinely ambiguous: it may have been
            # accepted and the acknowledgement lost. Guessing either way is
            # wrong, so it is resolved below by asking origin.
            failed, why = True, "the push timed out"
        # Ask origin rather than trusting the exit code, because a push can
        # fail *after* the remote accepted it -- and then resetting the base
        # would drop a commit that is already published, which is a worse mess
        # than the one this is preventing.
        if failed and _on_origin(repo, base_name) == head:
            log.warning("push of %s reported failure but origin has %s; "
                        "treating it as published", base_name, head[:8])
            failed = False
        if failed:
            # A base that failed to publish is not landed. Leaving the commit
            # on local main would recreate exactly the divergence publishing
            # exists to prevent, except now silently and with the reply saying
            # it worked. Put the base back; the branch is untouched and the
            # landing can be retried once whatever refused the push is dealt
            # with.
            _git(repo, "reset", "--hard", before)
            log.error("landing %s could not be published; reset %s back to %s",
                      branch, base_name, before[:8])
            return _fail("publish",
                         str(why) +
                         f"\n{base_name!r} could not be pushed to origin, so the "
                         "landing was undone rather than left unpublished")

    log.info("landed %s on %s (%s..%s)%s", branch, base_name, before[:8],
             head[:8], " and published" if publish else "")
    return {"landed": True, "stage": "done", "base": base_name,
            "before": before, "head": head, "published": bool(publish),
            "detail": ""}


def needs_a_person(outcome: dict) -> bool:
    """True when a landing was actually tried against git and refused.

    The distinction this draws is the whole point of the outcome being a record
    rather than a sentence. "The project never asked us to merge" and "we
    rebased, it conflicted, and the branch is still sitting there" both used to
    arrive as a string the caller could only print, so both ended the task the
    same way -- `done`, with eleven commits across five branches that no base
    had. Only the second needs someone.
    """
    return bool(outcome.get("eligible")) and not outcome.get("landed")


def summary(result: dict, branch: str) -> str:
    """One line for the thread."""
    if result.get("landed"):
        # Whether it was published is worth a word: "landed" meaning local-only
        # is exactly the ambiguity that let origin drift a dozen commits behind
        # without anyone noticing.
        where = ("pushed to origin" if result.get("published")
                 else "local only, not pushed")
        return (f":shipit: _Landed on *{result.get('base')}* "
                f"(`{result.get('head', '')[:8]}`, {where})._")
    if not result.get("eligible", True):
        # Never reached git, so there is no branch waiting and no output to
        # quote -- just the reason it was never a candidate.
        return f":hand: _Not landed: {result.get('detail') or result.get('stage')}._"
    return (f":hand: _Not landed ({result.get('stage')}). Branch `{branch}` is "
            f"waiting for you._\n```\n{result.get('detail', '')[-800:]}\n```")
