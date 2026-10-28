"""A small site mirror.

T4 publishes static output. Having it on disk gives real rendered markup to
develop CSS and JS against without a publish cycle.

This is a deliberately plain crawler built on the standard library rather than
a shell out to wget, because wget is absent on Windows and not standard on
macOS. It fetches same-host pages and their assets, and -- importantly -- it
does NOT rewrite the files it saves. They stay byte-identical to what T4
published, so markup copied out of them can go straight back into a layout.
URL rewriting happens at serve time instead; see serve.py.
"""
import collections
import os
import pathlib
import posixpath
import re
import urllib.error
import urllib.parse
import time
import urllib.request

USER_AGENT = ("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
              "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/131.0 Safari/537.36")

ASSET_ATTRS = ("src", "href", "data-src", "poster")
PAGE_TYPES = ("text/html", "application/xhtml+xml")

# Faceted listings generate unbounded permutations; skip them.
TRAP_QUERY = re.compile(r"[?&](sort|filter|page|start|view|date|q|search)=", re.I)
LINK_RE = re.compile(
    r"""<(?:a|link|script|img|source|iframe|video)\b[^>]*?\b(?:%s)\s*=\s*["']([^"'>]+)["']"""
    % "|".join(ASSET_ATTRS), re.I)
CSS_URL_RE = re.compile(r"""url\(\s*["']?([^"')]+)["']?\s*\)""", re.I)


def local_path(dest, parsed):
    """Map a URL onto a file, the way a static server would serve it back."""
    path = urllib.parse.unquote(parsed.path)
    if path.endswith("/") or not posixpath.basename(path):
        path = posixpath.join(path, "index.html")
    rel = path.lstrip("/")
    # Keep everything inside dest even if the site serves odd paths.
    full = (pathlib.Path(dest) / rel).resolve()
    if not str(full).startswith(str(pathlib.Path(dest).resolve())):
        return None
    return full


# Statuses worth waiting out rather than counting as a failure. 429 and 503
# are a server asking for a slower crawl; 502 and 504 are usually a proxy
# hiccup. A 403 is not here on purpose: that is a refusal, and retrying it
# faster is what gets an address blocked.
RETRY_STATUS = {429, 500, 502, 503, 504}

# A crawl is thousands of requests from one address in a few minutes, which
# is indistinguishable from an attack to a WAF. A small gap between requests
# costs a few minutes over a whole site and avoids looking like one.
DEFAULT_DELAY = 0.2


def fetch(url, timeout=30, retries=3, delay=DEFAULT_DELAY, log=None):
    """Fetch one URL, waiting out the failures that are worth waiting out.

    Returns (bytes, content_type). Raises the last error if it never
    succeeds, so the caller can tell a refusal from a transport failure.
    """
    attempt = 0
    while True:
        req = urllib.request.Request(url)
        req.add_header("User-Agent", USER_AGENT)
        req.add_header("Accept", "*/*")
        try:
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                return resp.read(), resp.headers.get_content_type()
        except urllib.error.HTTPError as exc:
            if exc.code not in RETRY_STATUS or attempt >= retries:
                raise
            # A server that says how long to wait has told us what it wants.
            wait = exc.headers.get("Retry-After") if exc.headers else None
            try:
                pause = float(wait)
            except (TypeError, ValueError):
                pause = max(delay, 1.0) * (2 ** attempt)
        except urllib.error.URLError:
            if attempt >= retries:
                raise
            pause = max(delay, 1.0) * (2 ** attempt)

        attempt += 1
        if log:
            log("    retry %d/%d in %.1fs  %s" % (attempt, retries, pause, url))
        time.sleep(pause)


def extract_links(body, content_type):
    if content_type == "text/css":
        return CSS_URL_RE.findall(body)
    return LINK_RE.findall(body)


def describe_error(exc):
    """A reason someone can act on, rather than an exception class name."""
    if isinstance(exc, urllib.error.HTTPError):
        if exc.code in (401, 403):
            return ("%s %s - refused. A firewall or WAF may be blocking this "
                    "crawl; try --delay 1" % (exc.code, exc.reason))
        if exc.code == 429:
            return "429 - rate limited even after retrying; try --delay 1"
        return "%s %s" % (exc.code, exc.reason)
    if isinstance(exc, urllib.error.URLError):
        reason = getattr(exc, "reason", exc)
        text = str(reason) or type(reason).__name__
        if "Name or service not known" in text or "nodename nor servname" in text:
            return "%s - host not resolving; check the VPN" % text
        if "timed out" in text.lower():
            return "%s - no response; the VPN may be down, or a firewall is "\
                   "dropping the connection" % text
        return text
    return "%s: %s" % (type(exc).__name__, exc)


def crawl(base_url, dest, reject_ext=(), limit=None, timeout=30, log=print,
          delay=DEFAULT_DELAY, retries=3):
    """Breadth-first crawl of one host. Returns (pages, assets, errors)."""
    start = urllib.parse.urlsplit(base_url)
    host = start.netloc
    dest = pathlib.Path(dest)
    dest.mkdir(parents=True, exist_ok=True)

    queue = collections.deque([base_url])
    seen = {base_url}
    pages = assets = errors = 0
    fetched = consecutive = 0
    reject = {e.lower().lstrip(".") for e in reject_ext}

    while queue:
        if limit and (pages + assets) >= limit:
            break
        url = queue.popleft()
        parsed = urllib.parse.urlsplit(url)

        ext = posixpath.splitext(parsed.path)[1].lstrip(".").lower()
        if ext in reject:
            continue

        target = local_path(dest, parsed)
        if target is None:
            continue

        if fetched and delay:
            time.sleep(delay)

        try:
            raw, content_type = fetch(url, timeout=timeout, retries=retries,
                                      delay=delay, log=log)
            fetched += 1
        except Exception as exc:                      # noqa: BLE001 - reported
            errors += 1
            consecutive += 1
            log("  ! %s  %s" % (url, describe_error(exc)))
            # Grinding out hundreds of identical failures helps nobody, and
            # against a WAF it is the thing that keeps the block in place.
            if consecutive >= 10:
                log("\n  Stopping: %d requests in a row failed." % consecutive)
                log("  If this is a firewall or rate limit, re-run with "
                    "--delay 1 (or higher).")
                log("  Progress is kept; re-running resumes from what is on "
                    "disk.")
                break
            continue
        consecutive = 0

        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(raw)                      # verbatim, never rewritten

        is_page = content_type in PAGE_TYPES
        if is_page:
            pages += 1
        else:
            assets += 1
        if (pages + assets) % 50 == 0:
            log("  %d pages, %d assets" % (pages, assets))

        if not (is_page or content_type == "text/css"):
            continue

        body = raw.decode("utf-8", errors="replace")
        for href in extract_links(body, content_type):
            href = href.strip()
            if not href or href.startswith(("#", "data:", "mailto:", "tel:", "javascript:")):
                continue
            absolute = urllib.parse.urljoin(url, href)
            split = urllib.parse.urlsplit(absolute)
            if split.scheme not in ("http", "https") or split.netloc != host:
                continue
            if TRAP_QUERY.search(absolute):
                continue
            clean = urllib.parse.urlunsplit(
                (split.scheme, split.netloc, split.path, split.query, ""))
            if clean not in seen:
                seen.add(clean)
                queue.append(clean)

    return pages, assets, errors
