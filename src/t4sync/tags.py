"""Compose T4 tags and Handlebars expressions from the project's own data.

Writing a T4 tag by hand means remembering an element's exact name, which
`output` value that element type takes, and which modifiers the site tends to
apply. Getting any of them wrong fails at publish time, which is a slow way to
find a typo. T4's own UI has a tag builder for this; outside the CMS there was
nothing.

Everything offered here comes from what has been pulled, not from a list
written into this file:

  elements      the Content Type records, which carry every field with its
                name, alias and type
  vocabulary    mined from the project's own layouts -- 4,627 tags on the
                site this was built against -- so the suggestions are what
                this site actually does, ordered by how often it does it

A documented floor is merged underneath, so a project that has pulled layouts
but written none still offers the standard values. Anything mined that is not
in the floor is kept and marked, because a site's own conventions are worth
more than a general list.

**Nothing here evaluates a tag.** Composing one is a text problem; rendering
one is T4's publish engine against the CMS database, which does not exist
locally. See "What this does not do" in the README.
"""
import collections
import json
import os
import re

TAG_RE = re.compile(r"<t4\b([^>]*?)/?>", re.I)
ATTR_RE = re.compile(r'([\w-]+)\s*=\s*"([^"]*)"')

# The documented values, used as a floor under whatever the project's own
# layouts turn out to use. Kept short on purpose: this is a fallback, and a
# long invented list would drown the evidence.
# Which tag kinds refer to another object by id. A title or meta tag takes
# none, and offering a picker for one invites a tag that cannot work.
TAKES_OBJECT = {"navigation": "navigation", "list": "list",
                "web_object": "navigation", "data-obj": "list"}

DOCUMENTED = {
    "type": ["content", "navigation", "media", "meta", "title", "channel",
             "web_object", "data-obj"],
    "output": ["normal", "selective-output", "linkurl", "fulltext", "file",
               "image", "name", "linktext"],
    "modifiers": ["htmlentities", "striptags", "nl2br", "medialibrary",
                  "tags", "js-string", "js-var", "encode_emails"],
    "meta": ["name", "description", "keywords", "content_id", "html_anchor",
             "last_modified", "version"],
}

# T4's element type codes. Only the ones that change what a tag should say
# are listed; anything else is reported by number rather than guessed at.
ELEMENT_TYPES = {
    1: "plain text",
    2: "HTML",
    3: "image",
    4: "file",
    5: "list",
    6: "date",
    9: "hidden",
}


def element_type(code):
    return ELEMENT_TYPES.get(code, "type %s" % code)


def content_types(env_dir):
    """Every pulled Content Type, with its elements.

    The elements are the whole point: a tag names one by its exact label, and
    a list typed from memory is where the typos come from.
    """
    directory = os.path.join(env_dir or "", "contenttype")
    found = []
    if not os.path.isdir(directory):
        return found
    for filename in sorted(os.listdir(directory)):
        if not filename.endswith(".json"):
            continue
        try:
            with open(os.path.join(directory, filename), encoding="utf-8") as fh:
                record = json.load(fh)
        except (OSError, ValueError):
            continue
        elements = []
        for element in record.get("contentTypeElements") or []:
            name = element.get("name")
            if not name:
                continue
            elements.append({
                "name": name,
                "alias": element.get("alias") or name,
                "type": element_type(element.get("type")),
            })
        if not elements:
            continue
        found.append({
            "id": record.get("id"),
            "name": record.get("name") or filename,
            "alias": record.get("alias"),
            "elements": elements,
        })
    return found


def named_objects(env_dir, endpoint):
    """Navigation objects, lists and the like: things a tag refers to by id."""
    directory = os.path.join(env_dir or "", endpoint)
    found = []
    if not os.path.isdir(directory):
        return found
    for filename in sorted(os.listdir(directory)):
        if not filename.endswith(".json"):
            continue
        try:
            with open(os.path.join(directory, filename), encoding="utf-8") as fh:
                record = json.load(fh)
        except (OSError, ValueError):
            continue
        if isinstance(record, list):
            record = record[0] if record else {}
        if record.get("id") is None:
            continue
        # Half the Lists on a real instance come back with no name at all.
        # Falling back to the filename showed "1.json" in a picker, which
        # tells you nothing; say plainly that T4 did not name it.
        name = (record.get("name") or record.get("title")
                or record.get("description") or "").strip()
        found.append({"id": record["id"],
                      "name": name or "(unnamed %s)" % endpoint})
    return found


def vocabulary(env_dir, limit=14):
    """What this site's own layouts actually use, commonest first.

    Reading the layouts rather than asking T4: the answer is on disk already,
    it costs no request, and a value that appears four hundred times in this
    project is a better suggestion than one that appears in the manual.
    """
    counts = {key: collections.Counter() for key in DOCUMENTED}
    counts["attributes"] = collections.Counter()
    seen = 0

    for folder in ("contentLayout", "pageLayout"):
        directory = os.path.join(env_dir or "", folder)
        if not os.path.isdir(directory):
            continue
        for filename in os.listdir(directory):
            if not filename.endswith((".html", ".hbs", ".txt")):
                continue
            try:
                with open(os.path.join(directory, filename), encoding="utf-8",
                          errors="replace", newline="") as fh:
                    text = fh.read()
            except OSError:
                continue
            for match in TAG_RE.finditer(text):
                attrs = dict(ATTR_RE.findall(match.group(1)))
                seen += 1
                for key in attrs:
                    counts["attributes"][key] += 1
                for key in ("type", "output", "meta"):
                    if attrs.get(key):
                        counts[key][attrs[key]] += 1
                for modifier in (attrs.get("modifiers") or "").split(","):
                    modifier = modifier.strip()
                    if modifier:
                        counts["modifiers"][modifier] += 1

    out = {"tags_seen": seen}
    for key, documented in DOCUMENTED.items():
        ranked = [{"value": value, "count": count}
                  for value, count in counts[key].most_common(limit)]
        known = {entry["value"] for entry in ranked}
        # The documented values the site does not use yet, after the ones it
        # does: still valid, just not this site's habit.
        ranked += [{"value": value, "count": 0}
                   for value in documented if value not in known]
        out[key] = ranked
    out["attributes"] = [{"value": v, "count": c}
                         for v, c in counts["attributes"].most_common(limit)]
    return out


def build_tag(spec):
    """Render a tag from a description of one. Returns the markup.

    Attribute order follows what T4's own builder emits -- type, name, id,
    output, modifiers, then the rest -- so a generated tag sits in a layout
    looking like the ones already there.
    """
    kind = (spec.get("type") or "content").strip()
    parts = ['type="%s"' % _attr(kind)]

    for key in ("name", "id", "meta", "output"):
        value = (spec.get(key) or "").strip()
        if value:
            parts.append('%s="%s"' % (key, _attr(value)))

    modifiers = [m.strip() for m in (spec.get("modifiers") or []) if m.strip()]
    if modifiers:
        parts.append('modifiers="%s"' % _attr(",".join(modifiers)))

    for key in ("format", "date_format", "formatter", "display_field"):
        value = (spec.get(key) or "").strip()
        if value:
            parts.append('%s="%s"' % (key, _attr(value)))

    if spec.get("cdn"):
        parts.append('cdn="true"')
    pxl = (spec.get("pxl_filter_id") or "").strip()
    if pxl:
        parts.append('pxl-filter-id="%s"' % _attr(pxl))

    return "<t4 %s />" % " ".join(parts)


def build_handlebars(spec):
    """The Handlebars equivalent, where there is one.

    Returns (markup, note). `note` is set when the two forms are not
    equivalent, because silently emitting something that does a different
    thing is worse than saying so.
    """
    kind = (spec.get("type") or "content").strip()
    name = (spec.get("name") or "").strip()
    output = (spec.get("output") or "").strip()

    if kind == "content":
        if not name:
            return "", "Pick an element first."
        attrs = ['name="%s"' % _attr(name)]
        if output and output not in ("normal", "selective-output"):
            attrs.append('output="%s"' % _attr(output))
        helper = "{{element %s}}" % " ".join(attrs)
        note = None
        if spec.get("modifiers"):
            note = ("Handlebars has no modifiers attribute. "
                    "`htmlentities` is the default; for `striptags` and the "
                    "rest, do it in the layout or keep the T4 tag.")
        date_format = (spec.get("date_format") or "").strip()
        if date_format:
            helper = '{{format-date element="%s" format="%s"}}' % (
                _attr(name), _attr(date_format))
            note = None
        return helper, note

    if kind == "media":
        return ('{{media-url name="%s"}}' % _attr(name) if name else "",
                None if name else "Pick an element first.")

    if kind == "meta":
        meta = (spec.get("meta") or "").strip()
        return ('{{t4-meta name="%s"}}' % _attr(meta) if meta else "",
                None if meta else "Choose a meta value.")

    return "", ("No Handlebars equivalent for a %s tag -- use a T4 tag, in a "
                "Standard layout." % kind)


MIXING_WARNING = ("A layout is either Standard or Handlebars. T4 will not "
                  "evaluate T4 tags inside a Handlebars layout, or Handlebars "
                  "inside a Standard one.")


def _attr(value):
    """Keep a value from breaking out of the attribute it sits in."""
    return str(value).replace('"', "&quot;").replace("<", "&lt;").replace(">", "&gt;")
