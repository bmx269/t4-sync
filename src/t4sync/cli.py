"""Command-line interface."""
import argparse
import sys

from . import __version__
from .config import Project
from .errors import T4Error
from .commands import diff, env, init, pull, push, site, sync, token


def build_parser():
    p = argparse.ArgumentParser(
        prog="t4",
        description="Pull, push and compare Terminal Four layout source, "
                    "and run a local copy of the published site.")
    p.add_argument("--version", action="version", version="t4-sync %s" % __version__)
    sub = p.add_subparsers(dest="command", metavar="<command>")

    def add(name, help_text):
        return sub.add_parser(name, help=help_text, description=help_text)

    def env_flags(sp, allow_all=True):
        sp.add_argument("--env", "-e", help="environment name")
        if allow_all:
            sp.add_argument("--all", action="store_true",
                            help="every configured environment with a token")
        sp.add_argument("--timeout", type=int, default=60)

    sp = add("init", "Set up a project in this directory.")
    sp.add_argument("directory", nargs="?")
    sp.add_argument("--force", action="store_true")

    sp = add("env", "List or configure environments.")
    esub = sp.add_subparsers(dest="env_action", metavar="<action>")
    ea = esub.add_parser("add", help="add an environment")
    ea.add_argument("name")
    ea.add_argument("url", help="CMS base URL, e.g. https://myweb-test.example.edu")
    ea.add_argument("--label")
    ea.add_argument("--push", action="store_true",
                    help="allow push to this environment (off by default)")
    er = esub.add_parser("remove", help="remove an environment")
    er.add_argument("name")

    sp = add("token", "Store or check API tokens.")
    tsub = sp.add_subparsers(dest="token_action", metavar="<action>")
    ts = tsub.add_parser("set", help="store a token (prompts; input hidden)")
    ts.add_argument("name")
    ts.add_argument("--value", help="pass non-interactively (avoid: enters shell history)")
    tsub.add_parser("status", help="show stored tokens and expiry")

    sp = add("pull", "Fetch layout source from T4 onto disk.")
    env_flags(sp)
    sp.add_argument("--only", action="append", help="limit to these endpoints")
    sp.add_argument("--no-detail", action="store_true",
                    help="metadata only: fast, but no source")
    sp.add_argument("--force", action="store_true",
                    help="overwrite files edited locally since the last pull")

    sp = add("sync", "Pull, then report what changed on each side.")
    env_flags(sp)

    sp = add("diff", "Show where local files differ from T4. Read-only.")
    sp.add_argument("paths", nargs="*")
    env_flags(sp, allow_all=False)
    sp.add_argument("--show", action="store_true", help="full unified diffs")
    sp.add_argument("--all", action="store_true", help="list unchanged files too")

    sp = add("push", "Send local changes to T4, one confirmation per file.")
    sp.add_argument("paths", nargs="*")
    env_flags(sp, allow_all=False)
    sp.add_argument("--dry-run", action="store_true")
    sp.add_argument("--yes", action="store_true",
                    help="skip per-file prompts (use with explicit paths)")

    sp = add("mirror", "Download the published site for local development.")
    sp.add_argument("url", nargs="?")
    sp.add_argument("--env", "-e")
    sp.add_argument("--limit", type=int, help="stop after this many files")
    sp.add_argument("--timeout", type=int, default=30)
    sp.add_argument("--include-documents", action="store_true",
                    help="also download PDFs and Office files")

    sp = add("serve", "Serve the mirrored site locally.")
    sp.add_argument("host_dir", nargs="?", help="which mirror to serve")
    sp.add_argument("--port", "-p", type=int, default=8321)
    sp.add_argument("--env", "-e", help="environment whose published URL to proxy")
    sp.add_argument("--proxy", metavar="URL",
                    help="fetch anything missing locally from this origin")
    sp.add_argument("--no-proxy", action="store_true",
                    help="serve only what is mirrored; 404 otherwise")
    sp.add_argument("--no-proxy-cache", action="store_true",
                    help="proxy without saving fetched files into the mirror")

    return p


HANDLERS = {
    "init": (init.run, False),
    "env": (env.run, True),
    "token": (token.run, True),
    "pull": (pull.run, True),
    "sync": (sync.run, True),
    "diff": (diff.run, True),
    "push": (push.run, True),
    "mirror": (site.run_mirror, True),
    "serve": (site.run_serve, True),
}


def main(argv=None):
    parser = build_parser()
    args = parser.parse_args(argv)
    if not args.command:
        parser.print_help()
        return 0

    handler, needs_project = HANDLERS[args.command]
    try:
        if args.command == "env" and not args.env_action:
            args.env_action = None
        if args.command == "token" and not args.token_action:
            args.token_action = "status"
        project = Project.find() if needs_project else None
        return handler(args, project)
    except T4Error as exc:
        print("\n%s" % exc, file=sys.stderr)
        return 2
    except KeyboardInterrupt:
        print("\nInterrupted.", file=sys.stderr)
        return 130


if __name__ == "__main__":
    sys.exit(main())
