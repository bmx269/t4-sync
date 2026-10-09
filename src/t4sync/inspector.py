"""A small overlay showing which T4 layout rendered a page, and what to edit.

T4 publishes `<meta name="t4-layout">` and `<meta name="t4-layout-id">` into
every page it renders. The id maps directly onto the pull manifest, so a page
being viewed locally can be traced back to the exact file on disk that
produced it -- which is otherwise guesswork across 58 layouts with names like
`u_webpage.production` and `m_open_alt_no_menu`.

The panel also lists the stylesheets and scripts the page loads, marking which
are already overridden, because that is the other half of "what do I edit".
"""
import html
import json
import os
import re

LAYOUT_META = re.compile(
    r'<meta\s+name=["\']t4-layout["\']\s+content=["\']([^"\']*)["\']', re.I)
LAYOUT_ID_META = re.compile(
    r'<meta\s+name=["\']t4-layout-id["\']\s+content=["\']([^"\']*)["\']', re.I)
ASSET_RE = re.compile(
    r'<(?:link[^>]+href|script[^>]+src)=["\']([^"\']+\.(?:css|js))["\']', re.I)


class LayoutIndex:
    """Maps a T4 layout id to the local files pulled from it."""

    def __init__(self, manifest=None, source_dir=None):
        self.by_id = {}
        self.source_dir = source_dir
        for rel, meta in (manifest or {}).items():
            if meta.get("endpoint") != "pageLayout":
                continue
            self.by_id.setdefault(str(meta.get("id")), []).append((rel, meta))

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


def _esc(value):
    return html.escape(str(value), quote=True)


def build_panel(body, index, overrides_dir=None, env_name=None):
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

        files = index.files_for(layout_id) if layout_id else []
        if files:
            for rel, _meta in files:
                rows.append(("", '<code>%s</code>' % _esc(rel),
                             "t4-source/%s/%s" % (env_name or "<env>", rel)))
        elif layout_id:
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

    return _render(rows, assets)


def _render(rows, assets):
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
