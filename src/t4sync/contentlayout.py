"""Content Layouts — the HTML templates attached to a Content Type.

These are not a top-level REST resource and took some finding. The paths are:

    GET /layout/contenttype/{contentTypeId}/{language}   layouts for one type
    GET /layout/{layoutId}/{language}                    one layout, with markup
    PUT /layout/{layoutId}/{language}                    write it back

The `{language}` segment is required and is what makes these look absent: omit
it and the request falls through to a 500, which on this API is also what an
unrouted path returns.

The markup is not a top-level field. It sits in `elements` under a key whose
suffix encodes the element's id and type, e.g. `formatcode#2:1`, so the key is
matched by prefix rather than assumed.
"""
import urllib.error

FORMAT_FIELD = "formatcode"
NAME_FIELD = "name"


def format_key(record):
    """The `elements` key holding the markup, or None."""
    for key in (record or {}).get("elements", {}):
        if key.split("#", 1)[0] == FORMAT_FIELD:
            return key
    return None


def layout_name(record):
    elements = (record or {}).get("elements", {})
    for key, value in elements.items():
        if key.split("#", 1)[0] == NAME_FIELD and isinstance(value, str) and value.strip():
            return value.strip()
    return record.get("name") or str(record.get("id"))


def list_for_type(client, content_type_id, language):
    """Layout stubs for one content type. Returns [] when it has none."""
    try:
        data = client.get_json("layout/contenttype/%s/%s" % (content_type_id, language))
    except urllib.error.HTTPError as exc:
        if exc.code in (404, 403):
            return []
        raise
    if isinstance(data, dict):
        data = data.get("data") or data.get("items") or [data]
    return [d for d in (data or []) if isinstance(d, dict) and d.get("id") is not None]


def fetch(client, layout_id, language):
    data = client.get_json("layout/%s/%s" % (layout_id, language))
    if isinstance(data, list):
        data = data[0] if data else None
    return data


def collect(client, content_types, language, progress_label=None):
    """Fetch every content layout across the given content types.

    One request per content type, plus one per layout found, so this is slow
    over a VPN and needs to say so while it runs.

    Returns (records, failures). Each record carries the content type's name so
    the files can be grouped by the component they belong to.
    """
    from .progress import Progress

    records, failures = [], 0
    bar = Progress(progress_label, total=len(content_types),
                   enabled=None if progress_label else False)
    try:
        for content_type in content_types:
            bar.update()
            type_id = content_type.get("id")
            if type_id is None:
                continue
            try:
                stubs = list_for_type(client, type_id, language)
            except Exception:
                failures += 1
                continue
            for stub in stubs:
                try:
                    record = fetch(client, stub["id"], language)
                except Exception:
                    failures += 1
                    continue
                if not record:
                    continue
                record["_contentTypeName"] = content_type.get("name")
                record["_contentTypeId"] = type_id
                records.append(record)
    finally:
        bar.close()
    return records, failures
