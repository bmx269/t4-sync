"""t4 status -- one view of everything pending, across both file sets.

A T4 project is edited in two places that behave differently:

  overrides/   published assets (CSS, JS, images). These live in the T4 Media
               Library. The REST API exposes no media endpoint, so they are
               deployed by uploading through the T4 UI -- this tool can show
               you what changed but cannot push it.

  t4-source/   layout source (page layouts, content types, navigation).
               Deployed with `t4 push`.

Showing them together is the point: otherwise it is easy to push layouts and
forget the stylesheet they depend on, or the reverse.
"""
import urllib.error

from ..api import Client
from ..compare import compare, load_manifest
from ..errors import T4Error
from .edit import overrides_root


def run(args, project):
    env_name = args.env or project.default_env()
    env = project.env(env_name)
    print("Project     %s" % project.root)
    print("Environment %s\n" % env["label"])

    # -- published assets --------------------------------------------------
    overrides = overrides_root(project)
    files = sorted(p for p in overrides.rglob("*") if p.is_file()) if overrides.is_dir() else []
    print("Published assets (overrides/)")
    if not files:
        print("  nothing overridden")
    else:
        for path in files:
            print("  M %s" % path.relative_to(overrides))
        print("\n  %d file(s). These are Media Library items: the REST API has no"
              % len(files))
        print("  media endpoint, so upload them through the T4 UI")
        print("  (Content > Media Library). `t4 push` cannot deploy them.")

    # -- layout source -----------------------------------------------------
    print("\nLayout source (t4-source/%s/)" % env_name)
    try:
        manifest = load_manifest(project, env_name)
    except T4Error:
        print("  not pulled yet - run: t4 pull --env %s" % env_name)
        return 0

    if args.offline:
        print("  %d file(s) tracked. Run without --offline to compare against T4."
              % len(manifest))
        return 0

    try:
        client = Client(env, project.require_token(env_name), timeout=args.timeout)
        results = compare(project, env_name, client, manifest, args.paths or None)
    except urllib.error.HTTPError as exc:
        print("  could not compare: HTTP %s" % exc.code)
        return 1
    except Exception as exc:
        print("  could not compare: %s" % type(exc).__name__)
        print("  (a T4 instance is usually behind a VPN - check it is connected)")
        return 1

    changed = [e for e in results if e["state"] == "changed"]
    missing = [e for e in results if e["state"] == "local-missing"]
    errors = [e for e in results if e["state"] == "error"]

    if not changed and not missing and not errors:
        print("  %d file(s), all identical to T4" % len(results))
    else:
        for entry in changed:
            delta = len(entry["local"] or "") - len(entry["remote"] or "")
            print("  M %-56s %+d bytes" % (entry["path"], delta))
        for entry in missing:
            print("  ? %-56s in T4, not on disk" % entry["path"])
        for entry in errors:
            print("  ! %-56s %s" % (entry["path"], entry.get("error", "")))
        print("\n  %d changed, %d identical" % (changed and len(changed) or 0,
                                                len(results) - len(changed) - len(missing)))

    print()
    if changed:
        writable = env.get("push_allowed")
        print("Next:  t4 diff --show        review the changes")
        if writable:
            print("       t4 push              deploy them, one confirmation each")
        else:
            print("       push is disabled for %s - set \"push_allowed\": true in"
                  % env_name)
            print("       .t4/config.json if this environment should be writable")
    if files:
        print("       upload the overrides above via the T4 UI")
    return 0
