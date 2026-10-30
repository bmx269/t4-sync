"""t4 fixtures -- render T4 tags with made-up content."""
import json
import pathlib
import sys
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

    # A layout named outright. The web UI sends this, because a row knows
    # exactly which file it is about -- where naming the content type and
    # the format has to be matched back to a file and can land on a
    # different one when two layouts share a name, or a layout has none.
    if getattr(args, "path", None):
        return _render_one(args, project, env_name, types, args.path)

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

    overrides = _read_values(args.values)
    stray = fixturelib.unknown_values(content_type, overrides)
    if stray:
        raise T4Error("%s has no element named %s.\n  Its elements: %s"
                      % (content_type["name"],
                         ", ".join(repr(name) for name in stray),
                         ", ".join(element["name"]
                                   for element in content_type["elements"])))
    values = fixturelib.values_for(content_type, overrides, args.lorem)

    # Tags being written, rather than a layout already pulled. The point of
    # these is to be seen before they go anywhere near the CMS, so there is
    # no manifest entry to look for.
    if args.tags:
        return _write(args, project, env_name, content_type,
                      _read_tags(args.tags), "tags", values)

    manifest = load_manifest(project, env_name)
    layouts = [(rel, meta) for rel, meta in sorted(manifest.items())
               if meta.get("endpoint") == "layout"
               and (meta.get("contentType") or "").lower()
               == content_type["name"].lower()]
    if not layouts:
        raise T4Error("No pulled Content Layout for %r.\n"
                      "  To render tags you are writing, use --tags."
                      % content_type["name"])

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
    return _write(args, project, env_name, content_type, source,
                  meta.get("name") or "layout", values)


def _render_one(args, project, env_name, types, rel):
    """Render one pulled layout, addressed by its path."""
    rel = rel.replace("\\", "/").strip("/")
    manifest = load_manifest(project, env_name)
    meta = manifest.get(rel)
    if meta is None:
        raise T4Error("%s is not a pulled file for %r." % (rel, env_name))
    if meta.get("endpoint") != "layout":
        raise T4Error("%s is a %s, not a Content Layout. Only layouts render."
                      % (rel, meta.get("endpoint") or "file"))

    wanted = (meta.get("contentType") or "").lower()
    content_type = next((c for c in types
                         if (c["name"] or "").lower() == wanted), None)
    if content_type is None:
        raise T4Error("%s belongs to content type %r, which is not pulled.\n"
                      "  t4 pull -e %s"
                      % (rel, meta.get("contentType"), env_name))

    overrides = _read_values(args.values)
    stray = fixturelib.unknown_values(content_type, overrides)
    if stray:
        raise T4Error("%s has no element named %s.\n  Its elements: %s"
                      % (content_type["name"],
                         ", ".join(repr(name) for name in stray),
                         ", ".join(element["name"]
                                   for element in content_type["elements"])))
    values = fixturelib.values_for(content_type, overrides, args.lorem)
    source = read_text(project.env_dir(env_name) / rel)
    return _write(args, project, env_name, content_type, source,
                  meta.get("name") or "layout", values)


def _read_tags(where):
    """The markup to render: a file, or stdin for '-'."""
    if where == "-":
        return sys.stdin.read()
    path = pathlib.Path(where)
    if not path.is_file():
        raise T4Error("No such file: %s" % where)
    return path.read_text(encoding="utf-8", newline="")


def _read_values(where):
    """{element name: value} from a JSON file, or {} if none was given."""
    if not where:
        return {}
    path = pathlib.Path(where)
    if not path.is_file():
        raise T4Error("No such file: %s" % where)
    try:
        loaded = json.loads(path.read_text(encoding="utf-8"))
    except ValueError as exc:
        raise T4Error("%s is not valid JSON: %s" % (where, exc))
    if not isinstance(loaded, dict):
        raise T4Error("%s should hold an object of {element name: value}, "
                      "not %s." % (where, type(loaded).__name__))
    return loaded


def _write(args, project, env_name, content_type, source, what, values):
    """Render, save and report, however the markup was arrived at."""
    html, report = fixturelib.render(source, content_type, values=values)

    stylesheets = [s for s in (args.css or [])]
    out = fixturelib.page("%s (%s)" % (content_type["name"], what),
                          html, report, stylesheets)

    target = pathlib.Path(args.out) if args.out else (
        project.root / ".t4" / "fixtures"
        / fixturelib.output_name(content_type["name"], what))
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(out, encoding="utf-8")

    print("%s  (%s)" % (content_type["name"], what))
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
