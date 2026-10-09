"""t4 layouts -- which page layout renders what, and where its source lives.

Answers two questions that are otherwise tedious:

  t4 layouts                 every layout, how many mirrored pages use it, and
                             the local files pulled from it
  t4 layouts <page path>     the layout behind one page

Both read the `t4-layout-id` meta tag T4 publishes into every page and match it
against the pull manifest. Matching on the id rather than the name matters:
slugged layout names are not unique, so a name can point at the wrong file.
"""
import collections
import os

from ..errors import T4Error
from ..inspector import LAYOUT_ID_META, LAYOUT_META, LayoutIndex
from .edit import find_mirror, overrides_root


def scan_mirror(mirror):
    """Count pages per published layout name.

    Keyed on the NAME, not the id: T4 writes t4-layout into nearly every page
    but t4-layout-id into only a small minority, so counting by id reports
    almost nothing.
    """
    usage = collections.defaultdict(lambda: {"ids": set(), "pages": []})
    untagged = []
    for dirpath, _dirnames, filenames in os.walk(mirror):
        for filename in filenames:
            if not filename.endswith((".html", ".htm")):
                continue
            full = os.path.join(dirpath, filename)
            try:
                with open(full, encoding="utf-8", errors="replace") as fh:
                    head = fh.read(8192)      # the meta tags are in <head>
            except OSError:
                continue
            rel = os.path.relpath(full, mirror)
            name = LAYOUT_META.search(head)
            if not name:
                untagged.append(rel)
                continue
            entry = usage[name.group(1)]
            entry["pages"].append(rel)
            found_id = LAYOUT_ID_META.search(head)
            if found_id:
                entry["ids"].add(found_id.group(1))
    return usage, untagged


def run(args, project):
    env_name = args.env or project.default_env()
    index = LayoutIndex.from_project_dir(str(project.env_dir(env_name)))
    mirror = find_mirror(project, args.host_dir)
    overrides = overrides_root(project)

    if not index.by_id:
        print("No page layouts pulled yet for %r." % env_name)
        print("Run: t4 pull --env %s" % env_name)
        return 1

    # -- one page ----------------------------------------------------------
    if args.page:
        rel = args.page.replace("\\", "/").lstrip("/")
        candidate = os.path.join(mirror, rel)
        if os.path.isdir(candidate):
            candidate = os.path.join(candidate, "index.html")
        if not os.path.isfile(candidate):
            raise T4Error("Not in the mirror: %s" % rel)
        with open(candidate, encoding="utf-8", errors="replace") as fh:
            head = fh.read(8192)
        found_id = LAYOUT_ID_META.search(head)
        name = LAYOUT_META.search(head)
        if not found_id:
            print("%s\n  no t4-layout meta - T4 did not tag this page" % rel)
            return 0
        print("%s\n" % rel)
        print("  layout   %s  #%s" % (name.group(1) if name else "(unnamed)",
                                      found_id.group(1)))
        files = index.files_for(found_id.group(1))
        if not files:
            print("  source   not in the local pull - run `t4 pull`")
            return 0
        for path, _meta in files:
            marker = "  *" if (overrides / path).is_file() else ""
            print("  source   t4-source/%s/%s%s" % (env_name, path, marker))
        return 0

    # -- every layout ------------------------------------------------------
    usage, untagged = scan_mirror(mirror)
    print("Page layouts for %s\n" % env_name)
    print("%d layout(s) pulled; %d distinct layout name(s) published across the "
          "mirror\n" % (len(index.by_id), len(usage)))

    for published, entry in sorted(usage.items(), key=lambda kv: -len(kv[1]["pages"])):
        ids = sorted(entry["ids"])
        files, note = index.resolve(published, ids[0] if len(ids) == 1 else None)
        print("%5d pages  %s%s" % (len(entry["pages"]), published,
                                   "  #%s" % ", #".join(ids) if ids else ""))
        for rel, _meta in files:
            marker = "  *overridden" if (overrides / rel).is_file() else ""
            print("             %s%s" % (rel, marker))
        if note:
            print("             %s" % note)
        if not files and not note:
            print("             not in the local pull")
        print()

    pulled_names = sorted(index.by_name)
    unseen = [n for n in pulled_names
              if n not in usage and n.split(".")[0] not in usage]
    if unseen and args.all:
        print("%d pulled layout(s) with no page in the mirror:" % len(unseen))
        for name in unseen:
            print("       %s" % name)
        print()
    elif unseen:
        print("%d pulled layout(s) have no page in the mirror (--all to list "
              "them).\n" % len(unseen))

    if untagged:
        print("\n%d mirrored page(s) carry no t4-layout meta." % len(untagged))
        for rel in untagged[:5]:
            print("       %s" % rel)
        if len(untagged) > 5:
            print("       ... and %d more" % (len(untagged) - 5))
    return 0
