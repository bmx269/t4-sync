"""Turn API records into files on disk, and record how to get back."""
import hashlib
import os
import json
import pathlib
import re

from .endpoints import FIELD_FILE, ID_FIELDS, NAME_FIELDS, SOURCE_FIELDS


def first_field(item, candidates):
    for key in candidates:
        if isinstance(item, dict) and item.get(key) not in (None, ""):
            return item[key]
    return None


def slug(value, fallback):
    text = re.sub(r"[^\w\s.-]", "", str(value if value not in (None, "") else fallback))
    text = re.sub(r"[\s_]+", "-", text.strip())
    return (text[:120] or str(fallback)).lower()


def guess_extension(source):
    head = source.lstrip()[:400]
    if "{{" in source and "<t4" not in source:
        return "hbs"
    if re.search(r"\b(importPackage|document\.write)", source):
        return "js"
    if head.startswith("<") or "<t4" in source:
        return "html"
    return "txt"


def local_path(base, rel):
    """Join a "/"-separated identifier to a filesystem path.

    Manifest keys and URL paths always use "/" -- they are portable
    identifiers, not filesystem paths. Python will open the mixed-separator
    result on Windows, so this is not about making IO work; it is so the path
    can be compared, printed and stored consistently.
    """
    return os.path.normpath(os.path.join(str(base), *str(rel).split("/")))


def write_text(path, value):
    """Write without newline translation.

    T4 stores CRLF. Python's text mode would rewrite it, which makes every file
    look modified on the next comparison and would push back mangled endings.
    """
    with open(path, "w", encoding="utf-8", newline="") as fh:
        fh.write(value)


def read_text(path):
    with open(path, encoding="utf-8", newline="") as fh:
        return fh.read()


def digest(value):
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def locally_modified(path, recorded_sha):
    """True if the file on disk differs from what was last pulled.

    Comparing against the recorded hash is what distinguishes "the user edited
    this" from "the remote moved on". Without it a pull cannot tell the two
    apart and would silently overwrite local work.
    """
    try:
        return digest(read_text(path)) != recorded_sha
    except OSError:
        return False


def extract(items, outdir, endpoint, manifest, previous=None, protect=False):
    """Write one file per source field per item.

    Records with no source string -- content types, navigation configuration --
    are written as pretty JSON instead; the record itself is the artifact.

    With protect=True, a file whose contents no longer match the hash recorded
    by the previous pull is left alone and reported, so local edits survive.
    Returns (source_files, json_files, skipped, kept).
    """
    previous = previous or {}
    kept = []
    outdir = pathlib.Path(outdir)
    outdir.mkdir(parents=True, exist_ok=True)
    sources = jsons = skipped = 0
    seen = {}

    for index, item in enumerate(items):
        if not isinstance(item, dict):
            skipped += 1
            continue

        base = slug(first_field(item, NAME_FIELDS), first_field(item, ID_FIELDS) or index)
        seen[base] = seen.get(base, 0) + 1
        if seen[base] > 1:                      # names are not unique in T4
            base = "%s-%d" % (base, seen[base])

        wrote = False
        for field in SOURCE_FIELDS:
            value = item.get(field)
            if not isinstance(value, str) or not value.strip():
                continue
            suffix, forced = FIELD_FILE.get(field, (None, None))
            ext = forced or guess_extension(value)
            name = "%s.%s.%s" % (base, suffix, ext) if suffix else "%s.%s" % (base, ext)
            rel = "%s/%s" % (endpoint, name)
            target = outdir / name
            prior = previous.get(rel, {}).get("sha")

            if protect and prior and locally_modified(target, prior):
                kept.append(rel)
            else:
                write_text(target, value)
                sources += 1

            manifest[rel] = {
                "endpoint": endpoint,
                "id": first_field(item, ID_FIELDS),
                "field": field,
                "name": first_field(item, NAME_FIELDS),
                "sha": digest(value),
            }
            wrote = True

        if not wrote:
            # sort_keys keeps diffs stable when T4 reorders fields between runs.
            write_text(outdir / ("%s.json" % base),
                       json.dumps(item, indent=2, sort_keys=True, ensure_ascii=False) + "\n")
            jsons += 1

    return sources, jsons, skipped, kept
