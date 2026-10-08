"""t4 pull -- fetch layout source from T4 onto disk."""
import json
import urllib.error

from ..api import Client, describe_http_error
from ..endpoints import ENDPOINTS, ID_FIELDS
from ..extract import extract, first_field, write_text


def pull_env(project, env_name, only=None, detail=True, timeout=60,
             quiet=False, force=False):
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

    for endpoint in (only or list(ENDPOINTS)):
        config = ENDPOINTS.get(endpoint, {})
        path = config.get("path", endpoint)
        try:
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
            for item in items:
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
                             force=args.force)
    return 1 if failures else 0
