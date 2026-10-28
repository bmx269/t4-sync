"""t4 mirror / t4 serve -- run a local copy of the published site."""
import argparse
import pathlib

from .. import mirror as mirror_mod
from .. import serve as serve_mod
from .. import history
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
    if delay:
        print("Pacing:   %.2fs between requests" % delay)
    delay = getattr(args, "delay", None)
    if delay is None:
        delay = mirror_mod.DEFAULT_DELAY
    pages, assets, errors = mirror_mod.crawl(
        url, dest, reject_ext=reject, limit=args.limit, timeout=args.timeout,
        delay=delay, retries=getattr(args, "retries", 3))
    print("\n%d pages, %d assets, %d errors" % (pages, assets, errors))
    history.record(project, "mirror", env=env_name, pages=pages,
                   assets=assets, failures=errors or None)
    if pages and not getattr(args, "quiet_hint", False):
        print("Serve it: t4 serve")
    return 0


def host_of(project, env_name):
    """The mirror directory name for an environment, from its published URL."""
    try:
        url = project.env(env_name).get("published_url") or ""
    except T4Error:
        return None
    return url.split("//", 1)[-1].split("/", 1)[0].strip() or None


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

    print("No mirror found for %s - running `t4 mirror` first.\n" % env_name)
    print("  source   %s" % url)
    print("  note     this fetches the published site over HTTP, so that host")
    print("           must be reachable; behind a VPN, that means connected.")
    print("  skip it  t4 serve --no-setup, or cap it with --setup-limit N\n")
    run_mirror(argparse.Namespace(url=url, env=env_name, timeout=30,
                                  limit=getattr(args, "setup_limit", None),
                                  include_documents=False, quiet_hint=True,
                                  delay=None, retries=3),
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
        tried = False
        # Match the environment first. Picking the alphabetically first
        # directory meant `t4 serve --env prod` served webtest.ufv.ca,
        # because "webtest" sorts before "www" -- the wrong site, silently,
        # with the prod source and inspector mapped over it.
        wanted = host_of(project, env_name)
        if wanted:
            preferred = [p for p in candidates if p.name == wanted]
            if preferred:
                candidates = preferred
            elif candidates and not args.no_setup:
                # The right mirror is simply absent; fetch it rather than
                # serving someone else's.
                bootstrap_mirror(args, project, root, env_name)
                candidates = [p for p in mirrors() if p.name == wanted] or mirrors()

        if not candidates and not args.no_setup:
            bootstrap_mirror(args, project, root, env_name)
            candidates = mirrors()
            tried = True
        if not candidates and tried:
            # Telling someone to run the command that just ran and failed is
            # worse than saying nothing.
            raise T4Error(
                "The mirror fetched no pages, so there is nothing to serve.\n"
                "The published site was unreachable or returned no HTML — if "
                "it is\nbehind a VPN, check you are connected, then: t4 mirror")
        if not candidates:
            raise T4Error("No mirror found in %s\nRun `t4 mirror` first." % root)
        if len(candidates) > 1:
            print("Several mirrors present; using %s" % candidates[0].name)
            print("Pick another with: t4 serve <host>")
        site = candidates[0]
        if wanted and site.name != wanted:
            print("! Serving %s, but %s publishes to %s."
                  % (site.name, env_name, wanted))
            print("  The inspector will map %s source onto another site's "
                  "pages." % env_name)

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
                    project_root=str(project.root),
                    published_urls=[e.get("published_url")
                                    for e in project.config.get("environments", {}).values()
                                    if e.get("published_url")])
    return 0
