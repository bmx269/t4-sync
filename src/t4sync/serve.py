"""Serve a mirrored site locally, with overrides and serve-time URL rewriting.

Two things a plain static server does not do:

1. Override layer -- a file in the overrides directory wins over the mirrored
   copy, so CSS and JS can be iterated against real published markup without
   touching the mirror.

2. Rewrites -- rules are applied to HTML responses on the way out, the way
   mod_substitute would. The mirrored files are never modified, so they stay
   byte-identical to what T4 published and remain safe to paste back into a
   layout. T4 hardcodes absolute production URLs into published pages, which
   would otherwise dead-end in a local copy.

3. Origin proxy (optional) -- anything missing locally is fetched from the live
   site on demand and cached, in the spirit of stage_file_proxy. Images and
   documents dominate a mirror's size while contributing nothing to front-end
   work, so this lets you mirror only pages, CSS and JS and still see a
   complete site.
"""
import functools
import http.server
import io
import os
import re
import socketserver
import sys
import threading
import time

from .inspector import (ContentTypeIndex, InspectorContext, LayoutIndex,
                        LAYOUT_ID_META, LAYOUT_META,
                        annotate_components, build_comment, build_panel, fence)
from .render import PreviewEngine, status_comment
import urllib.error
import urllib.parse
import urllib.request

DEFAULT_RULES = "# <regex><TAB><replacement>, one per line. '#' starts a comment.\n"

RELOAD_PATH = "/__t4_reload"

# Injected into HTML responses. Server-sent events rather than a websocket so
# this stays standard-library only; the browser reconnects on its own if the
# server restarts.
RELOAD_SCRIPT = """
<script>
(function () {
  var es = new EventSource(%r);
  es.onmessage = function (e) { if (e.data === "reload") location.reload(); };
})();
</script>
""" % RELOAD_PATH


def transform_html(body, rules=(), inject_reload=False, inspector=None):
    """Apply rewrite rules, then append anything injected.

    Separate from the handler so it can be tested without binding a socket.
    `inspector`, when given, is (LayoutIndex, overrides_dir, env_name).
    """
    for pattern, replacement in rules:
        body = pattern.sub(replacement, body)

    prefix = ""
    extra = ""
    if inspector:
        kwargs = dict(overrides_dir=inspector.overrides_dir,
                      env_name=inspector.env_name,
                      mirror_dir=inspector.mirror_dir,
                      url_path=inspector.url_path,
                      resolver=inspector.resolver)
        if inspector.mode in ("comments", "both"):
            prefix = fence(build_comment(body, inspector.index, **kwargs))
            body, marked = annotate_components(
                body, inspector.content_types,
                inspector.component_markers, inspector.env_name)
            if marked:
                prefix = prefix.replace(
                    "<!-- END T4 DEBUG -->",
                    "<!-- COMPONENTS: %d marked inline -->\n<!-- END T4 DEBUG -->"
                    % marked)
        if inspector.mode in ("panel", "both"):
            extra += fence(build_panel(body, inspector.index, **kwargs))
    if inject_reload:
        extra += fence(RELOAD_SCRIPT)

    if extra:
        if "</body>" in body:
            body = body.replace("</body>", extra + "</body>", 1)
        else:
            body += extra
    return prefix + body


class Watcher:
    """Polls a directory tree and reports when anything changes.

    Polling rather than an OS watch API: the standard library has no portable
    file-watching, and a poll over an overrides directory -- which holds a
    handful of files being actively edited -- costs nothing.
    """

    def __init__(self, paths, interval=0.4):
        self.paths = [p for p in paths if p]
        self.interval = interval
        self.version = 0
        self._stamp = None
        self._lock = threading.Condition()
        self._stop = threading.Event()

    def _fingerprint(self):
        latest = []
        for root in self.paths:
            for dirpath, _dirnames, filenames in os.walk(root):
                for name in filenames:
                    if name.startswith("."):
                        continue
                    full = os.path.join(dirpath, name)
                    try:
                        st = os.stat(full)
                    except OSError:
                        continue
                    latest.append((full, st.st_mtime_ns, st.st_size))
        return hash(tuple(sorted(latest)))

    def start(self):
        thread = threading.Thread(target=self._loop, daemon=True)
        thread.start()
        return self

    def _loop(self):
        self._stamp = self._fingerprint()
        while not self._stop.is_set():
            time.sleep(self.interval)
            current = self._fingerprint()
            if current != self._stamp:
                self._stamp = current
                with self._lock:
                    self.version += 1
                    self._lock.notify_all()

    def wait(self, seen, timeout):
        with self._lock:
            if self.version != seen:
                return self.version
            self._lock.wait(timeout)
            return self.version


def load_rules(path):
    rules = []
    if not path or not os.path.isfile(path):
        return rules
    with open(path, encoding="utf-8") as fh:
        for lineno, line in enumerate(fh, 1):
            line = line.rstrip("\n")
            if not line.strip() or line.lstrip().startswith("#"):
                continue
            if "\t" not in line:
                sys.stderr.write("%s:%d: no tab separator\n" % (path, lineno))
                continue
            pattern, _, replacement = line.partition("\t")
            try:
                rules.append((re.compile(pattern), replacement))
            except re.error as exc:
                sys.stderr.write("%s:%d: bad regex (%s)\n" % (path, lineno, exc))
    return rules


class MirrorHandler(http.server.SimpleHTTPRequestHandler):
    overrides = None
    rules = ()
    proxy_origin = None
    proxy_cache = True
    watcher = None
    inspector = None
    previewer = None

    def translate_path(self, path):
        mirrored = super().translate_path(path)
        if self.overrides:
            rel = os.path.relpath(mirrored, self.directory)
            candidate = os.path.join(self.overrides, rel)
            if os.path.isfile(candidate):
                return candidate
        if os.path.isdir(mirrored):
            index = os.path.join(mirrored, "index.html")
            if os.path.isfile(index):
                return index
        return mirrored

    # -- origin proxy ------------------------------------------------------

    def _proxy(self, local_target):
        """Fetch this path from the origin, optionally caching it locally."""
        origin = self.proxy_origin.rstrip("/")
        url = origin + urllib.parse.quote(self.path.split("?", 1)[0])
        try:
            req = urllib.request.Request(url)
            req.add_header("User-Agent", "t4-sync/serve (origin proxy)")
            with urllib.request.urlopen(req, timeout=30) as resp:
                body = resp.read()
                content_type = resp.headers.get("Content-Type", "application/octet-stream")
        except urllib.error.HTTPError as exc:
            self.send_error(exc.code, "origin returned %s" % exc.code)
            return None
        except Exception as exc:
            self.send_error(502, "origin fetch failed: %s" % type(exc).__name__)
            return None

        if self.proxy_cache and local_target:
            try:
                target = os.path.abspath(local_target)
                os.makedirs(os.path.dirname(target), exist_ok=True)
                with open(target, "wb") as fh:
                    fh.write(body)
            except OSError:
                pass  # serving it still works; caching is best effort

        self.send_response(200)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("X-T4-Proxied", "origin")
        self.end_headers()
        return io.BytesIO(body)

    def do_GET(self):
        if self.watcher and self.path.split("?")[0] == RELOAD_PATH:
            return self._reload_stream()
        return super().do_GET()

    def _reload_stream(self):
        """Hold the connection open and emit an event whenever a file changes."""
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream")
        self.send_header("Cache-Control", "no-store")
        self.send_header("Connection", "keep-alive")
        self.end_headers()
        seen = self.watcher.version
        try:
            while True:
                current = self.watcher.wait(seen, timeout=20)
                if current != seen:
                    seen = current
                    self.wfile.write(b"data: reload\n\n")
                else:
                    self.wfile.write(b": keep-alive\n\n")   # keeps proxies happy
                self.wfile.flush()
        except (BrokenPipeError, ConnectionResetError):
            pass

    def send_head(self):
        path = self.translate_path(self.path)

        if self.proxy_origin and not os.path.isfile(path):
            sys.stderr.write("  proxy %s\n" % self.path)
            return self._proxy(path)

        # HTML needs rewriting when there are rules, and needs the reload
        # script whenever the watcher is running. Gating on rules alone meant
        # live reload silently did nothing for a project with no rewrites.
        needs_transform = (bool(self.rules) or bool(self.watcher)
                           or bool(self.inspector) or bool(self.previewer))
        if not (needs_transform and path.endswith((".html", ".htm"))):
            return super().send_head()
        try:
            with open(path, "rb") as fh:
                raw = fh.read()
        except OSError:
            return super().send_head()

        inspector = None
        if self.inspector:
            inspector = self.inspector._replace(mirror_dir=self.directory,
                                                url_path=self.path)
        body = raw.decode("utf-8", errors="replace")

        # Apply locally-edited layouts before anything else looks at the page,
        # so the debug output describes what is actually being served.
        preview_status = None
        if self.previewer:
            name = LAYOUT_META.search(body)
            found_id = LAYOUT_ID_META.search(body)
            body, preview_status = self.previewer.apply(
                body, name.group(1) if name else None,
                found_id.group(1) if found_id else None)

        body = transform_html(body, self.rules,
                              inject_reload=bool(self.watcher),
                              inspector=inspector)
        if preview_status:
            body = fence(status_comment(
                preview_status,
                self.inspector.env_name if self.inspector else None)) + body
        encoded = body.encode("utf-8")

        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(encoded)))
        self.send_header("Cache-Control", "no-store")   # so edits show on reload
        self.end_headers()
        return io.BytesIO(encoded)

    def log_message(self, fmt, *args):
        if getattr(self, "path", "").startswith(RELOAD_PATH):
            return          # the reload stream would otherwise log constantly
        sys.stderr.write("%s %s\n" % (self.address_string(), fmt % args))


class ThreadingServer(socketserver.ThreadingTCPServer):
    # Threaded because the reload stream holds a connection open; a serial
    # server would block every other request behind it.
    daemon_threads = True
    allow_reuse_address = True


def serve(site, overrides=None, rules_file=None, port=8321, host="127.0.0.1",
          proxy_origin=None, proxy_cache=True, reload=True,
          source_dir=None, env_name=None, inspect="both",
          component_markers=None, preview_edits=True, span_markers=None):
    MirrorHandler.overrides = os.path.abspath(overrides) if overrides else None
    MirrorHandler.rules = load_rules(rules_file)
    MirrorHandler.proxy_origin = proxy_origin
    MirrorHandler.proxy_cache = proxy_cache

    index = LayoutIndex.from_project_dir(source_dir)

    MirrorHandler.previewer = None
    if preview_edits and source_dir:
        engine = PreviewEngine(source_dir, index, span_markers=span_markers,
                               env_name=env_name)
        if engine.available:
            MirrorHandler.previewer = engine

    watcher = None
    if reload:
        # Watch the pulled source as well, so editing a layout refreshes the
        # browser the same way editing an override does.
        watched = [p for p in (MirrorHandler.overrides,
                               source_dir if preview_edits else None) if p]
        if watched:
            watcher = Watcher(watched).start()
    MirrorHandler.watcher = watcher

    if inspect and inspect != "off":
        MirrorHandler.inspector = InspectorContext(
            index=index, overrides_dir=MirrorHandler.overrides,
            env_name=env_name, mode=inspect,
            resolver=(MirrorHandler.previewer.resolver()
                      if MirrorHandler.previewer else None),
            content_types={
                "contenttype": ContentTypeIndex.from_env_dir(source_dir, "contenttype"),
                "navigation": ContentTypeIndex.from_env_dir(source_dir, "navigation"),
            },
            component_markers=component_markers)
    else:
        MirrorHandler.inspector = None

    handler = functools.partial(MirrorHandler, directory=os.path.abspath(site))

    with ThreadingServer((host, port), handler) as httpd:
        print("Serving   %s" % site)
        if MirrorHandler.overrides:
            print("Overrides %s" % MirrorHandler.overrides)
        print("Rewrites  %d rule(s) applied to HTML" % len(MirrorHandler.rules))
        if proxy_origin:
            print("Proxy     %s%s" % (proxy_origin,
                                      " (caching)" if proxy_cache else " (no cache)"))
        print("Reload    %s" % ("on - edits in overrides/ refresh the browser"
                                if watcher else "off"))
        print("Preview   %s" % ("on - local layout edits are applied to served pages"
                                if MirrorHandler.previewer else
                                "off (run `t4 pull` to enable)"))
        if inspect and inspect != "off":
            known = len(index.by_id)
            print("Inspector %s - %s" % (inspect,
                  ("%d page layout(s) mapped" % known if known else
                   "no layouts pulled yet, run `t4 pull` to map pages to files")))
        else:
            print("Inspector off")
        print("URL       http://%s:%d/" % (host, port))
        print("Ctrl-C to stop.")
        try:
            httpd.serve_forever()
        except KeyboardInterrupt:
            print("\nStopped.")
