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

# Grouped rather than passed positionally: this is handed from the request
# handler through transform_html to build_panel, and a bare tuple made that
# chain easy to get wrong.
InspectorContext = collections.namedtuple(
    "InspectorContext",
    "index overrides_dir env_name mirror_dir url_path mode "
    "content_types component_markers")
InspectorContext.__new__.__defaults__ = (None, None, None, "/", "both", None, None)

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
            return match.group(0) + note_for(match, _endpoint)
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


def _esc(value):
    return html.escape(str(value), quote=True)


def _safe_comment(text):
    """`--` cannot appear inside an HTML comment."""
    return str(text).replace("--", "- -")


def build_comment(body, index, overrides_dir=None, env_name=None,
                  mirror_dir=None, url_path="/"):
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

    files, note = index.resolve(layout_name, layout_id)
    candidates = sorted(n for n in index.by_name
                        if layout_name and n.split(".")[0] == layout_name)

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
                              os.path.isfile(os.path.join(overrides_dir, rel)))
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
        over = bool(overrides_dir and os.path.isfile(os.path.join(overrides_dir, rel)))
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
    base = os.path.join(mirror_dir, rel) if rel else mirror_dir
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
                mirror_dir=None, url_path="/"):
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
        overridden = bool(overrides_dir and os.path.isfile(os.path.join(overrides_dir, rel)))
        assets.append((rel, overridden))

    children = child_layouts(mirror_dir, url_path)
    return _render(rows, assets, children, url_path)


def _render(rows, assets, children=(), url_path="/"):
    parts = ['<div id="t4-inspector" data-collapsed="1">',
             '<button type="button" id="t4i-toggle" title="T4 inspector">T4</button>',
             '<div id="t4i-body">']

    for label, value, copy_target in rows:
        parts.append('<div class="t4i-row">')
        parts.append('<span class="t4i-key">%s</span>' % _esc(label))
        parts.append('<span class="t4i-val">%s</span>' % value)
        if copy_target:
            parts.append('<button class="t4i-copy" data-copy="t4 edit %s">edit</button>'
                         % _esc(copy_target))
        parts.append('</div>')

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
@media (prefers-color-scheme:light){
 #t4-inspector{background:#fff;color:#1a1d25;border-color:#d4d8e0}
 #t4-inspector code{background:#f1f3f7}
 .t4i-key,.t4i-dim{color:#5a6376}
 .t4i-asset.t4i-on code{background:#e6f7ec;color:#17603a}
}
</style>
"""

SCRIPT = """
<script id="t4i-script">
(function () {
  var root = document.getElementById("t4-inspector");
  if (!root) return;
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
