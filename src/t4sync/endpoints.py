"""What to pull, and where the source lives inside each record.

Endpoint names here are VERIFIED against a live T4 instance and are
case-sensitive and inconsistent: /pageLayout is camelCase, /contenttype is not.
A wrong case returns 500 -- the same response as an unrouted path -- so a typo
is indistinguishable from "this endpoint does not exist". Do not tidy them.
"""

ENDPOINTS = {
    # detail=True: the collection response carries metadata only, so each item
    # must be fetched by id to get its source.
    "pageLayout":  {"detail": True},
    "navigation":  {"detail": True},
    "contenttype": {"detail": False},
    "channel":     {"detail": True},
    # /list alone returns 500. /list/<anything> returns ALL lists -- the segment
    # is required but ignored, and is not an id.
    "list":        {"detail": False, "path": "list/1"},
    # The whole section tree in one response.
    "sitestructure": {"detail": False},
}

# Media items also need a language segment: /media/{id}/{language}. Text-based
# media -- the code snippets T4 sites keep in the Media Library -- carry their
# content in a `text` field, so they are source worth versioning. Binary media
# (stylesheets, scripts, images) are not fetched: their bytes live behind
# /media/{id}/{language}/{version}/{element} and belong in the site mirror
# rather than here.
MEDIA_TEXT_FIELD = "text"

# Content Layouts are pulled separately: they hang off a content type and need
# a language segment, so they do not fit the endpoint/id shape above. See
# contentlayout.py.

# Page layouts split their source across three fields rather than one "code".
SOURCE_FIELDS = [
    "headerCode", "footerCode", "stylesheetCode",
    "code", "content", "source", "template", "html",
    "pageLayoutCode", "layoutCode", "body",
]

# Suffix and forced extension per source field, so filenames say what they are.
FIELD_FILE = {
    "headerCode": ("header", None),
    "footerCode": ("footer", None),
    "stylesheetCode": ("stylesheet", "css"),
}

NAME_FIELDS = ["name", "title", "layoutName", "displayName", "description"]
ID_FIELDS = ["id", "layoutID", "pageLayoutID", "contentTypeID", "navigationID"]

LAYOUT_PROCESSORS = {1: "legacy", 13: "handlebars"}
