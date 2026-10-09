"""t4 mirror / t4 serve -- run a local copy of the published site."""
import pathlib

from .. import mirror as mirror_mod
from .. import serve as serve_mod
from ..errors import T4Error

DEFAULT_REJECT = ["pdf", "docx", "xlsx", "pptx", "doc", "xls", "ppt"]


def site_root(project):
    return project.root / project.config.get("mirror_dir", "site-mirror")


def run_mirror(args, project):
    env_name = args.env or project.default_env()
    env = project.env(env_name)
    url = args.url or env.get("published_url")
    if not url:
        raise T4Error(
            "No published URL for %r.\n"
            "Add one to .t4/config.json as \"published_url\", or pass a URL:\n"
            "  t4 mirror https://webtest.example.edu/" % env_name)

    host = url.split("//", 1)[-1].split("/", 1)[0]
    dest = site_root(project) / host
    reject = DEFAULT_REJECT if not args.include_documents else []

    print("Mirroring %s -> %s" % (url, dest))
    if reject:
        print("Skipping documents: %s" % ", ".join(reject))
    pages, assets, errors = mirror_mod.crawl(
        url, dest, reject_ext=reject, limit=args.limit, timeout=args.timeout)
    print("\n%d pages, %d assets, %d errors" % (pages, assets, errors))
    print("Serve it: t4 serve")
    return 0


def run_serve(args, project):
    root = site_root(project)
    if args.host_dir:
        site = root / args.host_dir
    else:
        candidates = sorted(p for p in root.glob("*") if p.is_dir()) if root.is_dir() else []
        if not candidates:
            raise T4Error("No mirror found in %s\nRun `t4 mirror` first." % root)
        if len(candidates) > 1 and not args.host_dir:
            print("Several mirrors present; using %s" % candidates[0].name)
            print("Pick another with: t4 serve <host>")
        site = candidates[0]

    if not site.is_dir():
        raise T4Error("No mirror at %s\nRun `t4 mirror` first." % site)

    overrides = project.root / project.config.get("overrides_dir", "overrides")
    overrides.mkdir(parents=True, exist_ok=True)
    rules = project.root / ".t4" / "rewrites.conf"
    if not rules.is_file():
        rules.parent.mkdir(parents=True, exist_ok=True)
        rules.write_text(serve_mod.DEFAULT_RULES, encoding="utf-8")

    # An explicit --proxy wins; otherwise fall back to the environment's
    # published URL, which is almost always the right origin.
    origin = args.proxy
    if origin is None and not args.no_proxy:
        try:
            env = project.env(args.env or project.default_env())
            origin = env.get("published_url")
        except T4Error:
            origin = None
    if args.no_proxy:
        origin = None

    serve_mod.serve(str(site), overrides=str(overrides),
                    rules_file=str(rules), port=args.port,
                    proxy_origin=origin, proxy_cache=not args.no_proxy_cache,
                    reload=not args.no_reload)
    return 0
