"""t4 sections -- walk the section tree and record each section's page layout.

Two requests per section, so this is slow and deliberately separate from the
normal pull. It is resumable: re-running keeps what it already has.
"""
import json

from ..api import Client
from ..progress import Progress
from .. import sections as sectionlib


def run(args, project):
    env_name = args.env or project.default_env()
    env = project.env(env_name)
    client = Client(env, project.require_token(env_name), timeout=args.timeout)
    language = env.get("language", "en")
    env_dir = project.env_dir(env_name)
    (env_dir / "_raw").mkdir(parents=True, exist_ok=True)

    known = {} if args.restart else sectionlib.load(str(env_dir))
    if known:
        print("Resuming from %d known section(s)." % len(known))

    root = args.root or env.get("root_section")
    if root is None:
        raise_help = ("No root section. Pass --root <id>, or set "
                      "\"root_section\" in .t4/config.json.\n"
                      "The site root is usually a child of section 1; "
                      "`t4 sections --root 1 --limit 20` will show them.")
        from ..errors import T4Error
        raise T4Error(raise_help)

    bar = Progress("  sections", total=args.limit)
    try:
        found = sectionlib.walk(client, root, language, known=known,
                                limit=args.limit,
                                progress=lambda _n: bar.update())
    finally:
        bar.close()

    path = env_dir / "_raw" / sectionlib.CACHE
    path.write_text(json.dumps(found, indent=2, sort_keys=True) + "\n", encoding="utf-8")

    with_layout = sum(1 for r in found.values() if sectionlib.layout_for(r))
    print("\n%d section(s), %d with a page layout assignment" % (len(found), with_layout))
    print("Saved to %s" % path.relative_to(project.root))
    if args.limit and len(found) >= args.limit:
        print("Stopped at --limit; re-run to continue from here.")
    return 0
