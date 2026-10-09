"""t4 preview -- what a local layout edit changes in the published output.

Shows the difference between the page as T4 last published it and the page as
it would look with the layouts currently on disk. It answers "what will this
edit do" without a publish cycle, and without a browser.

It is not a substitute for T4's own Preview. This reuses the output each tag
produced when the page was last published, so it is accurate about markup you
have changed and silent about anything that changed inside T4 since the mirror
was taken. Where it cannot be accurate, it says so.
"""
import difflib
import os
import sys

from ..errors import T4Error
from ..inspector import LAYOUT_ID_META, LAYOUT_META, LayoutIndex
from ..render import PreviewEngine
from .edit import find_mirror


def run(args, project):
    env_name = args.env or project.default_env()
    env_dir = str(project.env_dir(env_name))
    mirror = find_mirror(project, args.host_dir)

    index = LayoutIndex.from_project_dir(env_dir)
    engine = PreviewEngine(env_dir, index,
                           span_markers=project.config.get("span_markers"),
                           env_name=env_name)
    if not engine.available:
        raise T4Error("No pulled layouts for %r.\nRun: t4 pull --env %s"
                      % (env_name, env_name))

    rel = (args.page or "").replace("\\", "/").strip("/")
    candidate = os.path.join(mirror, rel) if rel else str(mirror)
    if os.path.isdir(candidate):
        candidate = os.path.join(candidate, "index.html")
    if not os.path.isfile(candidate):
        raise T4Error("Not in the mirror: %s" % (rel or "index.html"))

    with open(candidate, encoding="utf-8", newline="") as fh:
        page = fh.read()

    name = LAYOUT_META.search(page)
    found_id = LAYOUT_ID_META.search(page)
    rendered, status = engine.apply(page,
                                    name.group(1) if name else None,
                                    found_id.group(1) if found_id else None)

    print("%s" % (rel or "index.html"))
    print("  layout  %s%s" % (name.group(1) if name else "(none)",
                              "  #%s" % found_id.group(1) if found_id else ""))

    if not status:
        print("\nNo local layout edits apply to this page - it would publish "
              "as it already is.")
        return 0

    for applied in status["applied"]:
        print("  edited  %s" % applied)

    for kind, detail in status["problems"]:
        if kind == "inseparable":
            print("\n  WARNING: markup was placed around tags whose output cannot be")
            print("           separated, so what follows wraps the whole run:")
            for tag in detail:
                print("             %s" % tag)
            print("           T4 will not publish it this way.")
        elif kind == "unpublished":
            print("\n  NOT SHOWN: %s has no published output to reuse." % detail)
        else:
            print("\n  %s: %s" % (kind, detail))

    if args.out:
        with open(args.out, "w", encoding="utf-8", newline="") as fh:
            fh.write(rendered)
        print("\nWrote %s (%d bytes)" % (args.out, len(rendered)))
        return 0

    diff = list(difflib.unified_diff(
        page.splitlines(keepends=True), rendered.splitlines(keepends=True),
        fromfile="published", tofile="with local edits",
        n=args.context))
    if not diff:
        print("\nThe edits produce no change in the output.")
        return 0

    print("\n%d line(s) of output change:\n" % sum(
        1 for line in diff if line.startswith(("+", "-"))
        and not line.startswith(("+++", "---"))))
    sys.stdout.writelines(diff)
    return 0
