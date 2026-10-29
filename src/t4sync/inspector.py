"""A small overlay showing which T4 layout rendered a page, and what to edit.

T4 publishes `<meta name="t4-layout">` and `<meta name="t4-layout-id">` into
every page it renders. The id maps directly onto the pull manifest, so a page
being viewed locally can be traced back to the exact file on disk that
produced it -- which is otherwise guesswork across 58 layouts with names like
`basic_page.production` and `standard_alt_no_menu`.

The panel also lists the stylesheets and scripts the page loads, marking which
are already overridden, because that is the other half of "what do I edit".
"""
import collections
import html
import json
import os
import re

from .extract import local_path

# Grouped rather than passed positionally: this is handed from the request
# handler through transform_html to build_panel, and a bare tuple made that
# chain easy to get wrong.
INSPECTOR_FIELDS = (
    "index overrides_dir env_name mirror_dir url_path mode "
    "content_types component_markers resolver sections content_items "
    "content_layouts media").split()

InspectorContext = collections.namedtuple("InspectorContext", INSPECTOR_FIELDS)

# Derived from the field list rather than written out: a positional tuple of
# Nones has to be recounted every time a field is added, and getting it wrong
# fails as a missing attribute far from the cause.
InspectorContext.__new__.__defaults__ = tuple(
    {"url_path": "/", "mode": "both"}.get(name)
    for name in INSPECTOR_FIELDS[1:])

LAYOUT_META = re.compile(
    r'<meta\s+name=["\']t4-layout["\']\s+content=["\']([^"\']*)["\']', re.I)
LAYOUT_ID_META = re.compile(
    r'<meta\s+name=["\']t4-layout-id["\']\s+content=["\']([^"\']*)["\']', re.I)
ASSET_RE = re.compile(
    r'<(?:link[^>]+href|script[^>]+src)=["\']([^"\']+\.(?:css|js))["\']', re.I)

# T4 does not delimit content items in published output. Many sites adopt a
# convention of commenting them in their content layouts, and where one exists
# it can be read back to mark where each component starts. These are the
# default patterns; a site with different conventions sets `component_markers`
# in .t4/config.json. A pattern needs an `id` group, a `name` group, or both.
# `[^>]` rather than `[^>-]`: a hyphen is ordinary in a name (Footer-Contact),
# and the pattern cannot overrun the comment anyway because `-->` contains `>`.
DEFAULT_COMPONENT_MARKERS = [
    (r"<!--\s*ct:(?P<id>\d+)\s*(?P<name>[^>]*?)\s*-->", "contenttype"),
    (r"<!--\s*ct-(?P<id>\d+)[ -](?P<name>[^>]*?)\s*-->", "contenttype"),
    (r"<!--\s*content type:\s*(?P<name>[^>]+?)\s*-->", "contenttype"),
    (r"<!--\s*n:(?P<name>[^>(]+?)\s*\((?P<id>\d+)\)\s*-->", "navigation"),
]


class ContentTypeIndex:
    """Maps a T4 content type id to the file pulled for it.

    Built by reading the pulled records rather than the manifest: content
    types have no template field, so they are written as JSON and the manifest
    -- which exists to tell `push` which field to write back -- does not list
    them.
    """

    def __init__(self, by_id=None, by_name=None):
        self.by_id = by_id or {}
        self.by_name = by_name or {}

    @classmethod
    def from_env_dir(cls, env_dir, endpoint="contenttype"):
        by_id, by_name = {}, {}
        if not env_dir:
            return cls()
        directory = os.path.join(env_dir, endpoint)
        if not os.path.isdir(directory):
            return cls()
        for filename in os.listdir(directory):
            if not filename.endswith(".json"):
                continue
            try:
                with open(os.path.join(directory, filename), encoding="utf-8") as fh:
                    record = json.load(fh)
            except (OSError, ValueError):
                continue
            rel = "%s/%s" % (endpoint, filename)
            entry = (rel, record.get("name"), record.get("alias"))
            if record.get("id") is not None:
                by_id[str(record["id"])] = entry
            for key in (record.get("name"), record.get("alias")):
                if key:
                    by_name.setdefault(key.strip().lower(), entry)
        return cls(by_id, by_name)

    def lookup(self, marker_id=None, marker_name=None):
        if marker_id and str(marker_id) in self.by_id:
            return self.by_id[str(marker_id)]
        if marker_name:
            return self.by_name.get(marker_name.strip().lower())
        return None


class ContentLayoutIndex:
    """Content Layouts grouped by the content type they belong to.

    A content type is a field definition; the markup it renders lives in its
    Content Layouts, so "which file holds this component's HTML" is answered
    here and not by the content type record. The manifest records the type by
    name (`contentType`) rather than by id, so the lookup is by name, with the
    alias tried as well -- T4 reports and exports disagree about which of the
    two they print.
    """

    def __init__(self, manifest=None):
        self.by_type = {}
        for rel, meta in (manifest or {}).items():
            if meta.get("endpoint") != "layout":
                continue
            key = (meta.get("contentType") or "").strip().lower()
            if not key:
                continue
            self.by_type.setdefault(key, []).append((rel, meta.get("name") or ""))
        for entries in self.by_type.values():
            entries.sort()

    @classmethod
    def from_env_dir(cls, env_dir):
        if not env_dir:
            return cls()
        path = os.path.join(env_dir, "_manifest.json")
        try:
            with open(path, encoding="utf-8") as fh:
                return cls(json.load(fh))
        except (OSError, ValueError):
            return cls()

    @property
    def available(self):
        return bool(self.by_type)

    def for_type(self, *names):
        for name in names:
            if name and name.strip().lower() in self.by_type:
                return self.by_type[name.strip().lower()]
        return []


# T4 publishes an anchor for every content item it renders. This is T4's own
# markup, not a site convention, so it works on any instance.
DIRECT_EDIT_ANCHOR = re.compile(r'<span\s+id=["\']d\.(?P<lang>[a-z]{2})\.(?P<id>\d+)["\']\s*>\s*</span>', re.I)


def describe_item(record, content_types=None, content_layouts=None):
    """What a content item is, and which files produced it."""
    type_name = record.get("contentTypeName")
    found = (content_types.lookup(record.get("contentTypeId"), type_name)
             if content_types else None)
    return {
        "id": str(record.get("id")),
        "name": record.get("name") or "(unnamed)",
        "type": type_name or "(unknown type)",
        "type_file": found[0] if found else None,
        "layouts": content_layouts.for_type(type_name, found[2] if found else None)
                   if content_layouts else [],
    }


def page_content(body, items, content_types=None, content_layouts=None):
    """Content items this page renders, in document order, de-duplicated.

    Reads the anchors out of the page rather than the section's content list:
    a page shows inherited and mirrored content too, so what T4 assigned to
    the section is not the same question as what ended up on the page.
    """
    if not items or not getattr(items, "available", False):
        return []
    found, seen = [], set()
    for match in DIRECT_EDIT_ANCHOR.finditer(body):
        content_id = match.group("id")
        if content_id in seen:
            continue
        seen.add(content_id)
        record = items.get(content_id)
        if record:
            found.append(describe_item(record, content_types, content_layouts))
    return found


def group_by_type(entries):
    """Collapse a page's content items into one row per content type."""
    groups = collections.OrderedDict()
    for entry in entries:
        group = groups.setdefault(entry["type"], {
            "type": entry["type"],
            "type_file": entry["type_file"],
            "layouts": entry["layouts"],
            "items": [],
        })
        group["items"].append((entry["id"], entry["name"]))
    return list(groups.values())


def annotate_content_items(body, items, content_types=None, env_name=None,
                           content_layouts=None):
    """Name each content item the page renders, at its anchor.

    More precise than the marker comments: the anchor is emitted by T4 for
    every item, and carries the item's id rather than only its type.
    """
    if not items or not getattr(items, "available", False):
        return body, 0

    count = 0
    env = _safe_comment(env_name or "<env>")

    def replace(match):
        nonlocal count
        record = items.get(match.group("id"))
        if not record:
            return match.group(0)
        count += 1
        entry = describe_item(record, content_types, content_layouts)
        # One comment node, several lines: the highlighter finds components by
        # walking comment nodes, and splitting this across several would make
        # the element each one introduces ambiguous.
        lines = ["<!-- T4 CONTENT: #%s '%s' of type '%s'"
                 % (_safe_comment(entry["id"]), _safe_comment(entry["name"]),
                    _safe_comment(entry["type"]))]
        if entry["type_file"]:
            lines.append("     type   -> t4-source/%s/%s"
                         % (env, _safe_comment(entry["type_file"])))
        for rel, label in entry["layouts"]:
            lines.append("     layout -> t4-source/%s/%s%s"
                         % (env, _safe_comment(rel),
                            "  (%s)" % _safe_comment(label) if label else ""))
        if not entry["layouts"]:
            lines.append("     layout -> none pulled for this type")
        lines.append("-->")
        return match.group(0) + fence("\n".join(lines))

    return DIRECT_EDIT_ANCHOR.sub(replace, body), count


def annotate_components(body, indexes, patterns=None, env_name=None):
    """Insert a debug comment at each component marker found in the page.

    `indexes` maps an endpoint name to a ContentTypeIndex. Markers are left in
    place and the note added after them, so the output still contains whatever
    the site's own layouts emitted.
    """
    if not indexes:
        return body, 0
    if isinstance(indexes, ContentTypeIndex):          # single index, legacy call
        indexes = {"contenttype": indexes}

    markers = []
    for entry in (patterns or DEFAULT_COMPONENT_MARKERS):
        pattern, endpoint = entry if isinstance(entry, (tuple, list)) else (entry, "contenttype")
        try:
            markers.append((re.compile(pattern, re.I), endpoint))
        except re.error:
            continue

    count = 0

    def note_for(match, endpoint):
        groups = match.groupdict()
        index = indexes.get(endpoint)
        found = index.lookup(groups.get("id"), groups.get("name")) if index else None
        label = (groups.get("name") or groups.get("id") or "?").strip()
        if not found:
            return ("<!-- T4 %s: %s - not in the local pull -->"
                    % (endpoint.upper(), _safe_comment(label)))
        rel, name, _alias = found
        return ("<!-- T4 %s: '%s' -> t4-source/%s/%s -->"
                % ("COMPONENT" if endpoint == "contenttype" else endpoint.upper(),
                   _safe_comment(name or label), _safe_comment(env_name or "<env>"),
                   _safe_comment(rel)))

    for regex, endpoint in markers:
        def replace(match, _endpoint=endpoint):
            nonlocal count
            count += 1
            return match.group(0) + fence(note_for(match, _endpoint))
        body = regex.sub(replace, body)

    return body, count


class LayoutIndex:
    """Maps a published page back to the layout files it came from.

    T4 publishes two meta tags, and they are not equally useful:

      t4-layout-id   exact, but rare -- it appears on a small minority of pages
      t4-layout      present on nearly every page, but it is the name the
                     layout writes into its own markup, which need not be the
                     layout's name in T4. A site may publish `basic_page` from
                     any of basic_page.production, basic_page.webdev and so on.

    So: resolve by id when present, else by name when the name identifies
    exactly one layout, and otherwise report the candidates rather than
    guessing. Guessing here would point someone at the wrong file to edit.
    """

    def __init__(self, manifest=None, source_dir=None):
        self.by_id = {}
        self.by_name = {}
        self.source_dir = source_dir
        # Kept whole as well as indexed: the content layout previewer needs
        # the `layout` entries, which this index filters out.
        self.manifest = dict(manifest or {})
        for rel, meta in (manifest or {}).items():
            if meta.get("endpoint") != "pageLayout":
                continue
            self.by_id.setdefault(str(meta.get("id")), []).append((rel, meta))
            if meta.get("name"):
                self.by_name.setdefault(meta["name"], set()).add(str(meta.get("id")))

    @classmethod
    def from_project_dir(cls, env_dir):
        """Load from t4-source/<env>/_manifest.json, if it has been pulled."""
        if not env_dir:
            return cls()
        path = os.path.join(env_dir, "_manifest.json")
        try:
            with open(path, encoding="utf-8") as fh:
                return cls(json.load(fh), source_dir=env_dir)
        except (OSError, ValueError):
            return cls()

    def files_for(self, layout_id):
        return sorted(self.by_id.get(str(layout_id), []), key=lambda pair: pair[0])

    def resolve(self, layout_name=None, layout_id=None):
        """Return (files, note). `note` explains anything the caller should say."""
        if layout_id and str(layout_id) in self.by_id:
            return self.files_for(layout_id), None
        if layout_id:
            return [], "id #%s is not in the local pull" % layout_id

        if not layout_name:
            return [], None

        ids = self.by_name.get(layout_name)
        if ids and len(ids) == 1:
            return self.files_for(next(iter(ids))), None
        if ids:
            return [], ("name matches %d layouts (#%s) - the page does not say "
                        "which" % (len(ids), ", #".join(sorted(ids))))

        # The common case: the published name is a prefix of several real
        # layout names, e.g. basic_page -> basic_page.production, .webdev, ...
        near = sorted(n for n in self.by_name if n.split(".")[0] == layout_name)
        if len(near) == 1:
            return self.files_for(next(iter(self.by_name[near[0]]))), (
                "matched %s by name" % near[0])
        if near:
            return [], ("no layout is named %r; %d candidates: %s"
                        % (layout_name, len(near), ", ".join(near)))
        return [], "no layout named %r in the local pull" % layout_name


# Everything this tool injects is fenced, so it can be removed exactly.
# Without a fence, stripping means guessing at comment boundaries, and the
# annotations themselves contain "->" and ">" which trip naive patterns.
# Where the inspector asks the server to open a file. Defined here rather
# than in serve.py because the script that calls it is here; serve.py imports
# it, so the two cannot drift.
OPEN_PATH = "/__t4_open"

MARK_BEGIN = "<!--T4SYNC:BEGIN-->"
MARK_END = "<!--T4SYNC:END-->"


def fence(text):
    """Wrap injected markup so `strip_injected` can take it out again."""
    return "%s%s%s" % (MARK_BEGIN, text, MARK_END) if text else ""


def strip_injected(html):
    """Remove everything this tool injected, leaving the page as published."""
    out, cursor = [], 0
    while True:
        start = html.find(MARK_BEGIN, cursor)
        if start < 0:
            out.append(html[cursor:])
            return "".join(out)
        out.append(html[cursor:start])
        end = html.find(MARK_END, start)
        if end < 0:
            return "".join(out)
        cursor = end + len(MARK_END)


def _esc(value):
    return html.escape(str(value), quote=True)


def _json_island(data):
    """JSON safe to drop inside a <script> tag.

    A content type is named by whoever created it in T4, so `<` can appear in
    one. Inside a script element the parser looks for `</script`, and nothing
    else is escaped for it -- so the angle brackets are encoded rather than
    trusted. `\\u003c` is still the same string once parsed.
    """
    return (json.dumps(data)
            .replace("<", "\\u003c")
            .replace(">", "\\u003e")
            .replace("&", "\\u0026"))


def _safe_comment(text):
    """Neutralise only what would close the comment early.

    `--` is illegal in SGML but harmless in HTML5: the comment state ends at
    `-->` or `--!>`, nothing else. Breaking every `--` mangled the one thing
    these notes exist to give you -- a path you can open, and pulled content
    layouts are named `<type>--<layout>.html`.
    """
    return str(text).replace("--!>", "- -!>").replace("-->", "- ->")


def build_comment(body, index, overrides_dir=None, env_name=None,
                  mirror_dir=None, url_path="/", resolver=None, sections=None):
    """Drupal-style theme-debug comments, emitted into the served HTML.

    Mirrors how Twig debug marks up a page: the hook, the candidate templates
    with the chosen one marked, and the file that actually produced the output.
    The candidate list is the useful part here, because T4 publishes a layout
    name that often does not identify a single layout -- `basic_page` can come
    from any of seven `basic_page.*` layouts.

    Injected at serve time only. Files under the mirror are never modified.
    """
    layout = LAYOUT_META.search(body)
    layout_id = LAYOUT_ID_META.search(body)
    layout_name = layout.group(1) if layout else None
    layout_id = layout_id.group(1) if layout_id else None

    lines = ["<!-- T4 DEBUG -->"]
    if not layout_name and not layout_id:
        lines.append("<!-- PAGE LAYOUT: unknown - this page carries no "
                     "t4-layout meta tag -->")
        return "\n".join(lines) + "\n"

    lines.append("<!-- PAGE LAYOUT: '%s'%s -->" % (
        _safe_comment(layout_name or "(unnamed)"),
        " #%s" % _safe_comment(layout_id) if layout_id else ""))

    note = None
    files = []

    # T4's own assignment, when the section tree has been pulled. Exact, so it
    # is preferred over inferring the layout from the page.
    if sections is not None and getattr(sections, "available", False):
        layout_id_exact, section = sections.resolve(url_path)
        if layout_id_exact:
            files = index.files_for(layout_id_exact)
            note = ("assigned in T4 to section %s (%s)"
                    % (section.get("id"), section.get("name")))

    # Only fall back while nothing has been resolved. An earlier version made
    # the fallback an `else` of the resolver check, so an exact answer from the
    # section tree was overwritten by the ambiguous name lookup.
    if not files:
        if resolver is not None:
            record, why = resolver.resolve(body, layout_name, layout_id)
            if record:
                files = index.files_for(record["id"])
                note = "identified as %s (%s)" % (record.get("name"), why)
            else:
                note = why
        else:
            files, note = index.resolve(layout_name, layout_id)

    candidates = ([] if files else
                  sorted(n for n in index.by_name
                         if layout_name and n.split(".")[0] == layout_name))

    if candidates and not files:
        # Same convention as Twig debug: '*' offered, 'x' used.
        lines.append("<!-- LAYOUT NAME SUGGESTIONS:")
        for name in candidates:
            ids = ", #".join(sorted(index.by_name[name]))
            lines.append("   * %s  (#%s)" % (_safe_comment(name), _safe_comment(ids)))
        lines.append("   the published name does not say which - "
                     "check the section's layout in T4")
        lines.append("-->")
    elif note:
        lines.append("<!-- NOTE: %s -->" % _safe_comment(note))

    if files:
        lines.append("<!-- SOURCE FILES:")
        for rel, meta in files:
            overridden = bool(overrides_dir and
                              os.path.isfile(local_path(overrides_dir, rel)))
            lines.append("   x t4-source/%s/%s%s"
                         % (_safe_comment(env_name or "<env>"), _safe_comment(rel),
                            "  (overridden)" if overridden else ""))
        lines.append("-->")
        lines.append("<!-- EDIT: t4 edit is for assets; layouts are edited in "
                     "t4-source/ and deployed with `t4 push` -->")

    assets = []
    for href in dict.fromkeys(ASSET_RE.findall(body)):
        if href.startswith(("http://", "https://", "//")):
            continue
        rel = href.split("?")[0].lstrip("/")
        over = bool(overrides_dir and os.path.isfile(local_path(overrides_dir, rel)))
        assets.append((rel, over))
    if assets:
        lines.append("<!-- ASSETS (%d, %d overridden):"
                     % (len(assets), sum(1 for _r, o in assets if o)))
        for rel, over in assets:
            lines.append("   %s %s" % ("x" if over else "*", _safe_comment(rel)))
        lines.append("   * not overridden. CSS and JS are Media Library items: "
                     "edit t4-source/<env>/media/<name>")
        lines.append("     directly - that file is served here and is what "
                     "t4 push deploys.")
        lines.append("-->")

    children = child_layouts(mirror_dir, url_path)
    if children:
        groups = {}
        for name, child_layout, _cid in children:
            groups.setdefault(child_layout or "(no meta)", []).append(name)
        lines.append("<!-- CHILD PAGES (%d):" % len(children))
        for child_layout, names in sorted(groups.items()):
            lines.append("   %s: %s" % (_safe_comment(child_layout),
                                        _safe_comment(", ".join(names[:8]))
                                        + ("..." if len(names) > 8 else "")))
        lines.append("-->")

    lines.append("<!-- END T4 DEBUG -->")
    return "\n".join(lines) + "\n"


def child_layouts(mirror_dir, url_path, limit=25):
    """Layouts used by the pages directly below this one in the site tree.

    Answers "and what do the children use", which is the question that follows
    once you know the current page's layout -- a section usually shares a
    layout, and the exceptions are what you are looking for.
    """
    if not mirror_dir:
        return []
    rel = url_path.split("?")[0].strip("/")
    base = local_path(mirror_dir, rel) if rel else mirror_dir
    if not os.path.isdir(base):
        return []

    found = []
    try:
        entries = sorted(os.listdir(base))
    except OSError:
        return []

    for name in entries:
        child_dir = os.path.join(base, name)
        if not os.path.isdir(child_dir):
            continue
        index_file = os.path.join(child_dir, "index.html")
        if not os.path.isfile(index_file):
            continue
        try:
            with open(index_file, encoding="utf-8", errors="replace") as fh:
                head = fh.read(8192)
        except OSError:
            continue
        layout = LAYOUT_META.search(head)
        layout_id = LAYOUT_ID_META.search(head)
        found.append((name,
                      layout.group(1) if layout else None,
                      layout_id.group(1) if layout_id else None))
        if len(found) >= limit:
            break
    return found


def build_panel(body, index, overrides_dir=None, env_name=None,
                mirror_dir=None, url_path="/", resolver=None, sections=None,
                content_items=None, content_types=None, content_layouts=None,
                media=None):
    """Return the overlay markup for this page, or '' if there is nothing to say."""
    layout = LAYOUT_META.search(body)
    layout_id = LAYOUT_ID_META.search(body)
    layout_name = layout.group(1) if layout else None
    layout_id = layout_id.group(1) if layout_id else None

    rows = []
    files = []          # also read below, for the click-to-source map
    if layout_name or layout_id:
        label = layout_name or "(unnamed)"
        if layout_id:
            label += '  <span class="t4i-dim">#%s</span>' % _esc(layout_id)
        rows.append(("Page layout", label, None))

        note = None
        # T4's own assignment when the section tree has been pulled: exact,
        # so preferred over inferring the layout from the page.
        if sections is not None and getattr(sections, "available", False):
            exact_id, section = sections.resolve(url_path)
            if exact_id:
                files = index.files_for(exact_id)
                note = "assigned in T4 to section %s" % _esc(section.get("id"))

        # Only fall back while nothing has been resolved. Written as an `else`
        # of the resolver check, the name lookup ran whenever there was no
        # resolver and overwrote the exact answer from the section tree with
        # an ambiguous one -- which is why a page whose layout T4 names
        # precisely still reported "7 candidates" and offered no file to open.
        # The same bug was fixed in build_comment and missed here.
        if not files:
            if resolver is not None:
                record, why = resolver.resolve(body, layout_name, layout_id)
                files = index.files_for(record["id"]) if record else files
                # Plain text: escaped before it reaches the page.
                note = ("%s (%s)" % (record.get("name"), why)) if record else why
            else:
                files, note = index.resolve(layout_name, layout_id)
        for rel, _meta in files:
            # Rendered as a file row: these are source files like any other,
            # and the panel should open them rather than hand you a command.
            rows.append(("", None, None, rel))
        if note:
            rows.append(("", '<span class="t4i-dim">%s</span>' % _esc(note), None))
        if not files and not note:
            rows.append(("", '<span class="t4i-dim">not in the local pull — '
                             'run <code>t4 pull</code></span>', None))
    else:
        rows.append(("Page layout", '<span class="t4i-dim">no t4-layout meta '
                                    'on this page</span>', None))

    assets = []
    by_name = getattr(media, "by_name", None) or {}
    for href in dict.fromkeys(ASSET_RE.findall(body)):
        if href.startswith(("http://", "https://", "//")):
            continue
        rel = href.split("?")[0].lstrip("/")
        overridden = bool(overrides_dir and os.path.isfile(local_path(overrides_dir, rel)))
        # The served URL and the pulled file agree on the filename, not the
        # path: T4 publishes a Media Library item under a channel path that
        # has nothing to do with where the pull put it.
        source_rel = by_name.get(rel.rsplit("/", 1)[-1].lower())
        assets.append((rel, overridden, source_rel))

    groups = group_by_type(page_content(
        body, content_items,
        (content_types or {}).get("contenttype") if isinstance(content_types, dict)
        else content_types,
        content_layouts))
    have_items = bool(content_items and getattr(content_items, "available", False))
    anchor_count = len(set(m.group("id") for m in DIRECT_EDIT_ANCHOR.finditer(body)))

    children = child_layouts(mirror_dir, url_path)
    data = page_data(env_name, layout_name, files, groups, assets, media)
    return _render(rows, assets, children, url_path, groups, have_items,
                   env_name, data, anchor_count)


def page_data(env_name, layout_name, layout_files, groups, assets, media=None):
    """The file-to-output map, for the script to resolve a click against.

    Emitted as one JSON island rather than as attributes scattered over the
    panel: clicking an element has to answer "every local file behind this",
    and reading that back out of rendered markup would mean the answer
    depended on how the panel happened to be laid out.
    """
    media_map = dict(getattr(media, "by_name", None) or {})
    # An asset with no pulled source still names the served path, which is
    # what `t4 edit` takes, so it is worth reporting either way.
    for asset in assets or []:
        media_map.setdefault(asset[0].rsplit("/", 1)[-1].lower(), None)

    return {
        "env": env_name or "<env>",
        "layout": ({"name": layout_name or "(unnamed)",
                    "files": [rel for rel, _meta in layout_files]}
                   if layout_files or layout_name else None),
        "types": {g["type"]: ([rel for rel, _l in g["layouts"]]
                              + ([g["type_file"]] if g["type_file"] else []))
                  for g in groups},
        "media": media_map,
    }


def _hue(name):
    """Must match the hue function in SCRIPT, so swatch and outline agree."""
    value = 0
    for char in str(name):
        value = (value * 31 + ord(char)) % 360
    return value


# The format a web channel publishes. T4 picks a content layout by matching
# the format the page layout's content tag asks for, and that tag is usually
# in a navigation object this tool does not resolve -- so this is a strong
# convention, not a lookup, and the panel says so rather than asserting it.
WEB_FORMAT = "text/html"


def _file_row(parts, rel, env_name, label=None, note=None):
    """One source file, with the ways of getting at it."""
    full = "t4-source/%s/%s" % (env_name or "<env>", rel)
    parts.append('<div class="t4i-row t4i-file">')
    parts.append('<span class="t4i-val"><code>%s</code></span>' % _esc(rel))
    if label:
        parts.append('<span class="t4i-dim t4i-fmt">%s</span>' % _esc(label))
    if note:
        parts.append('<span class="t4i-badge">%s</span>' % _esc(note))
    parts.append('<span class="t4i-tools">')
    parts.append('<button class="t4i-copy" data-open="edit" data-path="%s" '
                 'title="Open in your editor">edit</button>' % _esc(full))
    parts.append('<button class="t4i-copy" data-open="reveal" data-path="%s" '
                 'title="Show in Finder / Explorer">folder</button>' % _esc(full))
    parts.append('<button class="t4i-copy" data-copy="%s" '
                 'title="Copy the path">copy</button>' % _esc(full))
    parts.append('</span></div>')


def _render_content(parts, groups, have_items, env_name, anchors=0):
    if not have_items:
        parts.append('<div class="t4i-row t4i-head">Content</div>')
        parts.append('<div class="t4i-row"><span class="t4i-dim">content items '
                     'are not mapped yet — run <code>t4 sections</code></span></div>')
        return
    if not groups:
        parts.append('<div class="t4i-row t4i-head">Content</div>')
        if anchors:
            # The difference matters: "nothing here" and "I cannot see what is
            # here" look identical in a panel and mean opposite things. A
            # partial section walk leaves most pages in the second state, and
            # reporting it as the first sends you looking for a bug in the
            # page instead of finishing the walk.
            parts.append('<div class="t4i-row"><span class="t4i-dim">'
                         '%d content item(s) on this page, none of them in the '
                         'local map.<br>This page\'s section has not been '
                         'walked yet — run <code>t4 sections</code> to cover '
                         'it.</span></div>' % anchors)
        else:
            parts.append('<div class="t4i-row"><span class="t4i-dim">no content '
                         'item anchors on this page</span></div>')
        return

    total = sum(len(g["items"]) for g in groups)
    parts.append('<div class="t4i-row t4i-head">Content '
                 '<span class="t4i-dim">%d type(s), %d item(s)</span></div>'
                 % (len(groups), total))
    for group in groups:
        parts.append('<div class="t4i-row t4i-type" data-type="%s">'
                     % _esc(group["type"]))
        parts.append('<i class="t4i-dot" style="background:hsl(%d 85%% 62%%)"></i>'
                     % _hue(group["type"]))
        parts.append('<span class="t4i-val"><b>%s</b></span>' % _esc(group["type"]))
        parts.append('<span class="t4i-dim">x%d</span>' % len(group["items"]))
        parts.append('</div>')

        # A content type usually has several layouts, one per output format,
        # and only one of them rendered this page. Listing them as equals
        # leaves you to guess which file to edit -- which is the question the
        # panel exists to answer. So the web format leads, and the rest fold
        # away behind a count.
        layouts = group["layouts"]
        primary = [(rel, fmt) for rel, fmt in layouts
                   if (fmt or "").strip().lower() == WEB_FORMAT]
        others = [pair for pair in layouts if pair not in primary]
        if not primary and layouts:
            # No text/html: nothing to prefer, so show them all as equals
            # rather than promoting an arbitrary one.
            primary, others = layouts, []

        if layouts:
            parts.append('<div class="t4i-row t4i-label">Content layout%s</div>'
                         % (" <span class=\"t4i-dim\">renders this</span>"
                            if len(primary) == 1 and others else ""))
            for rel, fmt in primary:
                _file_row(parts, rel, env_name, fmt,
                          "likely" if others else None)
            if others:
                parts.append('<details class="t4i-more"><summary>'
                             '%d other format%s</summary>'
                             % (len(others), "" if len(others) == 1 else "s"))
                for rel, fmt in others:
                    _file_row(parts, rel, env_name, fmt)
                parts.append('<div class="t4i-row t4i-sub"><span class="t4i-dim">'
                             'T4 picks by the format the page layout asks for; '
                             '%s is the web default.</span></div>' % WEB_FORMAT)
                parts.append('</details>')
        else:
            parts.append('<div class="t4i-row t4i-sub"><span class="t4i-dim">'
                         'no content layout pulled for this type</span></div>')

        if group["type_file"]:
            parts.append('<div class="t4i-row t4i-label">Content type '
                         '<span class="t4i-dim">fields, not markup</span></div>')
            _file_row(parts, group["type_file"], env_name)

        parts.append('<div class="t4i-row t4i-label">On this page '
                     '<span class="t4i-dim">%d</span></div>' % len(group["items"]))
        parts.append('<div class="t4i-row t4i-sub t4i-items">')
        for content_id, name in group["items"]:
            parts.append('<a class="t4i-item" data-item="%s" href="#d.en.%s" '
                         'title="Content item #%s - click to find it on the page">'
                         '%s</a>'
                         % (_esc(content_id), _esc(content_id),
                            _esc(content_id), _esc(name)))
        parts.append('</div>')


def _render(rows, assets, children=(), url_path="/", content_groups=(),
            have_items=False, env_name=None, data=None, anchor_count=0):
    parts = ['<script type="application/json" id="t4i-data">%s</script>'
             % _json_island(data or {}),
             '<div id="t4-inspector" data-collapsed="1">',
             '<button type="button" id="t4i-toggle" title="T4 inspector">T4</button>',
             '<div id="t4i-body">',
             '<div class="t4i-row t4i-controls">',
             '<label class="t4i-switch">Highlight '
             '<select id="t4i-hl">'
             '<option value="off">off</option>'
             '<option value="content">content items</option>'
             '<option value="components">marker components</option>'
             '<option value="all">everything</option>'
             '</select></label>',
             '<label class="t4i-switch"><input type="checkbox" id="t4i-labels" '
             'checked> labels</label>',
             '</div>',
             '<div class="t4i-row t4i-controls">',
             '<label class="t4i-switch"><input type="checkbox" id="t4i-pick"> '
             'Select on click <span class="t4i-dim">(links off)</span></label>',
             '</div>',
             '<div id="t4i-legend" class="t4i-legend"></div>',
             '<div id="t4i-picked"></div>']

    for row in rows:
        label, value, copy_target = row[0], row[1], row[2]
        file_rel = row[3] if len(row) > 3 else None
        if file_rel:
            _file_row(parts, file_rel, env_name)
            continue
        parts.append('<div class="t4i-row">')
        parts.append('<span class="t4i-key">%s</span>' % _esc(label))
        parts.append('<span class="t4i-val">%s</span>' % value)
        if copy_target:
            parts.append('<button class="t4i-copy" data-copy="%s">copy</button>'
                         % _esc(copy_target))
        parts.append('</div>')

    # Everything on the page, which is the right answer until you have
    # picked something. Once you have, the script hides this and shows only
    # the chain behind what you clicked -- the way an element inspector
    # works, and the reason you clicked in the first place.
    parts.append('<div id="t4i-all">')
    _render_content(parts, content_groups, have_items, env_name,
                    anchor_count)

    if assets:
        overridden = sum(1 for asset in assets if asset[1])
        pulled = sum(1 for asset in assets if len(asset) > 2 and asset[2])
        parts.append('<div class="t4i-row t4i-head">Assets '
                     '<span class="t4i-dim">%d, %d pulled, %d overridden'
                     '</span></div>' % (len(assets), pulled, overridden))
        for asset in assets:
            rel, over = asset[0], asset[1]
            source_rel = asset[2] if len(asset) > 2 else None
            parts.append('<div class="t4i-row t4i-asset%s">'
                         % (" t4i-on" if over else ""))
            parts.append('<span class="t4i-val"><code>%s</code></span>' % _esc(rel))
            if over:
                parts.append('<span class="t4i-badge">overridden</span>')
            parts.append('</div>')
            if source_rel:
                # A Media Library item: the pulled file is what is being
                # served and what deploys, so open that rather than the URL.
                _file_row(parts, source_rel, env_name)
            else:
                parts.append('<div class="t4i-row t4i-sub"><span class="t4i-dim">'
                             'not pulled — served from the mirror. '
                             '<code>t4 edit %s</code> to experiment locally.'
                             '</span></div>' % _esc(rel))

    if children:
        same = {}
        for _name, layout, _lid in children:
            same[layout] = same.get(layout, 0) + 1
        summary = "all %s" % next(iter(same)) if len(same) == 1 else "%d layouts" % len(same)
        parts.append('<div class="t4i-row t4i-head">Children '
                     '<span class="t4i-dim">%d, %s</span></div>'
                     % (len(children), _esc(summary)))
        for name, layout, layout_id in children:
            label = layout or "(no meta)"
            if layout_id:
                label += " #%s" % layout_id
            parts.append('<div class="t4i-row t4i-child">')
            parts.append('<span class="t4i-val"><a href="%s">%s</a></span>'
                         % (_esc(url_path.rstrip("/") + "/" + name + "/"), _esc(name)))
            parts.append('<span class="t4i-dim">%s</span>' % _esc(label))
            parts.append('</div>')

    parts.append('</div>')          # #t4i-all
    parts.append('</div></div>')
    return STYLE + "".join(parts) + SCRIPT.replace("__OPEN_PATH__", OPEN_PATH)


STYLE = """
<style id="t4i-style">
#t4-inspector{position:fixed;right:12px;bottom:12px;z-index:2147483647;
 font:12px/1.5 ui-monospace,SFMono-Regular,Menlo,monospace;color:#e8eaf0;
 background:#161922;border:1px solid #2c3240;border-radius:8px;
 box-shadow:0 8px 28px rgba(0,0,0,.45);max-width:min(560px,92vw);}
#t4-inspector[data-collapsed="1"] #t4i-body{display:none}
#t4i-toggle{all:unset;display:block;cursor:pointer;padding:6px 10px;
 font:600 11px/1 ui-monospace,monospace;letter-spacing:.08em;color:#9fb4ff}
#t4i-body{padding:4px 10px 10px;max-height:60vh;overflow:auto}
.t4i-row{display:flex;gap:8px;align-items:baseline;padding:3px 0;
 border-top:1px solid #222733}
.t4i-row:first-child{border-top:0}
.t4i-head{color:#9fb4ff;font-weight:600;margin-top:6px}
.t4i-key{flex:0 0 84px;color:#8b93a7}
.t4i-val{flex:1 1 auto;word-break:break-all}
.t4i-dim{color:#8b93a7}
#t4-inspector code{background:#0f1218;padding:1px 4px;border-radius:3px}
.t4i-asset.t4i-on code{background:#17331f;color:#8ff0a6}
.t4i-copy{all:unset;cursor:pointer;flex:0 0 auto;color:#9fb4ff;
 border:1px solid #2c3240;border-radius:4px;padding:1px 6px;font-size:11px}
.t4i-copy:hover{background:#222a3a}
.t4i-child a{color:#9fb4ff;text-decoration:none}
.t4i-child a:hover{text-decoration:underline}
.t4i-controls{gap:12px;flex-wrap:wrap}
.t4i-switch{cursor:pointer;user-select:none;display:flex;gap:6px;align-items:center}
#t4i-hl{font:inherit;background:#0f1218;color:inherit;border:1px solid #2c3240;
 border-radius:4px;padding:1px 4px}
.t4i-type{align-items:center}
.t4i-dot{flex:0 0 auto;width:10px;height:10px;border-radius:2px;display:inline-block}
.t4i-sub{padding-left:18px;border-top:0}

/* A group heading inside a content type: which of these files is what. */
.t4i-label{padding-left:18px;border-top:0;color:#8b93a7;font-size:11px;
 text-transform:uppercase;letter-spacing:.05em;margin-top:4px}
.t4i-label .t4i-dim{text-transform:none;letter-spacing:0;font-size:11px}

.t4i-file{padding-left:18px;border-top:0;gap:6px;align-items:center}
.t4i-fmt{flex:0 0 auto;font-size:11px}
.t4i-badge{flex:0 0 auto;font-size:10px;padding:0 5px;border-radius:999px;
 background:#1d2a1f;color:#8ff0a6;border:1px solid #2a4030}
.t4i-tools{flex:0 0 auto;display:flex;gap:3px}
.t4i-tools .t4i-copy{padding:1px 5px}

/* Folded-away formats that did not render this page. */
.t4i-more{padding-left:18px;font-size:11px}
.t4i-more summary{cursor:pointer;color:#8b93a7;padding:2px 0}
.t4i-more summary:hover{color:#9fb4ff}
.t4i-more .t4i-file{padding-left:8px}

/* Chips, not a run of underlined words: these are separate items. */
.t4i-items{flex-wrap:wrap;gap:4px}
a.t4i-item{color:#9fb4ff;text-decoration:none;border:1px solid #2c3240;
 border-radius:4px;padding:1px 6px;font-size:11px;background:#0f1218;
 max-width:100%;overflow:hidden;text-overflow:ellipsis;white-space:nowrap}
a.t4i-item:hover{color:#fff;border-color:#9fb4ff}
/* Flashed when a panel item is clicked, so the row and the page agree. */
html .t4c-flash{outline:3px solid #ffd34d!important;outline-offset:-3px}
.t4i-flash{background:rgba(255,211,77,.22);border-radius:3px}

/* Pick mode: click an element on the page to select it. */
html.t4c-pick,html.t4c-pick *{cursor:crosshair!important}
html.t4c-pick #t4-inspector,html.t4c-pick #t4-inspector *{cursor:auto!important}
html.t4c-pick [data-t4c-hover]{outline:2px solid #6ea8ff;outline-offset:-2px}
html [data-t4c-sel]{outline:3px solid #ffd34d!important;outline-offset:-3px;
 position:relative}
html [data-t4c-sel]::after{content:attr(data-t4c-sel);position:absolute;
 top:0;right:0;z-index:2147483646;background:#ffd34d;color:#0b0d12;
 font:600 10px/1.4 ui-monospace,SFMono-Regular,Menlo,monospace;
 padding:1px 5px;border-radius:0 0 0 3px;pointer-events:none;white-space:nowrap}
/* With something selected the panel shows that and nothing else, the way an
   element inspector does. The full listing is still there, one click away. */
#t4-inspector[data-focus="1"] #t4i-all{display:none}
#t4i-picked:empty{display:none}
.t4i-back{all:unset;cursor:pointer;color:#9fb4ff;font-size:11px;
 border:1px solid #2c3240;border-radius:4px;padding:1px 6px}
.t4i-back:hover{background:#222a3a}
.t4i-hint{color:#8b93a7;font-size:11px;padding:2px 0 4px}
.t4i-legend{display:none;flex-wrap:wrap;gap:4px 10px;padding:4px 0 2px}
.t4i-legend.on{display:flex}
.t4i-legend span{display:flex;align-items:center;gap:5px;font-size:11px}
.t4i-legend i{width:10px;height:10px;border-radius:2px;display:inline-block}

/* Applied to the page being inspected, not to the panel. */
html.t4c-on [data-t4c-hl]{outline:2px dashed var(--t4c);outline-offset:-2px;
 position:relative}
html.t4c-on.t4c-labels [data-t4c-hl]::before{content:attr(data-t4c-hl);position:absolute;
 top:0;left:0;z-index:2147483646;background:var(--t4c);color:#0b0d12;
 font:600 10px/1.4 ui-monospace,SFMono-Regular,Menlo,monospace;
 padding:1px 5px;border-radius:0 0 3px 0;pointer-events:none;white-space:nowrap}
@media (prefers-color-scheme:light){
 #t4-inspector{background:#fff;color:#1a1d25;border-color:#d4d8e0}
 #t4-inspector code{background:#f1f3f7}
 .t4i-key,.t4i-dim{color:#5a6376}
 .t4i-asset.t4i-on code{background:#e6f7ec;color:#17603a}
 #t4i-hl{background:#fff;border-color:#d4d8e0}
 a.t4i-item{color:#2b4bbf;background:#fff;border-color:#d4d8e0}
 a.t4i-item:hover{color:#12205c;border-color:#2b4bbf}
 .t4i-badge{background:#e6f7ec;color:#17603a;border-color:#bfe3cd}
}
</style>
"""

SCRIPT = """
<script id="t4i-script">
(function () {
  var root = document.getElementById("t4-inspector");
  if (!root) return;

  // Component highlighting.
  //
  // The component markers are already comment nodes in the DOM, so the element
  // each one introduces can be found without injecting any markup. Injecting
  // elements would risk landing somewhere invalid -- inside <head>, a <table>
  // or a <select> -- and changing how the page renders, which defeats the
  // purpose of looking at it.
  var NOTE = /T4 (COMPONENT|NAVIGATION): '([^']*)'/;
  // Emitted at every Direct Edit anchor, so this is the reliable one: T4
  // writes the anchor itself, while the COMPONENT markers depend on the site
  // having commented its content layouts.
  var CONTENT = /T4 CONTENT: #(\\d+) '([^']*)' of type '([^']*)'/;
  var found = null;

  function hue(name) {
    var h = 0;
    for (var i = 0; i < name.length; i++) { h = (h * 31 + name.charCodeAt(i)) % 360; }
    return h;
  }

  function collect() {
    if (found) return found;
    found = [];
    var walker = document.createTreeWalker(document.body || document.documentElement,
                                           NodeFilter.SHOW_COMMENT, null, false);
    var node;
    while ((node = walker.nextNode())) {
      var text = node.nodeValue || "";
      var entry = null;
      var m = CONTENT.exec(text);
      if (m) {
        // Coloured and legended by content type, labelled with the item name:
        // the type is what you go and edit, the name is what you see.
        entry = { kind: "content", group: m[3], label: m[3] + " \\u00b7 " + m[2],
                  id: m[1] };
      } else if ((m = NOTE.exec(text))) {
        entry = { kind: "component", group: m[2], label: m[2] };
      }
      if (!entry) continue;
      var el = node.nextElementSibling;
      // The marker may sit just before its element, or be the last child of a
      // wrapper; fall back to the parent so something is always highlighted.
      if (!el && node.parentElement) el = node.parentElement;
      if (!el || el.closest("#t4-inspector")) continue;
      entry.el = el;
      found.push(entry);
    }

    // Content layouts often open with an empty anchor span, which has no box
    // to outline. Climb to the nearest ancestor that actually occupies space,
    // without escaping into a shared wrapper that holds other components.
    var claimed = new Set(found.map(function (f) { return f.el; }));
    found.forEach(function (item) {
      var el = item.el, hops = 0;
      while (el && hops < 3 && el.getBoundingClientRect().height === 0) {
        var parent = el.parentElement;
        if (!parent || parent === document.body || claimed.has(parent)) break;
        el = parent;
        hops++;
      }
      item.el = el;
    });
    return found;
  }

  function wanted(mode, kind) {
    return mode === "all" || mode === kind ||
           (mode === "components" && kind === "component") ||
           (mode === "content" && kind === "content");
  }

  function apply(mode) {
    var items = collect();
    var seen = {};
    items.forEach(function (item) {
      var on = wanted(mode, item.kind);
      if (on) {
        var h = hue(item.group);
        // Two components can resolve to the same element -- a wrapper holding
        // both, or an empty anchor that climbed to a shared parent. Combine
        // the labels rather than letting the last one win, so the page and
        // the legend agree about what is on it.
        var existing = item.el.getAttribute("data-t4c-hl");
        if (existing && existing.split(" + ").indexOf(item.label) === -1) {
          item.el.setAttribute("data-t4c-hl", existing + " + " + item.label);
        } else if (!existing) {
          item.el.style.setProperty("--t4c", "hsl(" + h + " 85% 62%)");
          item.el.setAttribute("data-t4c-hl", item.label);
        }
        if (item.id) item.el.setAttribute("data-t4c-id", item.id);
        seen[item.group] = h;
      } else {
        item.el.removeAttribute("data-t4c-hl");
        item.el.style.removeProperty("--t4c");
      }
    });
    document.documentElement.classList.toggle("t4c-on", mode !== "off");

    var legend = document.getElementById("t4i-legend");
    if (!legend) return;
    legend.classList.toggle("on", mode !== "off");
    if (mode === "off") { legend.innerHTML = ""; return; }
    var names = Object.keys(seen).sort();
    legend.innerHTML = names.map(function (n) {
      return '<span><i style="background:hsl(' + seen[n] + ' 85% 62%)"></i>' +
             n.replace(/[&<>]/g, "") + "</span>";
    }).join("") || '<span class="t4i-dim">nothing of that kind is marked on ' +
                   'this page</span>';
  }

  var HLKEY = "t4-inspector-highlight";
  var select = document.getElementById("t4i-hl");
  if (select) {
    var stored = null;
    try { stored = localStorage.getItem(HLKEY); } catch (e) {}
    // "1"/"0" is what the earlier checkbox stored; treat it as the old
    // meaning rather than resetting everyone's setting to off.
    if (stored === "1") stored = "components";
    else if (stored === "0") stored = "off";
    if (stored) select.value = stored;
    if (select.value !== "off") apply(select.value);
    select.addEventListener("change", function () {
      apply(select.value);
      try { localStorage.setItem(HLKEY, select.value); } catch (e) {}
    });
  }

  var LABELKEY = "t4-inspector-labels";
  var labels = document.getElementById("t4i-labels");
  if (labels) {
    try { if (localStorage.getItem(LABELKEY) === "0") labels.checked = false; }
    catch (e) {}
    document.documentElement.classList.toggle("t4c-labels", labels.checked);
    labels.addEventListener("change", function () {
      document.documentElement.classList.toggle("t4c-labels", labels.checked);
      try { localStorage.setItem(LABELKEY, labels.checked ? "1" : "0"); } catch (e) {}
    });
  }

  // Clicking a content item in the panel takes you to it on the page. The
  // anchor T4 published is always there, highlighting on or off.
  function flashOnPage(elements, scroll) {
    if (!elements.length) return;
    if (scroll) elements[0].scrollIntoView({behavior: "smooth", block: "center"});
    elements.forEach(function (el) {
      el.classList.add("t4c-flash");
      setTimeout(function () { el.classList.remove("t4c-flash"); }, 1800);
    });
  }

  // Panel -> page. The same question as pick mode, asked the other way: you
  // are reading the file list and want to see where on the page it landed.
  root.addEventListener("click", function (e) {
    if (e.target.closest(".t4i-copy")) return;        // handled below

    if (e.target.closest("#t4i-back")) { clearPick(); return; }

    var link = e.target.closest("a.t4i-item");
    if (link) {
      e.preventDefault();
      var anchor = document.getElementById("d.en." + link.getAttribute("data-item"));
      if (anchor) flashOnPage([anchor.nextElementSibling || anchor], true);
      return;
    }

    // A content type: show every item of that type at once.
    var typeRow = e.target.closest(".t4i-type[data-type]");
    if (typeRow) {
      var want = typeRow.getAttribute("data-type");
      var found = collect().filter(function (item) { return item.group === want; });
      flashOnPage(found.map(function (item) { return item.el; }), true);
      return;
    }

    // An asset: every element that pulls that file in.
    var assetRow = e.target.closest(".t4i-asset");
    if (assetRow) {
      var code = assetRow.querySelector("code");
      if (!code) return;
      var base = code.textContent.split("/").pop().toLowerCase();
      var using = [].slice.call(document.querySelectorAll("[src],[href]"))
        .filter(function (el) {
          if (el.closest("#t4-inspector")) return false;
          var url = el.getAttribute("src") || el.getAttribute("href") || "";
          return url.split("?")[0].split("/").pop().toLowerCase() === base;
        });
      // A stylesheet has no box to flash, so say how many referenced it
      // rather than appearing to do nothing.
      flashOnPage(using.filter(function (el) {
        return el.getBoundingClientRect().height > 0;
      }), true);
      assetRow.classList.add("t4i-flash");
      setTimeout(function () { assetRow.classList.remove("t4i-flash"); }, 1200);
    }
  });

  // -- pick mode: click an element on the page to select its component ----
  //
  // The other direction of the same question. Highlighting shows you every
  // component at once, which is the wrong tool when you are looking at one
  // thing on screen and want to know what produced it.

  var picked = null;

  var DATA = (function () {
    var tag = document.getElementById("t4i-data");
    try { return JSON.parse(tag ? tag.textContent : "{}"); }
    catch (e) { return {}; }
  })();

  function componentAt(node) {
    // Nearest enclosing element that collect() recognised. Walking up rather
    // than matching exactly: a click lands on a heading or a link inside a
    // component far more often than on the component's own box.
    var items = collect();
    for (var el = node; el && el !== document.documentElement; el = el.parentElement) {
      for (var i = 0; i < items.length; i++) {
        if (items[i].el === el) return items[i];
      }
    }
    return null;
  }

  function assetAt(node) {
    // An element that pulls in a file of its own -- an image, a stylesheet,
    // a script. These have local sources too, and are the obvious thing to
    // click on when what you want to change is the picture or the CSS.
    var el = node.closest && node.closest("[src],[href],[data-src]");
    if (!el) return null;
    var url = el.getAttribute("src") || el.getAttribute("href") ||
              el.getAttribute("data-src") || "";
    if (!url || url.charAt(0) === "#") return null;
    var base = url.split("?")[0].split("#")[0].split("/").pop().toLowerCase();
    if (!base || !(DATA.media || {}).hasOwnProperty(base)) return null;
    var rel = DATA.media[base];
    return {
      kind: "asset", el: el, label: base,
      files: rel ? [rel] : [],
      note: rel ? null : "served from the mirror; not a pulled Media Library item"
    };
  }

  // Every local file behind one element, innermost first. The page layout is
  // always the last entry: whatever you clicked, it is inside one.
  function chainAt(node) {
    var chain = [];
    var asset = assetAt(node);
    if (asset) chain.push(asset);

    var component = componentAt(node);
    if (component) {
      chain.push({
        kind: component.kind, el: component.el, id: component.id,
        label: component.label,
        files: (DATA.types || {})[component.group] || [],
        note: ((DATA.types || {})[component.group] || []).length ? null
              : "no content layout pulled for this type"
      });
    }

    if (DATA.layout) {
      chain.push({kind: "layout", el: null,
                  label: "page layout " + DATA.layout.name,
                  files: DATA.layout.files,
                  note: DATA.layout.files.length ? null
                        : "not in the local pull - run t4 pull"});
    }
    return chain;
  }

  function flashPanel(el) {
    if (!el) return;
    if (root.dataset.collapsed === "1") root.dataset.collapsed = "0";
    el.scrollIntoView({ block: "nearest" });
    el.classList.add("t4i-flash");
    setTimeout(function () { el.classList.remove("t4i-flash"); }, 1800);
  }

  function clearPick() {
    if (picked) picked.removeAttribute("data-t4c-sel");
    picked = null;
    var box = document.getElementById("t4i-picked");
    if (box) box.innerHTML = "";
    root.dataset.focus = "0";          // back to everything on the page
  }

  function fileRow(rel) {
    var full = "t4-source/" + (DATA.env || "<env>") + "/" + rel;
    var row = document.createElement("div");
    row.className = "t4i-row t4i-file";
    var val = document.createElement("span");
    val.className = "t4i-val";
    var code = document.createElement("code");
    code.textContent = rel;
    val.appendChild(code);
    row.appendChild(val);

    var tools = document.createElement("span");
    tools.className = "t4i-tools";
    [["edit", "data-open", "edit"], ["folder", "data-open", "reveal"],
     ["copy", "data-copy", full]].forEach(function (spec) {
      var b = document.createElement("button");
      b.className = "t4i-copy";
      b.textContent = spec[0];
      b.setAttribute(spec[1], spec[2]);
      if (spec[1] === "data-open") b.setAttribute("data-path", full);
      tools.appendChild(b);
    });
    row.appendChild(tools);
    return row;
  }

  function select(chain, clicked) {
    clearPick();
    if (!chain.length) return;

    // Outline the innermost thing that has a box of its own. The page layout
    // has none -- it is the page -- so a click that resolves only to it
    // marks what was actually clicked, which is still the honest answer.
    var frame = null;
    for (var i = 0; i < chain.length; i++) { if (chain[i].el) { frame = chain[i]; break; } }
    picked = frame ? frame.el : clicked;
    if (picked) picked.setAttribute("data-t4c-sel", (frame || chain[0]).label);

    var box = document.getElementById("t4i-picked");
    if (!box) return;
    box.innerHTML = "";

    // Only the selection, until you ask for the rest back.
    root.dataset.focus = "1";

    var head = document.createElement("div");
    head.className = "t4i-row t4i-head";
    head.appendChild(document.createTextNode("Selected "));
    var back = document.createElement("button");
    back.className = "t4i-back";
    back.id = "t4i-back";
    back.textContent = "show whole page";
    head.appendChild(back);
    box.appendChild(head);

    chain.forEach(function (entry) {
      var row = document.createElement("div");
      row.className = "t4i-row t4i-type";
      var dot = document.createElement("i");
      dot.className = "t4i-dot";
      dot.style.background = entry.kind === "layout" ? "#8b93a7"
        : "hsl(" + hue(entry.label) + " 85% 62%)";
      var val = document.createElement("span");
      val.className = "t4i-val";
      val.textContent = entry.label;
      var kind = document.createElement("span");
      kind.className = "t4i-dim";
      kind.textContent = entry.kind;
      row.appendChild(dot); row.appendChild(val); row.appendChild(kind);
      box.appendChild(row);

      entry.files.forEach(function (rel) { box.appendChild(fileRow(rel)); });
      if (entry.note) {
        var note = document.createElement("div");
        note.className = "t4i-row t4i-sub";
        var dim = document.createElement("span");
        dim.className = "t4i-dim";
        dim.textContent = entry.note;
        note.appendChild(dim);
        box.appendChild(note);
      }
    });

    var hint = document.createElement("div");
    hint.className = "t4i-hint";
    hint.textContent = "Innermost first. Click another element to move the "
                     + "selection, or Escape to clear it.";
    box.appendChild(hint);

    if (root.dataset.collapsed === "1") root.dataset.collapsed = "0";
    box.scrollIntoView({block: "nearest"});

    // And point at the matching row further down the panel, so the two
    // halves of the answer line up.
    var component = null;
    for (var j = 0; j < chain.length; j++) {
      if (chain[j].kind === "content" || chain[j].kind === "component") {
        component = chain[j]; break;
      }
    }
    if (component && component.id) {
      flashPanel(root.querySelector('a.t4i-item[data-item="' + component.id + '"]'));
    }
  }

  function pickOn() {
    var box = document.getElementById("t4i-pick");
    return !!(box && box.checked);
  }

  // Capture phase, so a link's own handler never runs: the point of the mode
  // is that clicking a component tells you about it instead of navigating.
  document.addEventListener("click", function (e) {
    if (!pickOn()) return;
    if (e.target.closest && e.target.closest("#t4-inspector")) return;
    e.preventDefault();
    e.stopPropagation();
    select(chainAt(e.target), e.target);
  }, true);

  // A link reached by keyboard would otherwise still navigate.
  document.addEventListener("keydown", function (e) {
    if (e.key === "Escape") { clearPick(); return; }
    if (!pickOn() || e.key !== "Enter") return;
    if (e.target.closest && e.target.closest("#t4-inspector")) return;
    if (e.target.closest && e.target.closest("a[href]")) e.preventDefault();
  }, true);

  document.addEventListener("mouseover", function (e) {
    if (!pickOn()) return;
    if (e.target.closest && e.target.closest("#t4-inspector")) return;
    var previous = document.querySelector("[data-t4c-hover]");
    if (previous) previous.removeAttribute("data-t4c-hover");
    var chain = chainAt(e.target);
    for (var i = 0; i < chain.length; i++) {
      if (chain[i].el) { chain[i].el.setAttribute("data-t4c-hover", ""); return; }
    }
  }, true);

  var PICKKEY = "t4-inspector-pick";
  var pick = document.getElementById("t4i-pick");
  if (pick) {
    try { if (localStorage.getItem(PICKKEY) === "1") pick.checked = true; }
    catch (e) {}
    var sync = function () {
      document.documentElement.classList.toggle("t4c-pick", pick.checked);
      if (!pick.checked) {
        clearPick();
        var hover = document.querySelector("[data-t4c-hover]");
        if (hover) hover.removeAttribute("data-t4c-hover");
      }
      try { localStorage.setItem(PICKKEY, pick.checked ? "1" : "0"); } catch (e) {}
    };
    pick.addEventListener("change", sync);
    sync();
  }
  var KEY = "t4-inspector-open";
  try { if (localStorage.getItem(KEY) === "1") root.dataset.collapsed = "0"; } catch (e) {}
  root.querySelector("#t4i-toggle").addEventListener("click", function () {
    var open = root.dataset.collapsed === "1";
    root.dataset.collapsed = open ? "0" : "1";
    try { localStorage.setItem(KEY, open ? "1" : "0"); } catch (e) {}
  });
  function flashButton(btn, word) {
    var old = btn.textContent;
    btn.textContent = word;
    setTimeout(function () { btn.textContent = old; }, 1400);
  }

  root.addEventListener("click", function (e) {
    var btn = e.target.closest(".t4i-copy");
    if (!btn) return;

    // Open or reveal: the browser cannot launch an application, so the
    // server that served this page does it.
    var how = btn.getAttribute("data-open");
    if (how) {
      fetch("__OPEN_PATH__", {
        method: "POST",
        headers: {"Content-Type": "application/json"},
        body: JSON.stringify({path: btn.getAttribute("data-path"), action: how})
      }).then(function (r) { return r.json().then(function (d) {
        flashButton(btn, r.ok ? "opened" : "failed");
        if (!r.ok && d.error) console.warn("t4 inspector:", d.error);
      }); }).catch(function () { flashButton(btn, "failed"); });
      return;
    }

    var text = btn.getAttribute("data-copy");
    if (text === null) return;
    var done = function () { flashButton(btn, "copied"); };
    if (navigator.clipboard) { navigator.clipboard.writeText(text).then(done, done); }
    else { done(); }
  });
})();
</script>
"""
