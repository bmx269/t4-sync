"""t4 fixtures -- render a Content Layout with made-up content."""
import pathlib
import webbrowser

from .. import fixtures as fixturelib
from .. import tags as taglib
from ..compare import load_manifest
from ..errors import T4Error
from ..extract import read_text


def run(args, project):
    env_name = args.env or project.default_env()
    env_dir = str(project.env_dir(env_name))

    types = taglib.content_types(env_dir)
    if not types:
        raise T4Error("No content types pulled for %r.\n  t4 pull -e %s"
                      % (env_name, env_name))

    wanted = (args.type or "").strip().lower()
    if not wanted:
        print("Content types with a pulled layout:\n")
        for content_type in types:
            print("  %-44s %d element(s)"
                  % (content_type["name"], len(content_type["elements"])))
        print("\n  t4 fixtures \"<content type>\"")
        return 0

    matches = [c for c in types if wanted in (c["name"] or "").lower()]
    if not matches:
        raise T4Error("No content type matching %r." % args.type)
    if len(matches) > 1 and not any(c["name"].lower() == wanted for c in matches):
        print("%d content types match %r:" % (len(matches), args.type))
        for content_type in matches[:12]:
            print("  %s" % content_type["name"])
        return 1
    content_type = next((c for c in matches if c["name"].lower() == wanted),
                        matches[0])

    manifest = load_manifest(project, env_name)
    layouts = [(rel, meta) for rel, meta in sorted(manifest.items())
               if meta.get("endpoint") == "layout"
               and (meta.get("contentType") or "").lower()
               == content_type["name"].lower()]
    if not layouts:
        raise T4Error("No pulled Content Layout for %r." % content_type["name"])

    chosen = layouts[0]
    if args.format:
        named = [pair for pair in layouts
                 if (pair[1].get("name") or "").lower() == args.format.lower()]
        if not named:
            raise T4Error("No %r layout for %r. Available: %s"
                          % (args.format, content_type["name"],
                             ", ".join(meta.get("name") or "?"
                                       for _rel, meta in layouts)))
        chosen = named[0]
    elif len(layouts) > 1:
        print("%d layouts for %s; using %s. Pick another with --format."
              % (len(layouts), content_type["name"],
                 chosen[1].get("name") or chosen[0]))

    rel, meta = chosen
    source = read_text(project.env_dir(env_name) / rel)
    html, report = fixturelib.render(source, content_type)

    stylesheets = [s for s in (args.css or [])]
    out = fixturelib.page("%s (%s)" % (content_type["name"],
                                       meta.get("name") or "layout"),
                          html, report, stylesheets)

    target = pathlib.Path(args.out) if args.out else (
        project.root / ".t4" / "fixtures" / ("%s.html" % _slug(content_type["name"])))
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(out, encoding="utf-8")

    print("%s  (%s)" % (content_type["name"], meta.get("name") or "layout"))
    print("  %d element(s) filled with sample values" % len(set(report["filled"])))
    if report["skipped"]:
        print("  %d tag(s) left as placeholders:" % len(report["skipped"]))
        for original, reason in report["skipped"][:6]:
            print("     %s" % reason)
    if report["unknown"]:
        print("  %d tag(s) name an element this type does not have: %s"
              % (len(report["unknown"]),
                 ", ".join(str(n) for n in report["unknown"][:5])))
    print("\nWrote %s" % target)
    print("Sample content only - Terminal Four renders the real output.")

    if not args.no_open:
        webbrowser.open(target.resolve().as_uri())
    return 0


def _slug(value):
    import re
    return re.sub(r"[^a-z0-9]+", "-", str(value).lower()).strip("-") or "layout"
