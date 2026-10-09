"""t4 edit -- copy a mirrored file into overrides/ so it can be edited.

The override layer works by path: a file at overrides/<same path> is served
instead of the mirrored one. Doing that by hand means getting a deep path
exactly right, so this does it, and lists what is already overridden.
"""
import pathlib
import shutil

from ..errors import T4Error
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
