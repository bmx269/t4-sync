"""t4 token -- store and check API tokens, one per environment."""
import base64
import datetime
import getpass
import json

from ..errors import T4Error


def decode_expiry(token):
    """Read a JWT's exp claim without verifying it. Best-effort only."""
    parts = token.split(".")
    if len(parts) != 3:
        return None
    try:
        payload = parts[1] + "=" * (-len(parts[1]) % 4)
        claims = json.loads(base64.urlsafe_b64decode(payload))
    except Exception:
        return None
    exp = claims.get("exp")
    return datetime.datetime.fromtimestamp(exp) if exp else None


def run(args, project):
    if args.token_action == "set":
        name = args.name
        project.env(name)  # fail early if the environment is unknown

        # getpass keeps the token off the terminal, out of shell history and
        # out of the process list.
        token = args.value or getpass.getpass("Paste the %s API token (hidden): " % name)
        token = token.strip()
        if not token:
            raise T4Error("Empty input; nothing written.")
        if token.count(".") != 2:
            print("Warning: that does not look like a JWT (expected two dots).")

        path = project.write_token(name, token)
        expiry = decode_expiry(token)
        print("Stored for %s at %s" % (name, path.relative_to(project.root)))
        if expiry:
            state = "EXPIRED" if expiry < datetime.datetime.now() else "valid"
            print("Expires %s (%s)" % (expiry.strftime("%Y-%m-%d %H:%M"), state))
        print("Verify with: t4 pull --env %s --only channel" % name)
        return 0

    # status
    for name in project.env_names():
        token = project.read_token(name)
        if not token:
            print("  %-10s no token      (t4 token set %s)" % (name, name))
            continue
        expiry = decode_expiry(token)
        if expiry:
            state = "EXPIRED" if expiry < datetime.datetime.now() else "valid"
            print("  %-10s %-8s expires %s" % (name, state, expiry.strftime("%Y-%m-%d %H:%M")))
        else:
            print("  %-10s stored    (no expiry claim)" % name)
    return 0
