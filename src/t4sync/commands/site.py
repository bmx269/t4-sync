"""t4 mirror / t4 serve -- run a local copy of the published site."""
import argparse
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


def has_pages(path):
    """A mirror directory is only useful once it holds at least one page."""
    return path.is_dir() and any(path.rglob("index.html"))


def bootstrap_mirror(args, project, root, env_name):
    """Mirror the site because `t4 serve` was asked for and there is none.

    `t4 serve` is the command people reach for, and failing with "run t4
    mirror first" is a round trip the tool can take itself. It needs the
    published site to be reachable -- for a test channel behind a campus
    network, that means the VPN -- so the message says what it is about to do
    and why it might fail.
    """
    env = project.env(env_name)
    url = env.get("published_url")
    if not url:
        raise T4Error(
            "No mirror in %s, and no published_url for %r to make one from.\n"
            "  t4 mirror https://webtest.example.edu/" % (root, env_name))

    print("No mirror yet for %s." % env_name)
    print("Fetching one from %s - this needs the published site to be "
          "reachable\n(behind a VPN, that means connected).\n" % url)
    run_mirror(argparse.Namespace(url=url, env=env_name, timeout=30,
                                  limit=getattr(args, "setup_limit", None),
                                  include_documents=False),
               project)
    print()


def run_serve(args, project):
    root = site_root(project)
    env_name = args.env or project.default_env()

    if args.host_dir:
        site = root / args.host_dir
    else:
        def mirrors():
            return sorted(p for p in root.glob("*") if has_pages(p)) if root.is_dir() else []

        candidates = mirrors()
        if not candidates and not args.no_setup:
            bootstrap_mirror(args, project, root, env_name)
            candidates = mirrors()
        if not candidates:
            raise T4Error("No mirror found in %s\nRun `t4 mirror` first." % root)
        if len(candidates) > 1:
            print("Several mirrors present; using %s" % candidates[0].name)
            print("Pick another with: t4 serve <host>")
        site = candidates[0]

    if not site.is_dir():
        raise T4Error("No mirror at %s\nRun `t4 mirror` first." % site)

    # The mirror alone gives a browsable copy. Everything that makes the
    # inspector useful -- which layout, which content type, which file to edit
    # -- comes from `t4 pull`, so say plainly what is missing rather than
    # serving a stripped-down inspector that just looks broken.
    if not (project.env_dir(env_name) / "_manifest.json").is_file():
        print("No pulled source for %s: pages will serve, but the inspector "
              "cannot\nname the files behind them. Get it with: t4 pull -e %s\n"
              % (env_name, env_name))

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
                    reload=not args.no_reload,
                    source_dir=str(project.env_dir(env_name)),
                    env_name=env_name,
                    inspect="off" if args.no_inspect else args.inspect,
                    component_markers=project.config.get("component_markers"),
                    preview_edits=not args.no_preview,
                    span_markers=project.config.get("span_markers"),
                    serve_media_source=not args.no_media_source,
                    published_urls=[e.get("published_url")
                                    for e in project.config.get("environments", {}).values()
                                    if e.get("published_url")])
    return 0
