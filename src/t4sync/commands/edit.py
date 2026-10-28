"""t4 edit -- copy a mirrored file into overrides/ so it can be edited.

The override layer works by path: a file at overrides/<same path> is served
instead of the mirrored one. Doing that by hand means getting a deep path
exactly right, so this does it, and lists what is already overridden.

**It will not shadow a file that has been pulled.** A site's CSS and JS are
Media Library items, so `t4 pull` already brought them down as editable,
deployable source -- and an override of one of those is a second copy of the
same file that looks identical, serves identically, and silently never
deploys. That is a trap worth refusing rather than documenting: you can lose
an afternoon's work to it and the tool will cheerfully show you the change
the whole time.

So for anything with pulled source, `t4 edit` points at that file. Overrides
remain for what the pull does not cover -- a mirrored page, a third-party
script, anything you want to experiment on locally and never deploy.
"""
import pathlib
import shutil

from ..errors import T4Error
from ..extract import read_text, write_text
from ..sections import MediaSources
from .site import site_root


def overrides_root(project):
    return project.root / project.config.get("overrides_dir", "overrides")


def find_mirror(project, host_dir=None):
    root = site_root(project)
    if host_dir:
        candidate = root / host_dir
        if candidate.is_dir():
            return candidate
        raise T4Error("No mirror at %s" % candidate)
    candidates = sorted(p for p in root.glob("*") if p.is_dir()) if root.is_dir() else []
    if not candidates:
        raise T4Error("No mirror found in %s\nRun `t4 mirror` first." % root)
    return candidates[0]


def promote(project, rel, override, pulled, env_name):
    """Move an override's content into the pulled source, and drop the copy.

    The way out of having two copies of one file. Content is written through
    `write_text`, so CRLF survives: these files come from T4 with Windows
    line endings and rewriting them with LF would make every line of the
    push a change.
    """
    if not override.is_file():
        raise T4Error("Not overridden: %s" % rel)
    if not pulled:
        raise T4Error(
            "%s has no pulled source to promote into.\n"
            "Only Media Library items -- the CSS and JS `t4 pull` brings "
            "down -- can be\ndeployed. This file exists only in the mirror, "
            "so the override is the right\nplace for it." % rel)

    content = read_text(override)
    if read_text(pulled) == content:
        override.unlink()
        print("Identical to the pulled source; removed the duplicate copy.")
        print("  %s" % pulled)
        return 0

    write_text(pulled, content)
    override.unlink()
    print("Promoted into the pulled source:\n  %s\n" % pulled)
    print("The override is gone, so there is one file again. Next:")
    print("  t4 diff %s" % rel.rsplit("/", 1)[-1])
    print("  t4 save -e %s -m \"<why>\"" % env_name)
    print("  t4 push -e %s" % env_name)
    return 0


def run(args, project):
    overrides = overrides_root(project)

    if args.list or not args.path:
        existing = sorted(p for p in overrides.rglob("*") if p.is_file())
        if not existing:
            print("No overrides yet.")
            print("\n  t4 edit <path>        copy a mirrored file in to edit it")
            print("  t4 edit --list        show what is overridden")
            return 0
        print("Overriding %d file(s) in %s:\n" % (len(existing), overrides.name))
        for path in existing:
            rel = path.relative_to(overrides)
            print("  %s" % rel)
        print("\nDelete one to fall back to the mirrored version.")
        return 0

    mirror = find_mirror(project, args.host_dir)
    rel = args.path.replace("\\", "/").lstrip("/")
    source = mirror / rel
    target = overrides / rel

    env_name = getattr(args, "env", None) or project.default_env()
    env_dir = project.env_dir(env_name)
    pulled = MediaSources.from_env_dir(str(env_dir)).path_for(rel)

    if args.promote:
        return promote(project, rel, target, pulled, env_name)

    if args.revert:
        if not target.is_file():
            raise T4Error("Not overridden: %s" % rel)
        target.unlink()
        print("Removed override for %s" % rel)
        print("The mirrored version is served again.")
        return 0

    if not source.is_file():
        # Help with the common case of getting the path slightly wrong.
        stem = pathlib.Path(rel).name
        near = [p.relative_to(mirror) for p in mirror.rglob(stem)][:10]
        message = "Not in the mirror: %s" % rel
        if near:
            message += "\n\nDid you mean:\n" + "\n".join("  %s" % n for n in near)
        else:
            message += ("\n\nIf the file is only on the live site, `t4 serve` will "
                        "proxy and\ncache it; load the page once, then try again.")
        raise T4Error(message)

    if pulled and not args.force:
        print("Already pulled, and this is the file that deploys:\n")
        print("  %s\n" % pulled)
        print("`t4 serve` serves that file, so edit it directly: the change")
        print("shows in the browser, `t4 diff` reports it, `t4 push` sends it")
        print("to T4 and `t4 save` commits it.\n")
        print("An override would be a second copy of the same file -- served")
        print("the same, but never deployed and never committed.\n")
        print("  t4 edit --force %s     override it anyway (local only)" % rel)
        return 0

    if target.is_file() and not args.force:
        print("Already overridden: %s" % rel)
        print("Edit %s, or pass --force to reset it from the mirror." % target)
        return 0

    target.parent.mkdir(parents=True, exist_ok=True)
    shutil.copy2(source, target)
    print("Copied into overrides:\n  %s" % target)
    print("\nEdit it and reload - `t4 serve` refreshes the browser on save.")
    print("Remove the override with: t4 edit --revert %s" % rel)
    return 0
