"""Real-git tests for the explicit push lease (RHAI-3020).

A bare ``--force-with-lease`` trusts the local remote-tracking ref,
which anyone who can write ``.git`` can move or remap through
``remote.origin.fetch``. These tests tamper with exactly that and check
that an explicit expected value still refuses to overwrite a commit
someone else pushed.
"""

import subprocess
from pathlib import Path

import pytest

from agentic_ci.git import RemoteLease, push_branch

BRANCH = "autofix/fix-1"


@pytest.fixture(autouse=True)
def _isolated_git(monkeypatch, tmp_path):
    """Keep the developer's global and system git config out of the tests."""
    gitconfig = tmp_path / "global-gitconfig"
    gitconfig.write_text(
        "[user]\n\tname = Test\n\temail = test@example.com\n"
        "[commit]\n\tgpgsign = false\n[init]\n\tdefaultBranch = main\n"
    )
    monkeypatch.setenv("GIT_CONFIG_GLOBAL", str(gitconfig))
    monkeypatch.setenv("GIT_CONFIG_NOSYSTEM", "1")


def _git(repo: Path, *args: str) -> str:
    return subprocess.run(
        ["git", *args],
        cwd=str(repo),
        check=True,
        capture_output=True,
        text=True,
        stdin=subprocess.DEVNULL,
    ).stdout.strip()


def _commit(repo: Path, content: str) -> str:
    (repo / "app.py").write_text(content)
    _git(repo, "add", "app.py")
    _git(repo, "commit", "-q", "-m", content.strip())
    return _git(repo, "rev-parse", "HEAD")


def _remote_sha(origin: Path) -> str:
    return _git(origin, "rev-parse", f"refs/heads/{BRANCH}")


@pytest.fixture()
def repos(tmp_path):
    """A bare origin with BRANCH, the bot's clone and a human's clone."""
    origin = tmp_path / "origin.git"
    _git(tmp_path, "init", "-q", "--bare", str(origin))
    bot = tmp_path / "bot"
    _git(tmp_path, "init", "-q", "-b", BRANCH, str(bot))
    _git(bot, "remote", "add", "origin", str(origin))
    _commit(bot, "print('bot v1')\n")
    _git(bot, "push", "-q", "origin", BRANCH)
    human = tmp_path / "human"
    _git(tmp_path, "clone", "-q", "-b", BRANCH, str(origin), str(human))
    return origin, bot, human


def _race_and_tamper(origin: Path, bot: Path, human: Path) -> tuple[str, str]:
    """Record the lease, then let a human push and the working copy lie.

    Returns (recorded remote SHA, human's SHA). The bot's local
    tracking ref for BRANCH ends up at the human's commit, reached
    through a remapped ``remote.origin.fetch``, so a bare lease that
    trusts local state believes the remote is up to date.
    """
    recorded = _remote_sha(origin)
    human_sha = _commit(human, "print('human fix')\n")
    _git(human, "push", "-q", "origin", BRANCH)

    _commit(bot, "print('bot v2')\n")
    _git(bot, "config", "--replace-all", "remote.origin.fetch", "+refs/heads/*:refs/remotes/evil/*")
    _git(bot, "fetch", "-q", "origin")
    _git(bot, "update-ref", f"refs/remotes/origin/{BRANCH}", human_sha)
    return recorded, human_sha


def test_bare_lease_is_fooled_by_tampered_tracking_ref(repos):
    """Control: the default bare lease overwrites the human's push."""
    origin, bot, human = repos
    _recorded, human_sha = _race_and_tamper(origin, bot, human)

    assert push_branch(bot, branch=BRANCH, max_retries=0) is True
    assert _remote_sha(origin) != human_sha


def test_explicit_sha_keeps_human_push(repos, caplog):
    origin, bot, human = repos
    recorded, human_sha = _race_and_tamper(origin, bot, human)

    assert push_branch(bot, branch=BRANCH, max_retries=2, expected_remote_sha=recorded) is False
    assert _remote_sha(origin) == human_sha
    assert "no longer matches the lease" in caplog.text


def test_explicit_sha_ignores_ref_named_like_sha(repos):
    """A local ref spelled like the full SHA does not change the lease."""
    origin, bot, human = repos
    recorded, human_sha = _race_and_tamper(origin, bot, human)
    _git(bot, "update-ref", f"refs/tags/{recorded}", human_sha)

    assert push_branch(bot, branch=BRANCH, max_retries=0, expected_remote_sha=recorded) is False
    assert _remote_sha(origin) == human_sha


def test_explicit_sha_pushes_when_remote_unchanged(repos):
    origin, bot, _human = repos
    recorded = _remote_sha(origin)
    _git(bot, "commit", "-q", "--amend", "-m", "rewritten")
    new_sha = _git(bot, "rev-parse", "HEAD")
    # Tampering alone must not block a push whose lease holds.
    _git(bot, "update-ref", f"refs/remotes/origin/{BRANCH}", new_sha)

    assert push_branch(bot, branch=BRANCH, max_retries=0, expected_remote_sha=recorded) is True
    assert _remote_sha(origin) == new_sha
    assert _git(bot, "rev-parse", "--abbrev-ref", f"{BRANCH}@{{upstream}}")


def test_absent_creates_branch(repos):
    origin, bot, _human = repos
    _git(bot, "checkout", "-q", "-b", "autofix/new")
    new_sha = _commit(bot, "print('new')\n")

    assert (
        push_branch(
            bot, branch="autofix/new", max_retries=0, expected_remote_sha=RemoteLease.ABSENT
        )
        is True
    )
    assert _git(origin, "rev-parse", "refs/heads/autofix/new") == new_sha


def test_absent_keeps_branch_someone_created(repos):
    origin, bot, human = repos
    _git(human, "checkout", "-q", "-b", "autofix/new")
    human_sha = _commit(human, "print('human first')\n")
    _git(human, "push", "-q", "origin", "autofix/new")
    _git(bot, "checkout", "-q", "-b", "autofix/new")
    _commit(bot, "print('bot')\n")
    # The working copy claims it already saw the human's branch, which
    # would satisfy a bare lease.
    _git(bot, "fetch", "-q", "origin")
    _git(bot, "update-ref", "refs/remotes/origin/autofix/new", human_sha)

    assert (
        push_branch(
            bot, branch="autofix/new", max_retries=2, expected_remote_sha=RemoteLease.ABSENT
        )
        is False
    )
    assert _git(origin, "rev-parse", "refs/heads/autofix/new") == human_sha


@pytest.mark.parametrize("expected", ["recorded", RemoteLease.ABSENT])
def test_explicit_lease_never_pushes_same_named_tag(repos, expected):
    """With the local branch gone, a tag named like it must not be pushed.

    A bare ``<branch>`` refspec would resolve to ``refs/tags/<branch>``
    and create that tag on the remote, where the lease on
    ``refs/heads/<branch>`` does not apply.
    """
    origin, bot, _human = repos
    recorded = _remote_sha(origin)
    lease = recorded if expected == "recorded" else expected
    _git(bot, "checkout", "-q", "--detach")
    _git(bot, "tag", BRANCH)
    _git(bot, "branch", "-q", "-D", BRANCH)

    assert push_branch(bot, branch=BRANCH, max_retries=0, expected_remote_sha=lease) is False
    assert _git(origin, "tag", "--list") == ""
    assert _remote_sha(origin) == recorded
