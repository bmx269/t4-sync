"""Preview a layout edit against the last published output.

T4 tags and Handlebars are evaluated by T4's publish engine; nothing local can
run them. But the published page *is* the result of running them, so for a
layout whose source we also hold, the two can be aligned:

    source:   <lit A> <t4 .../> <lit B> <t4 .../> <lit C>
    output:   <lit A> ...????... <lit B> ...????... <lit C>

The literal chunks appear verbatim in the output, so whatever sits between them
is what the tags produced. Capture those spans, then re-emit the *edited*
source with the captured output substituted back in. Edits to literal markup --
wrapping a tag in a div, changing a class, reordering -- then show up locally
without a publish cycle.

What this is not: a renderer. A change to what a tag *does* (a different
navigation id, a new tag, altered Handlebars logic) cannot be previewed,
because its output has never been produced. Those are reported rather than
guessed at, so a preview never silently shows something T4 would not.
"""
import collections
import re

# A T4 tag's attributes can contain `>` inside quotes, e.g.
#   format="<p>Email: $value</p>"
# so the body is matched as a run of quoted strings or non-quote, non-`>`
# characters rather than simply "anything but >".
_ATTRS = r'(?:[^>"\']|"[^"]*"|\'[^\']*\')*?'
TAG_RE = re.compile(r"<t4\b%s/>|<t4\b%s</t4>" % (_ATTRS, _ATTRS), re.I)
ID_ATTR_RE = re.compile(r'\bid\s*=\s*["\'](\d+)["\']', re.I)

# Openers and closers a site's layouts emit around a tag's output. Both forms
# capture the id so an opener can be paired with its closer.
DEFAULT_SPAN_MARKERS = [
    (r"<!--\s*n:[^>(]*\((?P<id>\d+)\)\s*-->", r"<!--\s*/n:(?P<id>\d+)\s*-->"),
    (r"<!--\s*ct:(?P<id>\d+)[^>]*-->", r"<!--\s*/ct:(?P<id>\d+)[^>]*-->"),
]


class Unalignable(Exception):
    """The published source could not be located in the published page."""


def split_source(src):
    """[('lit', text) | ('tag', text), ...] in document order."""
    parts, last = [], 0
    for match in TAG_RE.finditer(src):
        parts.append(("lit", src[last:match.start()]))
        parts.append(("tag", match.group(0)))
        last = match.end()
    parts.append(("lit", src[last:]))
    return parts


def tag_key(tag):
    """A stable identity for a tag, so it survives being moved or wrapped."""
    return re.sub(r"\s+", " ", tag.strip().lower())


def align(published_src, page, span_markers=None):
    """Locate the published source inside the page.

    Returns (start, end, segments). `segments` is an ordered list of
    {"tags": [tag_key, ...], "html": str}: one entry per tag whose own output
    could be isolated, and one shared entry per run of tags that could not be
    told apart. A shared run can still be previewed as a block -- what cannot
    be done is placing new markup *inside* it.
    """
    parts = split_source(published_src)
    anchors = [(i, text) for i, (kind, text) in enumerate(parts)
               if kind == "lit" and text.strip()]
    if not anchors:
        raise Unalignable("the layout has no literal markup to anchor on")

    cursor, start, placements = 0, None, []
    for index, text in anchors:
        found = page.find(text, cursor)
        if found < 0:
            raise Unalignable("literal chunk not found in the page: %r"
                              % text.strip()[:60])
        if start is None:
            start = found
        placements.append((index, found, found + len(text)))
        cursor = found + len(text)
    end = cursor

    segments = []
    for position, (index, literal_start, _literal_end) in enumerate(placements):
        gap_start = placements[position - 1][2] if position else start
        previous_index = placements[position - 1][0] if position else -1
        tags = [parts[i][1] for i in range(previous_index + 1, index)
                if parts[i][0] == "tag"]
        if tags and gap_start < literal_start:
            segments.extend(_split_gap(page, gap_start, literal_start, tags,
                                       span_markers))
    return start, end, segments


def _split_gap(page, gap_start, gap_end, tags, span_markers):
    """Attribute a span of output to the tags that produced it."""
    segment = page[gap_start:gap_end]
    if len(tags) == 1:
        return [{"tags": [tag_key(tags[0])], "html": segment}]

    # Locate whichever tags the site's own markers delimit. The rest can only
    # be handled as the runs between them.
    located = {}
    for order, tag in enumerate(tags):
        found_id = ID_ATTR_RE.search(tag)
        if not found_id:
            continue
        span = _locate(segment, found_id.group(1), span_markers)
        if span:
            located[order] = span

    if not located:
        return [{"tags": [tag_key(t) for t in tags], "html": segment}]

    segments, cursor, pending = [], 0, []
    for order, tag in enumerate(tags):
        if order not in located:
            pending.append(tag_key(tag))
            continue
        span_start, span_end = located[order]
        if pending:
            segments.append({"tags": pending, "html": segment[cursor:span_start]})
            pending = []
        elif span_start > cursor:
            # Output before the first located tag with nothing to attribute it
            # to; keep it so the page still reassembles byte-for-byte.
            segments.append({"tags": [], "html": segment[cursor:span_start]})
        segments.append({"tags": [tag_key(tag)], "html": segment[span_start:span_end]})
        cursor = span_end

    if pending or cursor < len(segment):
        segments.append({"tags": pending, "html": segment[cursor:]})
    return segments


def _find_with_id(text, pattern, wanted, after=0):
    """First match of `pattern` whose `id` group equals `wanted`."""
    for match in re.finditer(pattern, text[after:], re.I):
        if match.groupdict().get("id") == wanted:
            return (after + match.start(), after + match.end())
    return None


def _locate(text, wanted, span_markers):
    for open_pattern, close_pattern in (span_markers or DEFAULT_SPAN_MARKERS):
        opener = _find_with_id(text, open_pattern, wanted)
        if opener is None:
            continue
        closer = _find_with_id(text, close_pattern, wanted, after=opener[1])
        if closer is None:
            continue
        return opener[0], closer[1]
    return None


def render(edited_src, segments):
    """Re-emit the edited source with captured output substituted back in.

    Returns (html, problems). `problems` is a list of (kind, detail):

      ("unpublished", tag)   the tag has no captured output at all
      ("inseparable", tags)  the edit puts markup inside a run of tags whose
                             individual output could not be told apart, so the
                             markup cannot be placed faithfully

    The second case matters: wrapping one tag of such a run would silently wrap
    the whole run. Reporting it is the difference between a preview that is
    trustworthy and one that merely looks right.
    """
    index = {}
    for position, segment in enumerate(segments):
        for key in segment["tags"]:
            index.setdefault(key, position)

    parts = split_source(edited_src)

    # Which run does each tag belong to, and does the edit break the run apart?
    runs = collections.defaultdict(list)
    for order, (kind, text) in enumerate(parts):
        if kind == "tag":
            position = index.get(tag_key(text))
            if position is not None and len(segments[position]["tags"]) > 1:
                runs[position].append(order)

    broken = set()
    for position, orders in runs.items():
        for left, right in zip(orders, orders[1:]):
            between = [parts[i][1] for i in range(left + 1, right) if parts[i][0] == "lit"]
            if any(chunk.strip() for chunk in between):
                broken.add(position)
                break
        # Markup added immediately around a run member is equally unplaceable.
        first, last = orders[0], orders[-1]
        before = parts[first - 1][1] if first and parts[first - 1][0] == "lit" else ""
        after = parts[last + 1][1] if last + 1 < len(parts) and parts[last + 1][0] == "lit" else ""
        if len(orders) < len(segments[position]["tags"]) and (before.strip() or after.strip()):
            broken.add(position)

    out, problems, emitted = [], [], set()
    for kind, text in parts:
        if kind == "lit":
            out.append(text)
            continue
        key = tag_key(text)
        position = index.get(key)
        if position is None:
            problems.append(("unpublished", text))
            out.append("<!-- T4 PREVIEW: %s has no published output to reuse, "
                       "so it cannot be previewed -->" % text.replace("--", "- -"))
            continue
        if position in emitted:
            continue
        emitted.add(position)
        if position in broken:
            problems.append(("inseparable", segments[position]["tags"]))
        out.append(segments[position]["html"])

    for position, segment in enumerate(segments):
        if not segment["tags"] and position not in emitted:
            out.append(segment["html"])
    return "".join(out), problems


def preview(page, published_src, edited_src, span_markers=None):
    """Splice a locally-edited layout into a published page.

    Returns (html, problems); see `render` for what a problem is.
    """
    if published_src == edited_src:
        return page, []
    start, end, segments = align(published_src, page, span_markers)
    rendered, problems = render(edited_src, segments)
    return page[:start] + rendered + page[end:], problems


class PreviewEngine:
    """Applies locally-edited page layouts to mirrored pages as they are served."""

    def __init__(self, env_dir, index, span_markers=None, env_name=None):
        self.env_dir = env_dir
        self.index = index
        self.span_markers = span_markers
        self.env_name = env_name
        self._published = {}
        self._load_published()

    def _load_published(self):
        """The as-pulled source, kept verbatim in _raw by `t4 pull`."""
        import json
        import os
        path = os.path.join(self.env_dir or "", "_raw", "pageLayout.detail.json")
        try:
            with open(path, encoding="utf-8") as fh:
                for record in json.load(fh):
                    self._published[str(record.get("id"))] = record
        except (OSError, ValueError):
            pass

    @property
    def available(self):
        return bool(self._published)

    @property
    def records(self):
        return list(self._published.values())

    def resolver(self):
        return LayoutResolver(self.index, self.records)

    def apply(self, page, layout_name=None, layout_id=None):
        """Return (html, status). `status` is None when nothing was applied."""
        import os
        if not self._published:
            return page, None

        # Identify the layout from the page itself, so a published name that
        # does not name one layout still resolves.
        record, _why = self.resolver().resolve(page, layout_name, layout_id)
        if not record:
            return page, None
        files = self.index.files_for(record["id"]) if self.index else []
        if not files:
            return page, None

        html, problems, applied = page, [], []
        for rel, meta in files:
            field = meta.get("field")
            published_src = record.get(field)
            if not isinstance(published_src, str):
                continue
            try:
                with open(os.path.join(self.env_dir, rel), encoding="utf-8",
                          newline="") as fh:
                    edited_src = fh.read()
            except OSError:
                continue
            if edited_src == published_src:
                continue
            try:
                html, found = preview(html, published_src, edited_src,
                                      self.span_markers)
            except Unalignable as exc:
                problems.append(("unalignable", "%s: %s" % (rel, exc)))
                continue
            applied.append(rel)
            problems.extend(found)

        if not applied:
            return page, None
        return html, {"applied": applied, "problems": problems}


def status_comment(status, env_name=None):
    """Render a preview status as HTML comments for the debug output."""
    if not status:
        return ""
    lines = ["<!-- T4 PREVIEW: local layout edits applied -->"]
    for rel in status["applied"]:
        lines.append("<!--   from t4-source/%s/%s -->"
                     % (env_name or "<env>", rel.replace("--", "- -")))
    for kind, detail in status["problems"]:
        if kind == "inseparable":
            names = ", ".join(str(t)[:60] for t in detail)
            lines.append("<!--   WARNING: markup was placed around tags whose "
                         "individual output cannot be separated -->")
            lines.append("<!--   affected: %s -->" % names.replace("--", "- -"))
            lines.append("<!--   what you see wraps the whole run, which is not "
                         "what T4 will publish -->")
        elif kind == "unpublished":
            lines.append("<!--   NOT PREVIEWED: %s has no published output to "
                         "reuse -->" % str(detail).replace("--", "- -"))
        else:
            lines.append("<!--   %s: %s -->" % (kind, str(detail).replace("--", "- -")))
    return "\n".join(lines) + "\n"


class LayoutResolver:
    """Works out which page layout actually produced a page.

    T4 publishes a layout *name* into nearly every page, but that name need
    not identify one layout: a site can publish `u_webpage` from any of
    several `u_webpage.*` layouts. Rather than ask for a mapping, identify the
    layout from the page itself, using two signals:

      alignment   the layout's literal markup must appear, in order, in the
                  page. A layout that did not produce the page generally fails
                  this outright.
      tag ids     among the survivors, score the tag ids that differ between
                  them by whether they appear in the page. T4 layouts commonly
                  echo media and navigation ids into the output.

    Where two layouts are genuinely indistinguishable -- identical literals and
    identical tags -- it says so rather than picking one.
    """

    def __init__(self, index, published_records):
        self.index = index
        self.records = {str(r.get("id")): r for r in (published_records or [])}

    def candidates(self, layout_name, layout_id=None):
        if layout_id and str(layout_id) in self.records:
            return [self.records[str(layout_id)]]
        if not layout_name:
            return []
        exact = [r for r in self.records.values() if r.get("name") == layout_name]
        if exact:
            return exact
        prefix = layout_name.split(".")[0]
        return [r for r in self.records.values()
                if (r.get("name") or "").split(".")[0] == prefix]

    def resolve(self, page, layout_name, layout_id=None):
        """Return (record, reason). `record` is None when undecidable."""
        pool = self.candidates(layout_name, layout_id)
        if not pool:
            return None, "no layout named %r in the local pull" % layout_name
        if len(pool) == 1:
            return pool[0], "named uniquely"

        aligned = [r for r in pool if self._aligns(r, page)]
        if len(aligned) == 1:
            return aligned[0], "its markup is the only one that fits the page"
        pool = aligned or pool

        ids = {str(r["id"]): self._tag_ids(r) for r in pool}
        shared = set.intersection(*ids.values()) if ids else set()
        scored = sorted(
            ((len({i for i in ids[str(r["id"])] - shared if i in page}), r) for r in pool),
            key=lambda pair: -pair[0])
        if len(scored) > 1 and scored[0][0] == scored[1][0]:
            tied = [r["name"] for score, r in scored if score == scored[0][0]]
            return None, ("indistinguishable from the published page: %s"
                          % ", ".join(sorted(set(tied))))
        return scored[0][1], ("matched on %d tag id(s) unique to it" % scored[0][0])

    def _aligns(self, record, page):
        for field in ("headerCode", "footerCode"):
            source = record.get(field)
            if not isinstance(source, str) or not source.strip():
                continue
            try:
                align(source, page)
            except Unalignable:
                return False
        return True

    @staticmethod
    def _tag_ids(record):
        ids = set()
        for field in ("headerCode", "footerCode"):
            for match in TAG_RE.finditer(record.get(field) or ""):
                found = ID_ATTR_RE.search(match.group(0))
                if found:
                    ids.add(found.group(1))
        return ids
