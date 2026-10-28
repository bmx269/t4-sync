"""What happened to this project, and when.

"When did we last pull?" and "has this been pushed since Tuesday?" are the
questions people actually ask, and neither the CLI nor T4 could answer them:
T4 records that a record changed, not that *you* changed it from here, and a
file's mtime says when it was written, not whether that write was a pull
overwriting your work or a push sending it out.

So each command that moves data appends a line here. Append-only JSONL rather
than a rewritten summary file, for the same reason `deployed.jsonl` is: two
people working from different checkouts produce a file git can merge, and a
truncated write loses one line rather than the lot.

Nothing in here is authoritative about T4's state. It is a log of what this
project did, which is exactly the thing nothing else records.
"""
import datetime
import json
import os

LOG = "history.jsonl"

# Keeping the whole log but only ever showing the tail: a year of pulls is
# perhaps 500 lines, small enough that pruning would cost more than it saves.
TAIL = 400


def path_for(project):
    return project.root / ".t4" / LOG


def now():
    return datetime.datetime.now().astimezone().isoformat(timespec="seconds")


def record(project, action, env=None, **fields):
    """Append one event. Never raises: logging must not fail a command."""
    entry = {"time": now(), "action": action}
    if env:
        entry["env"] = env
    entry.update({k: v for k, v in fields.items() if v is not None})
    try:
        target = path_for(project)
        target.parent.mkdir(parents=True, exist_ok=True)
        with open(target, "a", encoding="utf-8") as fh:
            fh.write(json.dumps(entry, sort_keys=True) + "\n")
    except OSError:
        pass
    return entry


def read(project, limit=TAIL):
    """The most recent events, newest last. Bad lines are skipped, not fatal."""
    target = path_for(project)
    try:
        with open(target, encoding="utf-8") as fh:
            lines = fh.readlines()
    except OSError:
        return []
    found = []
    for line in lines[-limit:]:
        line = line.strip()
        if not line:
            continue
        try:
            found.append(json.loads(line))
        except ValueError:
            continue
    return found


def latest(project, env=None):
    """The most recent event of each kind, as {action: entry}.

    This is what the panel shows: last pulled, last pushed, last committed,
    each with its own timestamp, rather than one "last activity" line that
    hides which of them it was.
    """
    found = {}
    for entry in read(project):
        if env and entry.get("env") and entry["env"] != env:
            continue
        found[entry["action"]] = entry
    return found


def ago(stamp):
    """'3 minutes ago' for a stored timestamp, or None if it will not parse.

    Relative, because the question is always how stale something is, and
    nobody reads an ISO timestamp and subtracts.
    """
    if not stamp:
        return None
    try:
        when = datetime.datetime.fromisoformat(stamp)
    except (TypeError, ValueError):
        return None
    if when.tzinfo is None:
        when = when.astimezone()
    seconds = (datetime.datetime.now().astimezone() - when).total_seconds()
    if seconds < 0:
        return "just now"
    for limit, size, name in ((60, 1, "second"), (3600, 60, "minute"),
                              (86400, 3600, "hour"), (604800, 86400, "day")):
        if seconds < limit:
            count = int(seconds // size)
            if count <= 1 and name == "second":
                return "just now"
            return "%d %s%s ago" % (count, name, "" if count == 1 else "s")
    return "%d weeks ago" % int(seconds // 604800)


def last_edit(project, env_name):
    """When a file under the pulled source was last modified by hand.

    Read from the filesystem rather than logged: an edit happens in an editor,
    which is not going to tell us about it. `_raw` and `_backup` are skipped
    because a pull writes those, so including them would report every pull as
    an edit.
    """
    root = project.env_dir(env_name)
    if not root.is_dir():
        return None, None
    newest, name = 0, None
    skip = {"_raw", "_backup"}
    for dirpath, dirnames, filenames in os.walk(root):
        dirnames[:] = [d for d in dirnames if d not in skip]
        for filename in filenames:
            if filename.startswith(".") or filename == "_manifest.json":
                continue
            full = os.path.join(dirpath, filename)
            try:
                stamp = os.stat(full).st_mtime
            except OSError:
                continue
            if stamp > newest:
                newest, name = stamp, os.path.relpath(full, root)
    if not newest:
        return None, None
    return (datetime.datetime.fromtimestamp(newest).astimezone()
            .isoformat(timespec="seconds"), name.replace(os.sep, "/"))
