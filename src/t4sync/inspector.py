"""A small overlay showing which T4 layout rendered a page, and what to edit.

T4 publishes `<meta name="t4-layout">` and `<meta name="t4-layout-id">` into
every page it renders. The id maps directly onto the pull manifest, so a page
being viewed locally can be traced back to the exact file on disk that
produced it -- which is otherwise guesswork across 58 layouts with names like
`u_webpage.production` and `m_open_alt_no_menu`.

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
InspectorContext = collections.namedtuple(
    "InspectorContext",
    "index overrides_dir env_name mirror_dir url_path mode "
    "content_types component_markers resolver sections content_items "
    "content_layouts")
InspectorContext.__new__.__defaults__ = (None, None, None, "/", "both",
                                         None, None, None, None, None, None)

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
                     layout's name in T4. A site may publish `u_webpage` from
                     any of u_webpage.production, u_webpage.webdev and so on.

    So: resolve by id when present, else by name when the name identifies
    exactly one layout, and otherwise report the candidates rather than
    guessing. Guessing here would point someone at the wrong file to edit.
    """

    def __init__(self, manifest=None, source_dir=None):
        self.by_id = {}
        self.by_name = {}
        self.source_dir = source_dir
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
        # layout names, e.g. u_webpage -> u_webpage.production, .webdev, ...
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
    name that often does not identify a single layout -- `u_webpage` can come
    from any of seven `u_webpage.*` layouts.

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
        lines.append("   * not overridden - copy it in with: t4 edit <path>")
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
                content_items=None, content_types=None, content_layouts=None):
    """Return the overlay markup for this page, or '' if there is nothing to say."""
    layout = LAYOUT_META.search(body)
    layout_id = LAYOUT_ID_META.search(body)
    layout_name = layout.group(1) if layout else None
    layout_id = layout_id.group(1) if layout_id else None

    rows = []
    if layout_name or layout_id:
        label = layout_name or "(unnamed)"
        if layout_id:
            label += '  <span class="t4i-dim">#%s</span>' % _esc(layout_id)
        rows.append(("Page layout", label, None))

        files, note = [], None
        # T4's own assignment when the section tree has been pulled: exact,
        # so preferred over inferring the layout from the page.
        if sections is not None and getattr(sections, "available", False):
            exact_id, section = sections.resolve(url_path)
            if exact_id:
                files = index.files_for(exact_id)
                note = "assigned in T4 to section %s" % _esc(section.get("id"))

        if not files and resolver is not None:
            record, why = resolver.resolve(body, layout_name, layout_id)
            files = index.files_for(record["id"]) if record else files
            # Plain text: the caller escapes it before it reaches the page.
            note = ("%s (%s)" % (record.get("name"), why)) if record else why
        else:
            files, note = index.resolve(layout_name, layout_id)
        for rel, _meta in files:
            rows.append(("", '<code>%s</code>' % _esc(rel),
                         "t4-source/%s/%s" % (env_name or "<env>", rel)))
        if note:
            rows.append(("", '<span class="t4i-dim">%s</span>' % _esc(note), None))
        if not files and not note:
            rows.append(("", '<span class="t4i-dim">not in the local pull — '
                             'run <code>t4 pull</code></span>', None))
    else:
        rows.append(("Page layout", '<span class="t4i-dim">no t4-layout meta '
                                    'on this page</span>', None))

    assets = []
    for href in dict.fromkeys(ASSET_RE.findall(body)):
        if href.startswith(("http://", "https://", "//")):
            continue
        rel = href.split("?")[0].lstrip("/")
        overridden = bool(overrides_dir and os.path.isfile(local_path(overrides_dir, rel)))
        assets.append((rel, overridden))

    groups = group_by_type(page_content(
        body, content_items,
        (content_types or {}).get("contenttype") if isinstance(content_types, dict)
        else content_types,
        content_layouts))
    have_items = bool(content_items and getattr(content_items, "available", False))

    children = child_layouts(mirror_dir, url_path)
    return _render(rows, assets, children, url_path, groups, have_items, env_name)


def _hue(name):
    """Must match the hue function in SCRIPT, so swatch and outline agree."""
    value = 0
    for char in str(name):
        value = (value * 31 + ord(char)) % 360
    return value


def _render_content(parts, groups, have_items, env_name):
    if not have_items:
        parts.append('<div class="t4i-row t4i-head">Content</div>')
        parts.append('<div class="t4i-row"><span class="t4i-dim">content items '
                     'are not mapped yet — run <code>t4 sections</code></span></div>')
        return
    if not groups:
        parts.append('<div class="t4i-row t4i-head">Content</div>')
        parts.append('<div class="t4i-row"><span class="t4i-dim">no content item '
                     'anchors on this page</span></div>')
        return

    total = sum(len(g["items"]) for g in groups)
    parts.append('<div class="t4i-row t4i-head">Content '
                 '<span class="t4i-dim">%d type(s), %d item(s)</span></div>'
                 % (len(groups), total))
    for group in groups:
        parts.append('<div class="t4i-row t4i-type">')
        parts.append('<i class="t4i-dot" style="background:hsl(%d 85%% 62%%)"></i>'
                     % _hue(group["type"]))
        parts.append('<span class="t4i-val"><b>%s</b></span>' % _esc(group["type"]))
        parts.append('<span class="t4i-dim">x%d</span>' % len(group["items"]))
        parts.append('</div>')

        for rel, label in group["layouts"]:
            parts.append('<div class="t4i-row t4i-sub">')
            parts.append('<span class="t4i-val"><code>%s</code>%s</span>'
                         % (_esc(rel),
                            ' <span class="t4i-dim">%s</span>' % _esc(label) if label else ""))
            parts.append('<button class="t4i-copy" data-copy="t4-source/%s/%s">path</button>'
                         % (_esc(env_name or "<env>"), _esc(rel)))
            parts.append('</div>')
        if not group["layouts"]:
            parts.append('<div class="t4i-row t4i-sub"><span class="t4i-dim">'
                         'no content layout pulled for this type</span></div>')
        if group["type_file"]:
            parts.append('<div class="t4i-row t4i-sub">')
            parts.append('<span class="t4i-val"><code>%s</code></span>'
                         % _esc(group["type_file"]))
            parts.append('<button class="t4i-copy" data-copy="t4-source/%s/%s">path</button>'
                         % (_esc(env_name or "<env>"), _esc(group["type_file"])))
            parts.append('</div>')

        parts.append('<div class="t4i-row t4i-sub t4i-items">')
        for content_id, name in group["items"]:
            parts.append('<a class="t4i-item" data-item="%s" href="#d.en.%s" '
                         'title="#%s">%s</a>'
                         % (_esc(content_id), _esc(content_id),
                            _esc(content_id), _esc(name)))
        parts.append('</div>')


def _render(rows, assets, children=(), url_path="/", content_groups=(),
            have_items=False, env_name=None):
    parts = ['<div id="t4-inspector" data-collapsed="1">',
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
             '<div id="t4i-legend" class="t4i-legend"></div>']

    for label, value, copy_target in rows:
        parts.append('<div class="t4i-row">')
        parts.append('<span class="t4i-key">%s</span>' % _esc(label))
        parts.append('<span class="t4i-val">%s</span>' % value)
        if copy_target:
            parts.append('<button class="t4i-copy" data-copy="t4 edit %s">edit</button>'
                         % _esc(copy_target))
        parts.append('</div>')

    _render_content(parts, content_groups, have_items, env_name)

    if assets:
        overridden = sum(1 for _rel, over in assets if over)
        parts.append('<div class="t4i-row t4i-head">Assets '
                     '<span class="t4i-dim">%d, %d overridden</span></div>'
                     % (len(assets), overridden))
        for rel, over in assets:
            parts.append('<div class="t4i-row t4i-asset%s">' % (" t4i-on" if over else ""))
            parts.append('<span class="t4i-val"><code>%s</code></span>' % _esc(rel))
            parts.append('<button class="t4i-copy" data-copy="t4 edit %s">%s</button>'
                         % (_esc(rel), "editing" if over else "edit"))
            parts.append('</div>')

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

    parts.append('</div></div>')
    return STYLE + "".join(parts) + SCRIPT


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
.t4i-items{flex-wrap:wrap;gap:4px 8px}
a.t4i-item{color:#9fb4ff;text-decoration:none;border-bottom:1px dotted #44506e}
a.t4i-item:hover{color:#fff}
/* Flashed when a panel item is clicked, so the row and the page agree. */
html .t4c-flash{outline:3px solid #ffd34d!important;outline-offset:-3px}
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
 a.t4i-item{color:#2b4bbf}
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
  root.addEventListener("click", function (e) {
    var link = e.target.closest("a.t4i-item");
    if (!link) return;
    var anchor = document.getElementById("d.en." + link.getAttribute("data-item"));
    if (!anchor) return;
    e.preventDefault();
    var target = anchor.nextElementSibling || anchor;
    target.scrollIntoView({ behavior: "smooth", block: "center" });
    target.classList.add("t4c-flash");
    setTimeout(function () { target.classList.remove("t4c-flash"); }, 1600);
  });
  var KEY = "t4-inspector-open";
  try { if (localStorage.getItem(KEY) === "1") root.dataset.collapsed = "0"; } catch (e) {}
  root.querySelector("#t4i-toggle").addEventListener("click", function () {
    var open = root.dataset.collapsed === "1";
    root.dataset.collapsed = open ? "0" : "1";
    try { localStorage.setItem(KEY, open ? "1" : "0"); } catch (e) {}
  });
  root.addEventListener("click", function (e) {
    var btn = e.target.closest(".t4i-copy");
    if (!btn) return;
    var text = btn.getAttribute("data-copy");
    var done = function () {
      var old = btn.textContent;
      btn.textContent = "copied";
      setTimeout(function () { btn.textContent = old; }, 1200);
    };
    if (navigator.clipboard) { navigator.clipboard.writeText(text).then(done, done); }
    else { done(); }
  });
})();
</script>
"""
