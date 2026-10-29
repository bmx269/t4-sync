"""Render a Content Layout with made-up content, before any exists.

The preview in `render.py` works by reusing output T4 already published, so
it can only show a layout that has already rendered something. That rules out
the case you most want help with: a layout you have just written, for a
content type nobody has filled in yet.

The element types make a way around it. A Content Type says what each of its
elements is -- plain text, HTML, date, list, link, image -- so a plausible
value can be invented for each one and substituted into the layout. What
comes back is the layout's shape with content in it: enough to see the markup
nest correctly and the CSS apply, which is the question being asked.

**It is not T4's renderer and does not pretend to be.** Modifiers are not
applied, navigation, media and meta tags cannot be filled in without the CMS,
and a Programmable Layout will not run. Anything it cannot fill is left
visibly marked rather than blanked, so the output never looks more finished
than it is.
"""
import datetime
import re

from .tags import TAG_TYPES

TAG_RE = re.compile(r"<t4\b([^>]*?)/?>", re.I)
# Single quotes as well as double: a tag that sits inside an HTML attribute
# has to use the other kind, and `src="<t4 type='content' .../>"` is one of
# the commonest things anyone writes.
ATTR_RE = re.compile(r"""([\w-]+)\s*=\s*(?:"([^"]*)"|'([^']*)')""")


def _attrs(inside):
    return {name.lower(): double if double else single
            for name, double, single in ATTR_RE.findall(inside)}

# Enough to read as real content without anyone mistaking it for real
# content. Deliberately not lorem ipsum: English makes a broken layout
# obvious at a glance, where Latin hides it.
WORDS = ("Example", "Sample", "Placeholder", "Draft", "Working", "Local",
         "Preview", "Test")
SENTENCES = (
    "This is sample content, generated locally so the layout can be seen "
    "with something in it.",
    "The wording here is invented. Only the shape of the markup is real.",
    "Replace this by publishing from Terminal Four, which is the only thing "
    "that renders the real values.",
)


def sample_value(element, index=0):
    """A plausible value for one element, from its verified type."""
    kind = element.get("type") or ""
    name = element.get("name") or "Element"
    word = WORDS[index % len(WORDS)]

    if kind == "HTML":
        return ("<p>%s</p>\n<p>%s</p>"
                % (SENTENCES[index % len(SENTENCES)],
                   SENTENCES[(index + 1) % len(SENTENCES)]))
    if kind == "date":
        when = datetime.date.today() + datetime.timedelta(days=14 * (index + 1))
        return when.strftime("%d %B %Y")
    if kind == "number":
        return str((index + 1) * 7)
    if kind == "image":
        # A data URI, so the preview needs nothing from the network and
        # cannot quietly show a real image from the live site.
        label = name.replace(" ", "+")
        return ("data:image/svg+xml;utf8,"
                "<svg xmlns='http://www.w3.org/2000/svg' width='480' "
                "height='320'><rect width='480' height='320' fill='%23e9edf6'/>"
                "<text x='240' y='168' font-family='sans-serif' font-size='18' "
                "fill='%236f7c9c' text-anchor='middle'>" + label +
                "</text></svg>")
    if kind == "file":
        return "/example/sample-document.pdf"
    if kind == "section or content link":
        return "/example/a-section/"
    if kind == "list":
        return "%s option" % word
    return "%s %s" % (word, name.lower())


def values_for(content_type, overrides=None):
    """{element name: sample value} for a whole content type.

    `overrides` replaces the invented value for an element by name, which is
    what makes a rendering a test rather than a look: a title long enough to
    wrap, an element left empty to see what the layout does without it, the
    exact string a bug was reported against. An override naming an element
    the type does not have is kept out and reported by `unknown_values`,
    because silently ignoring it would look like the layout dropped it.
    """
    values = {element["name"]: sample_value(element, index)
              for index, element in enumerate(content_type.get("elements") or [])}
    for name, value in (overrides or {}).items():
        if name in values:
            values[name] = "" if value is None else str(value)
    return values


def unknown_values(content_type, overrides):
    """Override names that are not elements of this type, in order."""
    elements = {element["name"]
                for element in content_type.get("elements") or []}
    return [name for name in (overrides or {}) if name not in elements]


def unfillable(kind):
    """Why a tag cannot be given a value here, or None if it can."""
    if kind == "content":
        return None
    if not kind:
        return "no type= on this tag"
    if kind not in TAG_TYPES:
        return "%r is not a T4 tag type" % kind
    return "%s needs the CMS" % TAG_TYPES[kind].get("label", kind)


def render(layout, content_type, mark=True, values=None):
    """Substitute sample values into markup containing T4 tags.

    `layout` is a pulled Content Layout or any fragment you are writing --
    the substitution is per tag, so a handful of tags you are about to paste
    into a page renders the same way a whole layout does.

    `values` is {element name: value}, already merged by `values_for`. Pass
    it to decide what goes in rather than taking what was invented.

    Returns (html, report). `report` lists what was filled and what was left,
    so the output can be read for what it is rather than taken for a publish.
    """
    if values is None:
        values = values_for(content_type)
    filled, skipped, unknown = [], [], []

    def replace(match):
        attrs = _attrs(match.group(1))
        kind = (attrs.get("type") or "").lower()
        reason = unfillable(kind)
        if reason:
            skipped.append((match.group(0), reason))
            return _placeholder(match.group(0), reason) if mark else ""

        name = attrs.get("name")
        if name in values:
            filled.append(name)
            return values[name]

        # A content tag naming an element this type does not have. Usually a
        # layout assigned to the wrong type, which is worth seeing.
        unknown.append(name)
        return (_placeholder(match.group(0), "no element named %r" % name)
                if mark else "")

    html = TAG_RE.sub(replace, layout)
    return html, {"filled": filled, "skipped": skipped, "unknown": unknown}


def _placeholder(original, reason):
    """Mark what could not be filled, visibly and in the source."""
    return ('<span style="outline:1px dashed #9a3412;color:#9a3412;'
            'font:11px ui-monospace,monospace;padding:0 .2em" '
            'title="%s">%s</span><!-- t4-sync: %s -->'
            % (_attr(reason), _attr(_shorten(original)), _comment(reason)))


def _shorten(text, limit=60):
    text = " ".join(str(text).split())
    return text if len(text) <= limit else text[:limit - 1] + "…"


def _attr(value):
    return (str(value).replace("&", "&amp;").replace('"', "&quot;")
            .replace("<", "&lt;").replace(">", "&gt;"))


def _comment(value):
    return str(value).replace("--", "- -")


PAGE = """<!doctype html>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>%(title)s - sample content</title>
%(styles)s
<div style="position:sticky;top:0;z-index:9999;background:#9a3412;color:#fff;
 font:12px/1.5 ui-sans-serif,system-ui,sans-serif;padding:.4rem .8rem">
  <b>Sample content.</b> %(title)s rendered locally with invented values.
  Modifiers are not applied and %(skipped)d tag(s) could not be filled without
  the CMS. Terminal Four is the only thing that renders the real output.
</div>
%(body)s
"""


def page(title, body, report, stylesheets=()):
    """Wrap a rendering in a page that says what it is."""
    styles = "\n".join('<link rel="stylesheet" href="%s">' % _attr(href)
                       for href in stylesheets)
    return PAGE % {"title": _attr(title), "body": body, "styles": styles,
                   "skipped": len(report.get("skipped") or [])}
