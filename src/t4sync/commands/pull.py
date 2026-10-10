"""t4 pull -- fetch layout source from T4 onto disk."""
import json
import os
import re
import urllib.error

from .. import contentlayout
from ..api import Client, describe_http_error
from ..endpoints import ENDPOINTS, ID_FIELDS, MEDIA_TEXT_FIELD
from ..extract import digest, extract, first_field, slug, write_text
from ..progress import Progress


def pull_env(project, env_name, only=None, detail=True, timeout=60,
             quiet=False, force=False, binary=False):
    """Pull one environment. Returns the number of failed endpoints."""
    env = project.env(env_name)
    token = project.require_token(env_name)
    client = Client(env, token, timeout=timeout)

    root = project.env_dir(env_name)
    (root / "_raw").mkdir(parents=True, exist_ok=True)
    manifest = {}
    failures = 0
    kept_total = []

    manifest_path = root / "_manifest.json"
    previous = {}
    if manifest_path.is_file():
        previous = json.loads(manifest_path.read_text(encoding="utf-8"))

    if not quiet:
        print("Pulling %s" % env["label"])

    # `only` may name the specially-handled pulls (media, contentLayout), which
    # are not in ENDPOINTS and must not be requested as `/<name>`.
    for endpoint in [e for e in (only or list(ENDPOINTS)) if e in ENDPOINTS]:
        config = ENDPOINTS.get(endpoint, {})
        path = config.get("path", endpoint)
        try:
            with Progress("  %-13s fetching list" % endpoint, enabled=False if quiet else None):
                data = client.get_json(path)
        except urllib.error.HTTPError as exc:
            print("  %-13s %s" % (endpoint, describe_http_error(exc)))
            failures += 1
            continue
        except Exception as exc:
            print("  %-13s failed: %s" % (endpoint, exc))
            failures += 1
            continue

        write_text(root / "_raw" / ("%s.json" % endpoint),
                   json.dumps(data, indent=2, ensure_ascii=False))

        items = data if isinstance(data, list) else (
            data.get("data") or data.get("items") or data.get("results") or [data])

        if detail and config.get("detail"):
            detailed, missed = [], 0
            with Progress("  %-13s" % endpoint, total=len(items),
                          enabled=False if quiet else None) as bar:
                for item in items:
                    bar.update()
                    item_id = first_field(item, ID_FIELDS)
                    if item_id is None:
                        detailed.append(item)
                        continue
                    try:
                        detailed.append(client.get_json("%s/%s" % (path, item_id)))
                    except Exception:
                        missed += 1
                        detailed.append(item)
            items = detailed
            write_text(root / "_raw" / ("%s.detail.json" % endpoint),
                       json.dumps(items, indent=2, ensure_ascii=False))
            if missed:
                print("  %-13s %d detail fetch(es) failed, used list data"
                      % (endpoint, missed))

        sources, jsons, skipped, kept = extract(
            items, root / endpoint, endpoint, manifest,
            previous=previous, protect=not force)
        kept_total.extend(kept)
        parts = []
        if sources:
            parts.append("%d source" % sources)
        if jsons:
            parts.append("%d json" % jsons)
        if kept:
            parts.append("%d kept (edited locally)" % len(kept))
        if skipped:
            parts.append("%d skipped" % skipped)
        print("  %-13s %d item(s): %s" % (endpoint, len(items), ", ".join(parts) or "nothing"))

    if not only or "media" in only:
        failures += pull_media(client, root, manifest, env, previous,
                               force=force, binary=binary)

    if not only or "contentLayout" in only:
        failures += pull_content_layouts(client, root, manifest, env, previous,
                                         force=force)

    if manifest:
        existing = {}
        if manifest_path.is_file() and only:
            # A partial pull must not discard entries for endpoints it skipped.
            existing = json.loads(manifest_path.read_text(encoding="utf-8"))
            existing = {k: v for k, v in existing.items()
                        if v.get("endpoint") not in (only or [])}
        existing.update(manifest)
        # A file kept because of local edits keeps its OLD hash, so the edit is
        # still recognised as an edit on the next run rather than being adopted.
        for rel in kept_total:
            if rel in previous and rel in existing:
                existing[rel]["sha"] = previous[rel]["sha"]
                existing[rel]["remote_sha"] = manifest[rel]["sha"]
        write_text(manifest_path,
                   json.dumps(existing, indent=2, sort_keys=True) + "\n")

    if kept_total:
        print("\n  %d file(s) kept because they differ from the last pull:"
              % len(kept_total))
        for rel in kept_total[:10]:
            print("    %s" % rel)
        if len(kept_total) > 10:
            print("    ... and %d more" % (len(kept_total) - 10))
        print("  Use `t4 diff` to review, or `t4 pull --force` to discard them.")

    if failures:
        print("  %d endpoint(s) failed." % failures)
    return failures


def prune(outdir, endpoint, manifest, previous, root):
    """Remove files this pull no longer produces.

    Only files the previous pull wrote, and only where the contents still
    match what it wrote: anything edited locally is left alone, because a
    changed file may be work in progress rather than an orphan.
    """
    from ..extract import locally_modified

    wanted = {rel for rel in manifest if rel.split("/", 1)[0] == endpoint}
    removed = 0
    for path in sorted(outdir.glob("*")):
        if not path.is_file():
            continue
        rel = "%s/%s" % (endpoint, path.name)
        if rel in wanted:
            continue
        prior = previous.get(rel, {}).get("sha")
        if not prior:
            continue                      # not ours; leave it
        if locally_modified(path, prior):
            continue                      # edited since; leave it
        try:
            path.unlink()
            removed += 1
        except OSError:
            pass
    return removed


def media_filename(name, media_id):
    """A safe filename for a media item, keeping its extension."""
    base = os.path.basename(str(name or "").strip()) or str(media_id)
    base = re.sub(r"[^\w.\- ]", "", base).strip().replace(" ", "-")
    base = base.lstrip(".") or str(media_id)
    if "." not in base:
        base += ".txt"
    return base


def save_binary(client, record, root, language):
    """Save a binary media item's bytes. Returns 1 if written.

    Opt-in: images and fonts are already visible locally through the site
    mirror, so the only thing this adds is being able to deploy a changed one,
    and a site's media library can be very large.
    """
    media_id = record.get("id")
    version = record.get("version") or "1.0"
    name = record.get("fileName") or record.get("name")
    if media_id is None or not name:
        return 0
    target = root / "media-binary" / media_filename(name, media_id)
    if target.is_file() and target.stat().st_size == (record.get("mediaSize") or -1):
        return 0                       # already have these exact bytes
    try:
        data = client.get_bytes("media/%s/%s/%s/Media" % (media_id, language, version))
    except Exception:
        return 0
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_bytes(data)
    return 1


def pull_media(client, root, manifest, env, previous, force=False, binary=False):
    """Text media items -- the code snippets T4 sites keep in the Media Library.

    Page layouts pull these in by id, so their markup is part of the site's
    source even though it lives under Media rather than Layouts. Binary media
    is deliberately skipped; see endpoints.py.
    """
    language = env.get("language", "en")
    # Scan the parsed records rather than the raw file: inside JSON every
    # quote is escaped, so a pattern written for markup matches nothing.
    media_tag = re.compile(r'type=["\']media["\'][^>]*?\bid=["\'](\d+)["\']', re.I)

    def strings(value):
        if isinstance(value, str):
            yield value
        elif isinstance(value, dict):
            for item in value.values():
                for found in strings(item):
                    yield found
        elif isinstance(value, list):
            for item in value:
                for found in strings(item):
                    yield found

    ids = set()
    for name in ("pageLayout.detail.json", "contentLayout.json"):
        path = root / "_raw" / name
        if not path.is_file():
            continue
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
        for text in strings(data):
            ids.update(media_tag.findall(text))

    if not ids:
        print("  %-13s none referenced by the pulled layouts" % "media")
        return 0

    from ..extract import locally_modified
    from ..progress import Progress

    outdir = root / "media"
    outdir.mkdir(parents=True, exist_ok=True)
    records, written, kept, failures = [], 0, 0, 0
    binary_saved = 0
    bar = Progress("  media", total=len(ids))
    try:
        for media_id in sorted(ids, key=int):
            bar.update()
            try:
                record = client.get_json("media/%s/%s" % (media_id, language))
            except Exception:
                failures += 1
                continue
            if isinstance(record, list):
                record = record[0] if record else None
            if not record:
                continue
            text = record.get(MEDIA_TEXT_FIELD)
            if not isinstance(text, str) or not text.strip():
                if binary:
                    binary_saved += save_binary(client, record, root, language)
                continue
            records.append(record)

            # The media item's own name carries its extension (v8.css,
            # app.js), so use it rather than slugging it into a .html file --
            # the extension is what gives an editor its syntax highlighting,
            # and what makes the file recognisable as the asset it is.
            rel = "media/%s" % media_filename(record.get("name"), media_id)
            target = root / rel
            prior = previous.get(rel, {}).get("sha")
            if not force and prior and locally_modified(target, prior):
                kept += 1
            else:
                write_text(target, text)
                written += 1
            manifest[rel] = {
                "endpoint": "media",
                "id": int(media_id),
                "path": "media/%s/%s" % (media_id, language),
                "field": MEDIA_TEXT_FIELD,
                "name": record.get("name"),
                "sha": digest(text),
            }
    finally:
        bar.close()

    removed = prune(outdir, "media", manifest, previous, root)

    write_text(root / "_raw" / "media.json",
               json.dumps(records, indent=2, ensure_ascii=False))
    parts = ["%d source" % written]
    if kept:
        parts.append("%d kept (edited locally)" % kept)
    if binary_saved:
        parts.append("%d binary" % binary_saved)
    if removed:
        parts.append("%d stale removed" % removed)
    if failures:
        parts.append("%d fetch failure(s)" % failures)
    print("  %-13s %d referenced, %d text: %s"
          % ("media", len(ids), len(records), ", ".join(parts)))
    return 0


def pull_content_layouts(client, root, manifest, env, previous, force=False):
    """Content Layouts, which are reached through their content type.

    Not a top-level resource: see contentlayout.py for the paths and why they
    are easy to miss.
    """
    language = env.get("language", "en")
    raw = root / "_raw" / "contenttype.json"
    if not raw.is_file():
        print("  %-13s skipped (needs contenttype; run a full pull)" % "contentLayout")
        return 0

    try:
        content_types = json.loads(raw.read_text(encoding="utf-8"))
    except ValueError:
        return 1
    if isinstance(content_types, dict):
        content_types = content_types.get("data") or []

    records, failures = contentlayout.collect(
        client, content_types, language,
        progress_label="  contentLayout")
    write_text(root / "_raw" / "contentLayout.json",
               json.dumps(records, indent=2, ensure_ascii=False))

    outdir = root / "contentLayout"
    outdir.mkdir(parents=True, exist_ok=True)
    written = kept = 0
    seen = {}

    for record in records:
        key = contentlayout.format_key(record)
        if not key:
            continue
        markup = record["elements"].get(key)
        if not isinstance(markup, str) or not markup.strip():
            continue

        base = "%s--%s" % (slug(record.get("_contentTypeName"), record.get("_contentTypeId")),
                           slug(contentlayout.layout_name(record), record["id"]))
        seen[base] = seen.get(base, 0) + 1
        if seen[base] > 1:
            base = "%s-%d" % (base, seen[base])

        rel = "contentLayout/%s.html" % base
        target = root / rel
        prior = previous.get(rel, {}).get("sha")
        from ..extract import locally_modified
        if not force and prior and locally_modified(target, prior):
            kept += 1
        else:
            write_text(target, markup)
            written += 1

        manifest[rel] = {
            "endpoint": "layout",
            "id": record["id"],
            "path": "layout/%s/%s" % (record["id"], language),
            "field": "elements.%s" % key,
            "name": contentlayout.layout_name(record),
            "contentType": record.get("_contentTypeName"),
            "sha": digest(markup),
        }

    parts = ["%d source" % written]
    if kept:
        parts.append("%d kept (edited locally)" % kept)
    if failures:
        parts.append("%d fetch failure(s)" % failures)
    print("  %-13s %d layout(s): %s" % ("contentLayout", len(records), ", ".join(parts)))
    return 0


def run(args, project):
    names = project.env_names() if args.all else [args.env or project.default_env()]
    failures = 0
    for index, name in enumerate(names):
        if index:
            print()
        if args.all and not project.read_token(name):
            print("Skipping %s: no token stored (t4 token set %s)" % (name, name))
            continue
        failures += pull_env(project, name, only=args.only,
                             detail=not args.no_detail, timeout=args.timeout,
                             force=args.force, binary=args.binary)
    return 1 if failures else 0
