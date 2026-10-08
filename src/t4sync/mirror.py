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


def fetch(url, timeout=30):
    req = urllib.request.Request(url)
    req.add_header("User-Agent", USER_AGENT)
    req.add_header("Accept", "*/*")
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return resp.read(), resp.headers.get_content_type()


def extract_links(body, content_type):
    if content_type == "text/css":
        return CSS_URL_RE.findall(body)
    return LINK_RE.findall(body)


def crawl(base_url, dest, reject_ext=(), limit=None, timeout=30, log=print):
    """Breadth-first crawl of one host. Returns (pages, assets, errors)."""
    start = urllib.parse.urlsplit(base_url)
    host = start.netloc
    dest = pathlib.Path(dest)
    dest.mkdir(parents=True, exist_ok=True)

    queue = collections.deque([base_url])
    seen = {base_url}
    pages = assets = errors = 0
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

        try:
            raw, content_type = fetch(url, timeout=timeout)
        except urllib.error.HTTPError as exc:
            errors += 1
            log("  ! %s %s" % (exc.code, url))
            continue
        except Exception as exc:
            errors += 1
            log("  ! %s %s" % (type(exc).__name__, url))
            continue

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
