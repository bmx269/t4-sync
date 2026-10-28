"""t4 save -- commit pulled source to the repository, before deploying it.

The step between "I edited a layout" and "it is live". Deliberately a separate
command rather than something `push` does for you: a commit message is a
judgement about why a change was made, and a tool that writes one for you
teaches people to ignore them.
"""
import json

from ..compare import load_manifest
from ..errors import T4Error
from ..vcs import message_for, open_repo


def entries_for(project, env_name, pending):
    """Pair each pending path with the T4 record it maps onto."""
    manifest = load_manifest(project, env_name)
    env_dir = project.env_dir(env_name)
    found = []
    for name in pending:
        meta = None
        full = (project.root / name).resolve()
        try:
            rel = full.relative_to(env_dir.resolve())
        except ValueError:
            rel = None
        if rel is not None:
            meta = manifest.get(str(rel).replace("\\", "/"))
        found.append({"path": name, "meta": meta or {}})
    return found


def run(args, project):
    env_name = args.env or project.default_env()
    repo = open_repo(project)

    if not repo.available:
        raise T4Error(
            "%s\n\n"
            "`t4 save` records changes in git so a deployment can be traced\n"
            "back to a commit. Create a repository first:\n"
            "  git init && git add . && git commit -m \"Initial pull from T4\""
            % repo.reason)

    # Scope: the environment's pulled source, plus the project config, which
    # is where a channel or environment change lives. Tokens are gitignored by
    # `t4 init` and are never staged here.
    targets = [project.env_dir(env_name)]
    if args.config:
        targets.append(project.root / ".t4" / "config.json")
    if args.paths:
        targets = [project.root / p for p in args.paths]

    existing = [t for t in targets if t.exists()]
    if not existing:
        raise T4Error("Nothing to save: %s does not exist. Run `t4 pull` first."
                      % project.env_dir(env_name))

    pending = repo.dirty(existing)
    if not pending:
        sha, branch = repo.head()
        print("Nothing to save - %s is already committed on %s."
              % (env_name, branch or "this branch"))
        return 0

    entries = entries_for(project, env_name, pending)

    print("%d file(s) to commit for %s:\n" % (len(entries), env_name))
    for entry in sorted(entries, key=lambda e: e["path"]):
        meta = entry["meta"]
        print("  %-58s %s" % (
            entry["path"],
            "%s #%s" % (meta["endpoint"], meta["id"]) if meta.get("id")
            else "(not a pulled record)"))

    if args.dry_run:
        print("\n[dry run] nothing committed.")
        return 0

    message = message_for(env_name, entries, args.message)
    sha = repo.commit(existing, message)
    if not sha:
        print("\nNothing staged; working tree unchanged.")
        return 0

    _, branch = repo.head()
    print("\nCommitted %s on %s." % (sha[:8], branch or "(detached)"))
    print("Deploy it with: t4 push -e %s" % env_name)
    return 0
