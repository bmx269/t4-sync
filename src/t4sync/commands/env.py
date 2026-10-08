"""t4 env -- list and configure environments."""
from ..endpoints import ENDPOINTS


def run(args, project):
    if args.env_action == "add":
        base = args.url.rstrip("/")
        if not base.endswith("/rs"):
            # Every T4 install serves the API under /terminalfour/rs; accepting
            # a bare host and correcting it saves a confusing round of 500s.
            base = base.rstrip("/") + "/terminalfour/rs"
        project.config.setdefault("environments", {})[args.name] = {
            "base": base,
            "label": args.label or args.name,
            "push_allowed": bool(args.push),
        }
        if len(project.config["environments"]) == 1:
            project.config["default_environment"] = args.name
        project.save()
        print("Added %s -> %s%s" % (args.name, base, "" if args.push else "  (pull-only)"))
        print("Next: t4 token set %s" % args.name)
        return 0

    if args.env_action == "remove":
        envs = project.config.get("environments", {})
        if args.name in envs:
            del envs[args.name]
            project.save()
            print("Removed %s (its pulled source and token were left in place)" % args.name)
        else:
            print("No such environment: %s" % args.name)
        return 0

    default = project.config.get("default_environment")
    if not project.env_names():
        print("No environments configured. Add one:")
        print("  t4 env add test https://myweb-test.example.edu")
        return 0
    for name in project.env_names():
        env = project.env(name)
        token = "token" if project.read_token(name) else "no token"
        print("  %-10s %-52s %-9s %s%s" % (
            name, env["base"], token,
            "push" if env.get("push_allowed") else "pull-only",
            "  (default)" if name == default else ""))
    print("\nEndpoints pulled: %s" % ", ".join(sorted(ENDPOINTS)))
    return 0
