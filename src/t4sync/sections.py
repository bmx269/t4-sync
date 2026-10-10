"""The section tree, and the page layout each section actually uses.

The inspector can usually work out which layout produced a page by aligning
layout source against the published output, but that is inference: where two
layouts are identical it cannot choose between them, and a published layout
name need not name one layout.

T4 knows the answer exactly. Each section carries, per channel:

    channels: [{"id": 13, "pageLayout": 1100982, "inheritedPageLayout": 1100998}]

Getting it costs two requests per section -- one for the children, one for the
section itself -- so this is a separate, opt-in pull rather than part of the
normal one. The result is cached and the walk is resumable.
"""
import json
import urllib.error

CACHE = "sections.json"
CONTENT_CACHE = "sectioncontent.json"


def child_ids(client, section_id, language):
    try:
        data = client.get_json("hierarchy/%s/%s/subsections" % (section_id, language))
    except Exception:
        # Including transport failures: the walk is long and the link to a T4
        # instance is usually a VPN, so one dropped request must not end it.
        return []
    children = (data or {}).get("children") if isinstance(data, dict) else data
    return [(c["id"], c.get("name")) for c in (children or []) if c.get("id") is not None]


def contents(client, section_id, language):
    """Content items placed in a section.

    T4 publishes an anchor for each one, `<span id="d.en.980255">`, so these
    ids can be matched straight back to the published page. That anchor is
    T4's own, not a site convention, which makes this mapping work anywhere.
    """
    try:
        data = client.get_json("hierarchy/%s/%s/contents" % (section_id, language))
    except Exception:
        return []
    children = (data or {}).get("children") if isinstance(data, dict) else data
    found = []
    for child in children or []:
        record = child.get("content") or {}
        if record.get("id") is None:
            continue
        found.append({
            "id": record["id"],
            "name": record.get("name"),
            "contentTypeId": record.get("contentTypeID"),
            "contentTypeName": record.get("contentTypeName"),
            "section": section_id,
        })
    return found


def describe(client, section_id, language):
    data = client.get_json("hierarchy/%s/%s" % (section_id, language))
    if isinstance(data, list):
        data = data[0] if data else None
    return data or {}


def layout_for(record, channel_id=None):
    """The layout a section uses: its own assignment, else the inherited one."""
    for channel in record.get("channels") or []:
        if channel_id is not None and channel.get("id") != channel_id:
            continue
        return channel.get("pageLayout") or channel.get("inheritedPageLayout")
    return None


def walk(client, root_id, language, known=None, limit=None, progress=None,
         with_contents=False, content_sink=None):
    """Breadth-first walk from `root_id`, returning {section_id: record}.

    `known` seeds the result so an interrupted walk can be resumed without
    refetching. Sections already present are not requested again.
    """
    found = dict(known or {})
    queue = [(root_id, None, "")]
    seen = set()
    visited = 0

    while queue:
        # Count what this run does, not what the cache already holds: a limit
        # smaller than the cache would otherwise stop the walk before it
        # started, making a resume silently do nothing.
        if limit and visited >= limit:
            break
        section_id, name, parent_path = queue.pop(0)
        key = str(section_id)
        if key in seen:
            continue
        seen.add(key)
        visited += 1

        if key in found:
            record = found[key]
        else:
            try:
                detail = describe(client, section_id, language)
            except Exception:
                continue
            record = {
                "id": section_id,
                "name": detail.get("name") or name,
                "outputUri": detail.get("output-uri") or detail.get("file-name"),
                "channels": detail.get("channels") or [],
                "parent": detail.get("parent"),
            }
            found[key] = record

        if with_contents and content_sink is not None:
            for item in contents(client, section_id, language):
                content_sink[str(item["id"])] = item

        slug = record.get("outputUri") or record.get("name") or ""
        record["path"] = "%s/%s" % (parent_path, slug) if slug else parent_path
        if progress:
            progress(len(found))

        for child_id, child_name in child_ids(client, section_id, language):
            if str(child_id) not in seen:
                queue.append((child_id, child_name, record["path"]))

    return found


def load(env_dir, name=CACHE):
    path = "%s/_raw/%s" % (env_dir, name)
    try:
        with open(path, encoding="utf-8") as fh:
            return json.load(fh)
    except (OSError, ValueError):
        return {}


class ContentItems:
    """Maps a published `d.en.<id>` anchor to the content item behind it."""

    def __init__(self, items=None):
        self.items = {str(k): v for k, v in (items or {}).items()}

    @classmethod
    def from_env_dir(cls, env_dir):
        return cls(load(env_dir, CONTENT_CACHE))

    @property
    def available(self):
        return bool(self.items)

    def get(self, content_id):
        return self.items.get(str(content_id))


def by_url_path(sections):
    """Map a published URL path to its section record.

    Section paths are rooted at the CMS tree ("/UFV/about"), while published
    URLs are rooted at the channel ("/about/"). The leading component is the
    site root, so both the full path and the path without it are indexed.
    """
    index = {}
    for record in (sections or {}).values():
        path = (record.get("path") or "").strip("/")
        if not path:
            continue
        index.setdefault(path.lower(), record)
        parts = path.split("/")
        if len(parts) > 1:
            index.setdefault("/".join(parts[1:]).lower(), record)
    return index


class SectionLayouts:
    """Resolves a published URL to the page layout T4 assigned it."""

    def __init__(self, sections=None, channel_id=None):
        self.sections = sections or {}
        self.index = by_url_path(self.sections)
        self.channel_id = channel_id

    @classmethod
    def from_env_dir(cls, env_dir, channel_id=None):
        return cls(load(env_dir), channel_id)

    @property
    def available(self):
        return bool(self.index)

    def resolve(self, url_path):
        """Return (layout_id, section) for a URL, or (None, None)."""
        key = (url_path or "/").split("?")[0].strip("/").lower()
        section = self.index.get(key)
        if section is None and key:
            # index.html and friends are not part of the section path.
            trimmed = key.rsplit("/", 1)[0]
            section = self.index.get(trimmed)
        if section is None:
            return None, None
        return layout_for(section, self.channel_id), section


class MediaSources:
    """Maps a published asset URL to the pulled media file behind it.

    Stylesheets and scripts are Media Library items whose content T4 serves
    verbatim, so the file pulled into t4-source/media is the same bytes the
    site publishes. Serving that file instead of the mirrored copy means the
    thing you edit is also the thing `t4 push` deploys -- no second copy in
    overrides/ to keep in step, and no separate manual upload.
    """

    def __init__(self, manifest=None, env_dir=None):
        self.env_dir = env_dir
        self.by_name = {}
        for rel, meta in (manifest or {}).items():
            if meta.get("endpoint") != "media":
                continue
            name = (rel.rsplit("/", 1)[-1] or "").lower()
            if name:
                self.by_name.setdefault(name, rel)

    @classmethod
    def from_env_dir(cls, env_dir):
        import json
        import os
        path = os.path.join(env_dir or "", "_manifest.json")
        try:
            with open(path, encoding="utf-8") as fh:
                return cls(json.load(fh), env_dir)
        except (OSError, ValueError):
            return cls({}, env_dir)

    @property
    def available(self):
        return bool(self.by_name)

    def path_for(self, url_path):
        """Local file for a published asset URL, or None.

        Manifest keys always use "/" -- they are portable identifiers, not
        filesystem paths -- so the result is normalised. Windows tolerates the
        mixed separators that joining produces, but anything comparing the
        path afterwards does not.
        """
        import os
        name = (url_path or "").split("?")[0].rsplit("/", 1)[-1].lower()
        rel = self.by_name.get(name)
        if not rel or not self.env_dir:
            return None
        from .extract import local_path
        full = local_path(self.env_dir, rel)
        return full if os.path.isfile(full) else None
