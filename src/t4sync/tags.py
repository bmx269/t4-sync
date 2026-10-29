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
# Taken from the T4 tag appendix rather than from what this site happens to
# use, because a builder that offers an attribute a tag does not accept
# produces markup that fails at publish time -- which is a slow way to find
# out, and exactly what a builder is for.
#
#   docs.terminalfour.com/documentation/page-management/generate-t4-tags/
#
# `needs` is what the tag cannot work without; `oneof` is where the tag takes
# either of two, such as a meta tag naming a Metadata definition by id or a
# content meta value by name.

TAG_TYPES = {
    "content": {
        "label": "Content element",
        "needs": ["name"],
        "attrs": ["name", "output", "modifiers", "display_field", "date_format",
                  "format", "process-format", "format-modifiers",
                  "enable-dedit", "delimiter", "separator",
                  "textual-name-separator"],
    },
    "navigation": {
        "label": "Navigation object",
        "needs": ["id"],
        "attrs": ["name", "id"],
        "object": "navigation",
    },
    "media": {
        "label": "Media Library item",
        "needs": ["id"],
        "attrs": ["id", "formatter", "max-width", "max-height",
                  "client-resize"],
        "note": "A media tag names a Media Library item by id and a Media "
                "Layout by formatter. It does not take name, output or "
                "modifiers -- those belong to a content tag.",
    },
    "meta": {
        "label": "Metadata",
        "oneof": ["id", "meta"],
        "attrs": ["id", "meta", "display_field"],
        "note": "In a Page Layout a meta tag names a Metadata definition by "
                "id and writes the whole <meta> element. The meta= form is "
                "the Content Type one, which writes just the value.",
    },
    "title": {"label": "Section title", "attrs": [], "needs": []},
    "title-on-index": {"label": "Title on index", "attrs": [], "needs": []},
    "channel": {"label": "Channel", "attrs": ["output", "name"], "needs": []},
    "parentnames": {
        "label": "Ancestor or parent name",
        "attrs": ["start-level", "number-of-levels", "separator",
                  "first-child", "empty-section"],
        "needs": [],
    },
    "edit-page": {"label": "Edit page link", "attrs": ["text"], "needs": []},
    "warning": {"label": "Secondary publish", "attrs": ["text"], "needs": []},
    "lang-var": {
        "label": "Language variable",
        "attrs": ["name", "default-language", "locale"],
        "needs": ["name"],
    },
    "data-obj": {
        "label": "Data object",
        "attrs": ["id", "name", "formatter"],
        "needs": ["id"],
        "object": "list",
    },
    "web-obj": {
        "label": "Web object",
        "attrs": ["id", "name"],
        "needs": ["id"],
        "object": "navigation",
    },
    "sslink": {"label": "Section link", "attrs": ["id", "output"], "needs": ["id"]},
    "fix-url": {"label": "Fix URL", "attrs": ["name"], "needs": []},
    "social": {"label": "Social poster", "attrs": ["id", "name"], "needs": []},
    "exceltohtml": {"label": "Excel to HTML", "attrs": ["name"], "needs": ["name"]},
    "import-url": {"label": "Import URL", "attrs": ["name"], "needs": ["name"]},
}

# Which kinds refer to another pulled object, and to which kind of object.
TAKES_OBJECT = {name: spec["object"]
                for name, spec in TAG_TYPES.items() if "object" in spec}

# Every documented output value. `repeater` and `download` are on the content
# page but not in the appendix, which predates them.
OUTPUTS = ["normal", "selective-output", "repeater", "fulltext", "image",
           "imageurl", "file", "download", "linktext", "linkurl", "id",
           "description", "fav-icon", "site-root", "userinfo",
           "lang_disclosure_style", "lang_disclosure_template"]

# The eight documented modifiers, with what each is for.
MODIFIERS = {
    "htmlentities": "Encode special characters to their HTML equivalent",
    "striptags": "Strip out all HTML tags",
    "medialibrary": "Parse for Media Library t4 tags",
    "nav_sections": "Parse for navigation t4 tags",
    "nl2br": "Change new lines to HTML line breaks (plain text only)",
    "rssentities": "Convert invalid RSS characters to their XML equivalent",
    "js-var": "Escape quotes and newlines for use in JavaScript",
    "encode_emails": "Encode email addresses to ASCII",
}

# Documented as incompatible: rssentities must not be combined with
# striptags.
MODIFIER_CONFLICTS = [({"rssentities", "striptags"},
                       "rssentities must not be combined with striptags")]


# T4's element type codes. Only those that change what a tag should say are
# named; anything else is reported by number rather than guessed at, because
# a wrong label here sends someone to the wrong output value.
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
    # The documented vocabulary, which the mined counts are ranked within.
    documented = {"type": list(TAG_TYPES), "output": OUTPUTS,
                  "modifiers": list(MODIFIERS), "meta": []}
    counts = {key: collections.Counter() for key in documented}
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
    for key, known_values in documented.items():
        ranked = []
        for value, count in counts[key].most_common(limit):
            ranked.append({"value": value, "count": count,
                           # A value this site uses that the documentation
                           # does not list is worth flagging rather than
                           # quietly endorsing.
                           "documented": not known_values or value in known_values})
        seen = {entry["value"] for entry in ranked}
        ranked += [{"value": value, "count": 0, "documented": True}
                   for value in known_values if value not in seen]
        out[key] = ranked
    out["attributes"] = [{"value": v, "count": c}
                         for v, c in counts["attributes"].most_common(limit)]
    return out


def build_tag(spec):
    """Render a tag from a description of one.

    Returns (markup, problems). Only attributes the tag type accepts are
    emitted, and `problems` says what was dropped and what is missing --
    silently producing a tag that fails at publish time is the one outcome
    worth ruling out, because finding it means a publish cycle.

    Attribute order follows T4's own builder: type, name, id, meta, output,
    modifiers, then the rest, so a generated tag sits in a layout looking
    like the ones already there.
    """
    kind = (spec.get("type") or "content").strip()
    rules = TAG_TYPES.get(kind)
    problems = []
    if rules is None:
        return "", ["%r is not a T4 tag type" % kind]

    allowed = set(rules["attrs"])
    given = {}
    for key, value in spec.items():
        if key in ("type", "modifiers"):
            continue
        if isinstance(value, str):
            value = value.strip()
        if value in ("", None, False):
            continue
        if key not in allowed:
            problems.append("a %s tag does not take %s" % (kind, key))
            continue
        given[key] = value

    for required in rules.get("needs", []):
        if required not in given:
            problems.append("a %s tag needs %s" % (kind, required))
    oneof = rules.get("oneof")
    if oneof and not any(key in given for key in oneof):
        problems.append("a %s tag needs one of %s" % (kind, " or ".join(oneof)))

    modifiers = [m.strip() for m in (spec.get("modifiers") or []) if m.strip()]
    if modifiers and "modifiers" not in allowed:
        problems.append("a %s tag does not take modifiers" % kind)
        modifiers = []
    for modifier in modifiers:
        if modifier not in MODIFIERS:
            problems.append("%s is not a documented modifier" % modifier)
    for conflicting, why in MODIFIER_CONFLICTS:
        if conflicting.issubset(set(modifiers)):
            problems.append(why)

    output = given.get("output")
    if output and output not in OUTPUTS:
        problems.append("%s is not a documented output value" % output)

    parts = ['type="%s"' % _attr(kind)]
    for key in ("name", "id", "meta", "output"):
        if key in given:
            parts.append('%s="%s"' % (key, _attr(given.pop(key))))
    if modifiers:
        parts.append('modifiers="%s"' % _attr(",".join(modifiers)))
    for key in sorted(given):
        parts.append('%s="%s"' % (key, _attr(given[key])))

    return "<t4 %s />" % " ".join(parts), problems


def note_for(kind):
    """What is worth knowing about this tag type, or None."""
    return (TAG_TYPES.get(kind) or {}).get("note")


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
        date_format = (spec.get("date_format") or "").strip()
        if date_format:
            return ('{{format-date element="%s" format="%s"}}'
                    % (_attr(name), _attr(date_format))), None
        attrs = ['name="%s"' % _attr(name)]
        if output and output not in ("normal", "selective-output"):
            attrs.append('output="%s"' % _attr(output))
        note = None
        if spec.get("modifiers"):
            note = ("Handlebars has no modifiers attribute. htmlentities is "
                    "the default; for striptags and the rest, do it in the "
                    "layout or keep the T4 tag.")
        return "{{element %s}}" % " ".join(attrs), note

    if kind == "media":
        media_id = (spec.get("id") or "").strip()
        return ('{{media-url id="%s"}}' % _attr(media_id) if media_id else "",
                None if media_id else "Choose a Media Library item first.")

    if kind == "meta":
        meta = (spec.get("meta") or "").strip()
        return ('{{t4-meta name="%s"}}' % _attr(meta) if meta else "",
                "A meta tag by id writes a whole <meta> element, which "
                "Handlebars has no helper for.")

    return "", ("No Handlebars equivalent for a %s tag -- use a T4 tag, in a "
                "Standard layout." % kind)


MIXING_WARNING = ("A layout is either Standard or Handlebars. T4 will not "
                  "evaluate T4 tags inside a Handlebars layout, or Handlebars "
                  "inside a Standard one.")


def _attr(value):
    """Keep a value from breaking out of the attribute it sits in."""
    return str(value).replace('"', "&quot;").replace("<", "&lt;").replace(">", "&gt;")
