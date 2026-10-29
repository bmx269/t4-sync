"""t4 app -- open the web UI in a browser."""
from ..app import DEFAULT_PORT, run_app


def run(args, project):
    return run_app(project, port=args.port, host=args.host,
                   open_browser=not args.no_open)
