"""t4 push -- send reviewed local changes back to T4, one confirmation each."""
import datetime
import json
import sys

from ..api import Client
from ..compare import compare, load_manifest, remote_path, unified, write_field
from ..errors import T4Error
from .. import history
from ..vcs import require_clean

DEPLOY_LOG = "deployed.jsonl"


def backup(project, env_name, entry, stamp):
    """Snapshot the untouched remote record before modifying it."""
    meta = entry["meta"]
    out = project.env_dir(env_name) / "_backup" / stamp / meta["endpoint"]
    out.mkdir(parents=True, exist_ok=True)
    path = out / ("%s.json" % meta["id"])
    if not path.exists():
        path.write_text(json.dumps(entry["record"], indent=2, sort_keys=True),
                        encoding="utf-8")
    return path


def record_deploy(project, env_name, entry, sha, branch):
    """Append what was deployed, from which commit.

    T4's own history says a record changed; it does not say which commit the
    markup came from. Appending rather than rewriting keeps the log readable
    when two people deploy from different checkouts, and makes it a file git
    can merge.
    """
    path = project.root / ".t4" / DEPLOY_LOG
    path.parent.mkdir(parents=True, exist_ok=True)
    meta = entry["meta"]
    line = json.dumps({
        "time": datetime.datetime.now().astimezone().isoformat(timespec="seconds"),
        "env": env_name,
        "path": entry["path"],
        "endpoint": meta.get("endpoint"),
        "id": meta.get("id"),
        "field": meta.get("field"),
        "commit": sha,
        "branch": branch,
    }, sort_keys=True)
    with open(path, "a", encoding="utf-8") as fh:
        fh.write(line + "\n")


def run(args, project):
    env_name = args.env or project.default_env()
    env = project.env(env_name)

    if not env.get("push_allowed", False):
        raise T4Error(
            "Push is disabled for %r.\n  %s\n"
            "Set \"push_allowed\": true for this environment in .t4/config.json\n"
            "to change that. It is off by default so that a production instance\n"
            "cannot be written to by accident." % (env_name, env["label"]))

    client = Client(env, project.require_token(env_name), timeout=args.timeout)
    manifest = load_manifest(project, env_name)

    print("Target: %s%s\n" % (env["label"], "   [DRY RUN]" if args.dry_run else ""))
    results = compare(project, env_name, client, manifest, args.paths or None,
                      progress_label="Checking against T4")
    changed = [e for e in results if e["state"] in ("changed", "conflict", "behind")]
    conflicts = [e for e in results if e["state"] == "conflict"]
    behind = [e for e in results if e["state"] == "behind"]

    # Both sides moved since the pull. Pushing discards whoever edited it in
    # T4, and on a shared instance that is the likeliest way to lose work.
    if conflicts and not args.ignore_conflicts:
        print("%d file(s) changed in T4 as well as here since the last pull:\n"
              % len(conflicts))
        for entry in conflicts:
            print("  %s" % entry["path"])
        print("\nPushing would discard the T4 side. Look first:")
        print("  t4 diff --show %s" % conflicts[0]["path"])
        print("\nThen either take theirs    t4 pull --force <path>")
        print("     or keep yours        t4 push --ignore-conflicts <path>")
        if not behind:
            return 2
        print()

    if behind:
        # Only T4 moved. Pushing here puts the old version back, which looks
        # like a successful deploy and is a silent revert.
        print("%d file(s) changed in T4 but not here; pushing would revert "
              "them:" % len(behind))
        for entry in behind:
            print("  %s" % entry["path"])
        print("Run `t4 pull` to take those, or name the files you do mean "
              "to push.\n")
        changed = [e for e in changed if e["state"] != "behind"]
        if not changed:
            return 0

    for entry in [e for e in results if e["state"] == "error"]:
        print("  ! %s  %s" % (entry["path"], entry.get("error", "")))

    if not changed:
        print("Nothing to push - %d file(s) checked, all identical."
              % len([e for e in results if e["state"] == "same"]))
        return 0

    print("%d file(s) differ.\n" % len(changed))

    # Checked after the comparison, so the gate names the files actually
    # about to be deployed rather than everything that happens to be dirty.
    repo = require_clean(project, env_name,
                         [project.root / e["path"] for e in changed],
                         allow_dirty=args.allow_dirty or args.dry_run)
    sha, branch = repo.head() if repo.available else (None, None)
    if sha and not args.dry_run:
        print("Deploying from %s (%s)\n" % (sha[:8], branch or "detached"))

    stamp = datetime.datetime.now().strftime("%Y%m%d-%H%M%S")
    pushed = skipped = failed = 0

    for index, entry in enumerate(changed, 1):
        meta = entry["meta"]
        print("=" * 72)
        print("[%d/%d] %s%s" % (index, len(changed), entry["path"],
                                 "   *** ALSO CHANGED IN T4 ***"
                                 if entry["state"] == "conflict" else ""))
        print("       %s id=%s field=%s  (%s)"
              % (meta["endpoint"], meta["id"], meta["field"], meta.get("name")))
        print("=" * 72)
        for line in unified(entry, from_label="T4 (current)", to_label="local (to push)"):
            sys.stdout.write(line if line.endswith("\n") else line + "\n")

        if args.dry_run:
            print("\n[dry run] would push\n")
            skipped += 1
            continue

        if args.yes:
            answer = "y"
        else:
            try:
                answer = input("\nPush this change to %s? [y/N/q] " % env_name).strip().lower()
            except (EOFError, KeyboardInterrupt):
                print("\nStopped.")
                break
        if answer in ("q", "quit"):
            print("Stopped.")
            break
        if answer not in ("y", "yes"):
            print("Skipped.\n")
            skipped += 1
            continue

        saved = backup(project, env_name, entry, stamp)
        record = write_field(dict(entry["record"]), meta["field"], entry["local"])
        try:
            client.put_json(remote_path(meta), record)
            print("Pushed. Backup: %s\n" % saved.relative_to(project.root))
            record_deploy(project, env_name, entry, sha, branch)
            pushed += 1
        except Exception as exc:
            print("FAILED: %s\n  remote unchanged; backup kept at %s\n"
                  % (exc, saved.relative_to(project.root)))
            failed += 1

    print("\n%d pushed, %d skipped, %d failed." % (pushed, skipped, failed))
    if pushed or failed:
        history.record(project, "push", env=env_name, files=pushed,
                       failures=failed or None, commit=sha)
    return 1 if failed else 0
