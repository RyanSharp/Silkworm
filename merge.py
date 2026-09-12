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
  merge    -- fast-forward only, so history stays linear and the merge itself
              cannot introduce a resolution nobody reviewed.
  retest   -- again, on the base, because a fast-forward can still break a
              repository whose tests depend on files outside the diff.
  revert   -- if that fails, put the base back exactly where it was. A broken
              main is much worse than an unlanded branch.

It refuses rather than guesses: a dirty checkout, a conflicting rebase or a
missing test command all stop the landing and hand it back with a reason.
"""

import logging
import subprocess

log = logging.getLogger("silkworm.merge")


def _git(cwd, *args, timeout: int = 300):
    return subprocess.run(["git", *args], cwd=str(cwd), capture_output=True,
                          text=True, timeout=timeout)


def _fail(stage: str, detail: str) -> dict:
    return {"landed": False, "stage": stage, "detail": detail.strip()[-600:]}


def land(worktree, repo, branch: str, base: str, run_tests) -> dict:
    """Rebase, retest, fast-forward, retest, and revert if that broke it.

    `run_tests(cwd)` is injected so the caller owns what "the tests" means and
    this stays testable without one.
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

    base_name = base.split("/")[-1] if base else "main"
    on = _git(repo, "rev-parse", "--abbrev-ref", "HEAD").stdout.strip()
    if on != base_name:
        return _fail("base-branch",
                     f"the checkout is on {on!r}, not {base_name!r}")

    _git(repo, "fetch", "--quiet", "origin", timeout=120)
    before = _git(repo, "rev-parse", "HEAD").stdout.strip()

    # 1. bring the work up to date with where the base actually is now
    reb = _git(worktree, "rebase", base)
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

    # 3. fast-forward only: no merge commit resolving anything unreviewed
    ff = _git(repo, "merge", "--ff-only", branch)
    if ff.returncode != 0:
        return _fail("merge", (ff.stderr or ff.stdout) +
                     "\nthe base moved again mid-landing")

    # 4. and prove the base itself is still sound
    after_merge = run_tests(repo)
    if not after_merge.get("ok"):
        _git(repo, "reset", "--hard", before)
        log.error("landing %s broke %s; reset back to %s", branch, base_name, before[:8])
        return _fail("tests-after-merge",
                     "merging broke the base, so it was put back:\n"
                     + str(after_merge.get("output", "")))

    head = _git(repo, "rev-parse", "HEAD").stdout.strip()
    log.info("landed %s on %s (%s..%s)", branch, base_name, before[:8], head[:8])
    return {"landed": True, "stage": "done", "base": base_name,
            "before": before, "head": head, "detail": ""}


def summary(result: dict, branch: str) -> str:
    """One line for the thread."""
    if result.get("landed"):
        return (f":shipit: _Landed on *{result.get('base')}* "
                f"(`{result.get('head', '')[:8]}`)._")
    return (f":hand: _Not landed ({result.get('stage')}). Branch `{branch}` is "
            f"waiting for you._\n```\n{result.get('detail', '')[-800:]}\n```")
