"""Compare local files against what T4 currently holds."""
import difflib
import json
import pathlib
import urllib.error

from .api import describe_http_error
from .extract import read_text
from .progress import Progress

MANIFEST = "_manifest.json"


def load_manifest(project, env_name):
    path = project.env_dir(env_name) / MANIFEST
    if not path.is_file():
        from .errors import T4Error
        raise T4Error("No manifest at %s\nRun: t4 pull --env %s" % (path, env_name))
    return json.loads(path.read_text(encoding="utf-8"))


def read_field(record, field):
    """Read a field that may be nested, e.g. 'elements.formatcode#2:1'.

    Content layouts keep their markup inside `elements`, so the manifest must
    be able to name a path rather than a single key.
    """
    value = record or {}
    for part in field.split("."):
        if not isinstance(value, dict):
            return None
        value = value.get(part)
    return value


def write_field(record, field, new_value):
    """Set a possibly-nested field, returning the modified record."""
    parts = field.split(".")
    target = record
    for part in parts[:-1]:
        nxt = dict(target.get(part) or {})
        target[part] = nxt
        target = nxt
    target[parts[-1]] = new_value
    return record


def remote_path(meta):
    """Where this item lives.

    `path` wins, because not every resource is `endpoint/id`: content layouts
    need a trailing language segment.
    """
    return meta.get("path") or "%s/%s" % (meta["endpoint"], meta["id"])


def matches(rel, selectors):
    if not selectors:
        return True
    for sel in selectors:
        sel = sel.replace("\\", "/").strip("/")
        if rel == sel or rel.startswith(sel + "/") or rel.endswith("/" + sel):
            return True
    return False


def compare(project, env_name, client, manifest, selectors=None,
            progress_label=None):
    """Return one entry per manifest item.

    state is 'same', 'changed', 'local-missing' or 'error'. Each item costs a
    request, so pass `progress_label` to show a bar while it runs.
    """
    selected = [(rel, meta) for rel, meta in sorted(manifest.items())
                if matches(rel, selectors)]
    bar = Progress(progress_label, total=len(selected),
                   enabled=None if progress_label else False)
    try:
        return _compare(project, env_name, client, selected, bar)
    finally:
        bar.close()


def _compare(project, env_name, client, selected, bar):
    root = project.env_dir(env_name)
    results = []
    for rel, meta in selected:
        bar.update()
        entry = {"path": rel, "meta": meta, "local": None, "remote": None}
        local_path = pathlib.Path(root) / rel
        if not local_path.is_file():
            entry["state"] = "local-missing"
            results.append(entry)
            continue
        entry["local"] = read_text(local_path)
        try:
            record = client.fetch_path(remote_path(meta))
        except urllib.error.HTTPError as exc:
            entry.update(state="error", error=describe_http_error(exc))
            results.append(entry)
            continue
        except Exception as exc:
            entry.update(state="error", error=str(exc))
            results.append(entry)
            continue
        entry["record"] = record
        entry["remote"] = read_field(record, meta["field"]) or ""
        entry["state"] = "same" if entry["remote"] == entry["local"] else "changed"
        results.append(entry)
    return results


def unified(entry, from_label=None, to_label=None):
    return difflib.unified_diff(
        (entry["remote"] or "").splitlines(keepends=True),
        (entry["local"] or "").splitlines(keepends=True),
        fromfile=from_label or ("T4/%s" % entry["path"]),
        tofile=to_label or ("local/%s" % entry["path"]),
        n=3,
    )
