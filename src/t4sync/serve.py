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
import urllib.error
import urllib.parse
import urllib.request

DEFAULT_RULES = "# <regex><TAB><replacement>, one per line. '#' starts a comment.\n"


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
    _proxy_misses = set()

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

    def send_head(self):
        path = self.translate_path(self.path)

        if self.proxy_origin and not os.path.isfile(path):
            sys.stderr.write("  proxy %s\n" % self.path)
            return self._proxy(path)

        if not (self.rules and path.endswith((".html", ".htm"))):
            return super().send_head()
        try:
            with open(path, "rb") as fh:
                raw = fh.read()
        except OSError:
            return super().send_head()

        body = raw.decode("utf-8", errors="replace")
        for pattern, replacement in self.rules:
            body = pattern.sub(replacement, body)
        encoded = body.encode("utf-8")

        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(encoded)))
        self.send_header("Cache-Control", "no-store")   # so edits show on reload
        self.end_headers()
        return io.BytesIO(encoded)

    def log_message(self, fmt, *args):
        sys.stderr.write("%s %s\n" % (self.address_string(), fmt % args))


def serve(site, overrides=None, rules_file=None, port=8321, host="127.0.0.1",
          proxy_origin=None, proxy_cache=True):
    MirrorHandler.overrides = os.path.abspath(overrides) if overrides else None
    MirrorHandler.rules = load_rules(rules_file)
    MirrorHandler.proxy_origin = proxy_origin
    MirrorHandler.proxy_cache = proxy_cache
    handler = functools.partial(MirrorHandler, directory=os.path.abspath(site))

    socketserver.TCPServer.allow_reuse_address = True
    with socketserver.TCPServer((host, port), handler) as httpd:
        print("Serving   %s" % site)
        if MirrorHandler.overrides:
            print("Overrides %s" % MirrorHandler.overrides)
        print("Rewrites  %d rule(s) applied to HTML" % len(MirrorHandler.rules))
        if proxy_origin:
            print("Proxy     %s%s" % (proxy_origin,
                                      " (caching)" if proxy_cache else " (no cache)"))
        print("URL       http://%s:%d/" % (host, port))
        print("Ctrl-C to stop.")
        try:
            httpd.serve_forever()
        except KeyboardInterrupt:
            print("\nStopped.")
