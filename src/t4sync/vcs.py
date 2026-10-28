"""Git, as the step between editing a layout and deploying it.

A push writes straight into the CMS database. T4 keeps its own history, but it
is per-record, lives behind the UI, and says nothing about why a change was
made or what else changed with it. So the record of a deployment has to be
kept on this side, and the obvious place is the repository the pulled source
already sits in.

The shape this takes:

    t4 save      commit the pulled source, with a message naming the records
    t4 push      refuses to deploy a file whose working copy is uncommitted

which makes "what is on test" answerable by a commit, and makes a deployment
revertable -- `git revert` restores the file, and `t4 push` sends it back.

Git is shelled out to rather than wrapped in a library: a dependency is not
worth it for six commands, and `git` is already installed anywhere this tool
is useful. Every call is explicit about its working directory, so the tool is
never at the mercy of where it was invoked from.
"""
import os
import subprocess

from .errors import T4Error

# Written into the commit message so a deployment can be found later with
# `git log --grep`. Kept short: it is read by people, not parsed.
TRAILER = "T4-Env"


class Repo:
    """A git working tree, or a stand-in that reports it is not one."""

    def __init__(self, root, available=True, reason=None):
        self.root = str(root)
        self.available = available
        self.reason = reason

    @classmethod
    def at(cls, root):
        """Open the repository containing `root`, or report why there is none."""
        root = str(root)
        try:
            out = _run(["git", "rev-parse", "--show-toplevel"], root)
        except FileNotFoundError:
            return cls(root, False, "git is not installed")
        except subprocess.CalledProcessError:
            return cls(root, False, "%s is not inside a git repository" % root)
        return cls(out.strip() or root)

    # -- reading -----------------------------------------------------------

    def rel(self, path):
        """A path relative to the repository root, in git's own form.

        Both sides are resolved first. `git rev-parse --show-toplevel` returns
        a real path, so on a machine where the project sits under a symlink --
        /tmp on macOS, /home on some Linux setups -- comparing it against the
        path the caller held produces a relpath full of `../..` that git then
        rejects as outside the repository.
        """
        return os.path.relpath(os.path.realpath(str(path)),
                               os.path.realpath(self.root)).replace(os.sep, "/")

    def status(self, paths=None):
        """[(code, path)] for everything uncommitted under `paths`.

        `code` is git's two-character porcelain status, so ' M' is modified
        and '??' untracked -- both count as "not committed yet", which is the
        only distinction push needs to make.
        """
        # -uall: by default git collapses an untracked directory to its name,
        # so a first pull would report "t4-source/" rather than the files the
        # push gate needs to match against.
        cmd = ["git", "status", "--porcelain", "-z", "-uall", "--"]
        cmd += [self.rel(p) for p in (paths or [self.root])]
        raw = _run(cmd, self.root)
        found, fields = [], [f for f in raw.split("\0") if f]
        index = 0
        while index < len(fields):
            entry = fields[index]
            code, _, name = entry[:2], entry[2:3], entry[3:]
            # A rename reports its source as a second field; skip it, the
            # destination is the path that matters here.
            if code and code[0] == "R":
                index += 1
            found.append((code, name))
            index += 1
        return found

    def dirty(self, paths=None):
        """Paths under `paths` with uncommitted changes."""
        return [name for _code, name in self.status(paths)]

    def head(self):
        """(sha, branch) for the current commit, or (None, branch) if unborn."""
        try:
            sha = _run(["git", "rev-parse", "HEAD"], self.root).strip()
        except subprocess.CalledProcessError:
            sha = None                      # a repository with no commits yet
        # --show-current rather than `rev-parse --abbrev-ref HEAD`: the latter
        # fails on a repository whose first commit has not been made, which is
        # exactly the state a project is in right after `t4 pull`.
        try:
            branch = _run(["git", "branch", "--show-current"], self.root).strip()
        except subprocess.CalledProcessError:
            branch = None
        return sha, branch or None

    def describe(self, paths=None):
        """A one-line summary for `t4 status`.

        `paths` scopes the pending count. A project repository usually holds
        more than the pulled source -- notes, exports, a scratch directory --
        and counting those as "uncommitted" would report a number that has
        nothing to do with what is about to be deployed.
        """
        if not self.available:
            return self.reason
        sha, branch = self.head()
        pending = len(self.dirty(paths))
        return "%s at %s, %s" % (
            branch or "(detached)", sha[:8] if sha else "no commits yet",
            "clean" if not pending else "%d file(s) uncommitted" % pending)

    # -- writing -----------------------------------------------------------

    def commit(self, paths, message):
        """Stage `paths` and commit them. Returns the new sha.

        Only the given paths are staged, so a commit made here cannot sweep up
        unrelated work someone had half-staged in the same tree.
        """
        rel = [self.rel(p) for p in paths]
        _run(["git", "add", "--"] + rel, self.root)
        staged = _run(["git", "diff", "--cached", "--name-only", "--"] + rel,
                      self.root).strip()
        if not staged:
            return None
        _run(["git", "commit", "-m", message, "--only", "--"] + rel, self.root)
        return _run(["git", "rev-parse", "HEAD"], self.root).strip()

    def init(self):
        _run(["git", "init"], self.root)
        return Repo.at(self.root)


def _run(cmd, cwd):
    """Run a git command, raising on failure with git's own stderr attached."""
    proc = subprocess.run(cmd, cwd=cwd, stdout=subprocess.PIPE,
                          stderr=subprocess.PIPE)
    if proc.returncode != 0:
        raise subprocess.CalledProcessError(
            proc.returncode, cmd, proc.stdout,
            proc.stderr.decode("utf-8", errors="replace"))
    return proc.stdout.decode("utf-8", errors="replace")


def open_repo(project):
    return Repo.at(project.root)


def require_clean(project, env_name, paths, allow_dirty=False):
    """Stop a push whose source is not committed.

    Returns the repository so the caller can record the commit it deployed
    from. A project that is not a git repository is not blocked: the tool
    predates this and works without one -- but it says so, because silently
    deploying untracked work is exactly what this is meant to prevent.
    """
    repo = open_repo(project)
    if not repo.available:
        print("Version control: %s - deploying without a commit record.\n"
              % repo.reason)
        return repo

    if project.config.get("require_commit") is False or allow_dirty:
        return repo

    pending = repo.dirty(paths)
    if not pending:
        return repo

    raise T4Error(
        "%d file(s) are not committed:\n  %s\n\n"
        "Commit them before deploying, so what is on %s matches a commit:\n"
        "  t4 save -e %s -m \"<why>\"\n\n"
        "To deploy anyway: t4 push --allow-dirty, or set \"require_commit\": "
        "false in .t4/config.json."
        % (len(pending), "\n  ".join(sorted(pending)[:20]), env_name, env_name))


def message_for(env_name, entries, summary=None):
    """A commit message that names the records the files map onto.

    A reviewer looking at `contentLayout/action-boxes--texthtml.html` cannot
    tell which T4 record it is, and the filename is derived, not authoritative.
    The manifest knows, so the commit says.
    """
    subject = summary or ("t4: %d change(s) for %s" % (len(entries), env_name))
    lines = [subject, ""]
    for entry in sorted(entries, key=lambda e: e["path"]):
        meta = entry.get("meta") or {}
        lines.append("  %s  ->  %s #%s%s"
                     % (entry["path"], meta.get("endpoint", "?"),
                        meta.get("id", "?"),
                        "  (%s)" % meta["name"] if meta.get("name") else ""))
    lines += ["", "%s: %s" % (TRAILER, env_name)]
    return "\n".join(lines)
