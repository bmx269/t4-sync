"""t4 doctor -- check this tool against a particular T4 instance.

T4's REST surface varies between versions and deployments: endpoint names are
case-sensitive, a path that is not routed answers 500 rather than 404, and
sites sit behind different reverse proxies. Rather than assert compatibility
with a given release, probe the instance and report what actually works.

Read-only. Issues GET requests only.
"""
import re
import urllib.error
import urllib.request

from ..api import Client, describe_http_error
from ..endpoints import ENDPOINTS, SOURCE_FIELDS
from ..commands.token import decode_expiry

VERSION_RE = re.compile(r"Version\s+(\d+\.\d+\.\d+(?:\.\d+)?)", re.I)


def detect_version(api_base, timeout=20):
    """Read the version off the CMS login page.

    The REST API does not report it, but the login screen does, and it needs no
    authentication. Best effort: a proxy or custom skin may hide it.
    """
    root = api_base.rstrip("/")
    root = root[:-3] if root.endswith("/rs") else root
    for path in ("/login.jsp", "/terminalfour/login.jsp", "/"):
        try:
            req = urllib.request.Request(root.rstrip("/") + path)
            req.add_header("User-Agent", "t4-sync/doctor")
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                body = resp.read().decode("utf-8", errors="replace")
        except Exception:
            continue
        found = VERSION_RE.search(body)
        if found:
            return found.group(1)
    return None


def run(args, project):
    env_name = args.env or project.default_env()
    env = project.env(env_name)
    print("Environment  %s" % env["label"])
    print("API base     %s" % env["base"])

    version = detect_version(env["base"], timeout=args.timeout)
    print("T4 version   %s" % (version or "could not detect"))

    token = project.read_token(env_name)
    if not token:
        print("Token        none stored - run: t4 token set %s" % env_name)
        return 1
    expiry = decode_expiry(token)
    print("Token        stored%s" % (
        ", expires %s" % expiry.strftime("%Y-%m-%d") if expiry else ""))
    print("Push         %s" % ("allowed" if env.get("push_allowed") else "disabled"))
    print()

    client = Client(env, token, timeout=args.timeout)
    working = 0
    http_failures = []
    unreachable = []

    print("Endpoints")
    for endpoint in sorted(ENDPOINTS):
        if unreachable:
            # The host is down; probing the rest just repeats the same timeout
            # and backoff for every endpoint.
            print("  %-13s -     skipped (host unreachable)" % endpoint)
            continue
        path = ENDPOINTS[endpoint].get("path", endpoint)
        try:
            data = client.get_json(path)
        except urllib.error.HTTPError as exc:
            print("  %-13s FAIL  %s" % (endpoint, describe_http_error(exc)))
            http_failures.append((endpoint, exc.code))
            continue
        except Exception as exc:
            # A transport failure says nothing about the endpoint -- the host
            # was never reached. Keep it separate so the advice below is right.
            reason = getattr(exc, "reason", exc)
            print("  %-13s FAIL  could not connect (%s)" % (endpoint, reason))
            unreachable.append(endpoint)
            continue

        items = data if isinstance(data, list) else [data]
        note = "%d item(s)" % len(items)

        # Confirm the fields the tool extracts are actually present, since a
        # DTO change between releases would show up here rather than as an
        # error -- a silent empty pull is the failure mode worth catching.
        if ENDPOINTS[endpoint].get("detail") and items:
            first = items[0]
            item_id = first.get("id") if isinstance(first, dict) else None
            if item_id is not None:
                try:
                    detail = client.get_json("%s/%s" % (path, item_id))
                    present = [f for f in SOURCE_FIELDS
                               if isinstance(detail.get(f), str) and detail[f].strip()]
                    known = sorted(set(detail) & set(SOURCE_FIELDS))
                    if present:
                        note += ", source in: %s" % ", ".join(present)
                    elif known:
                        note += ", source fields present but empty: %s" % ", ".join(known)
                    else:
                        note += ", NO known source field (DTO may have changed)"
                except Exception:
                    note += ", detail fetch failed"

        print("  %-13s ok    %s" % (endpoint, note))
        working += 1

    # When the host is unreachable the probe stops early, so count every
    # endpoint as failing rather than only the one that was actually tried.
    failing = len(ENDPOINTS) - working if unreachable else len(http_failures)
    print("\n%d endpoint(s) working, %d failing." % (working, failing))

    if unreachable:
        print("\nNothing could connect to %s." % env["base"])
        print("That is a network problem, not an API one -- the host was never\n"
              "reached. A T4 instance is usually behind a VPN; check that it is\n"
              "connected and that the hostname resolves.")
    elif any(code == 500 for _, code in http_failures):
        print("\nA 500 means the endpoint is not routed on this deployment, or\n"
              "its name is cased differently -- the two are indistinguishable.\n"
              "Endpoint names are in src/t4sync/endpoints.py.")
    elif any(code in (401, 403) for _, code in http_failures):
        print("\nThe token was rejected. Generate a new one in T4 under\n"
              "Administration > User Management, then: t4 token set %s" % env_name)

    if version and not version.startswith("8.4."):
        print("\nThis tool is verified against T4 8.4.x. Treat %s as untested --\n"
              "if the endpoints above all report ok, it is working." % version)
    return 1 if failing else 0
