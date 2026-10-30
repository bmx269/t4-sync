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
from ..compare import load_manifest
from ..endpoints import ENDPOINTS, ID_FIELDS, SOURCE_FIELDS
from ..commands.token import decode_expiry

VERSION_RE = re.compile(r"Version\s+(\d+\.\d+\.\d+(?:\.\d+)?)", re.I)
MEDIA_TAG = re.compile(r'type=["\']media["\'][^>]*?\bid=["\'](\d+)["\']', re.I)


def first_id(items):
    """The first usable record id in a collection response."""
    for item in items or []:
        if not isinstance(item, dict):
            continue
        for field in ID_FIELDS:
            value = item.get(field)
            if isinstance(value, int):
                return value
            if isinstance(value, str) and value.isdigit():
                return value
    return None


def as_items(data):
    """A collection response, however this endpoint happens to wrap one."""
    if isinstance(data, dict):
        for key in ("children", "data", "items"):
            if isinstance(data.get(key), list):
                return data[key]
        return [data]
    return data if isinstance(data, list) else []


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


def _more_layout_ids(stubs, skip, limit):
    """A few other content layout ids, for finding a media tag."""
    found = []
    for item in as_items(stubs):
        if not isinstance(item, dict):
            continue
        value = item.get("id")
        if value is not None and value != skip:
            found.append(value)
        if len(found) >= limit:
            break
    return found


def media_id_from_manifest(project, env_name):
    """A media id this project has already pulled, if it has pulled any.

    Media is the one thing with no collection endpoint: ids are found by
    reading media tags out of layouts, and whether any given layout has one
    is luck. Reading enough layouts to be sure would be a crawl, which is
    what this probe is meant not to be. A previous pull already recorded
    exactly the ids the tool fetches, so when there is one, use it.
    """
    if project is None or env_name is None:
        return None
    try:
        manifest = load_manifest(project, env_name)
    except Exception:                                 # noqa: BLE001
        return None
    for meta in manifest.values():
        if meta.get("endpoint") == "media" and meta.get("id") is not None:
            return meta["id"]
    return None


def probe_derived(client, collected, language, project=None, env_name=None):
    """The paths that need an id in them.

    `pull` and `sections` spend almost all their requests here -- content
    layouts, the section tree, media text -- and none of it was probed,
    because none of it is a bare collection name. The ids come from the
    collection responses already fetched above, so this costs a handful of
    requests rather than a crawl.

    A DTO change behind one of these shows up as an empty pull rather than
    an error, which is the failure worth catching early. Returns
    (working, failing, skipped).
    """
    working, failing, skipped = 0, 0, 0
    # Anything fetched that might carry a media tag, since media has no
    # collection endpoint to take an id from.
    bodies = list(collected.get("_bodies") or [])

    def row(label, state, note):
        print("  %-33s %-5s %s" % (label, state, note))

    def probe(label, path, want=None):
        """Fetch one path; `want` names a field that has to be in the reply."""
        nonlocal working, failing
        try:
            data = client.get_json(path)
        except urllib.error.HTTPError as exc:
            row(label, "FAIL", describe_http_error(exc))
            failing += 1
            return None
        except Exception as exc:                      # noqa: BLE001
            row(label, "FAIL", "could not connect (%s)"
                % getattr(exc, "reason", exc))
            failing += 1
            return None
        items = as_items(data)
        note = "%d item(s)" % len(items)
        if want:
            first = items[0] if items else {}
            if isinstance(first, dict) and want in first:
                note += ", has %s" % want
            elif items:
                note += ", NO %s (DTO may have changed)" % want
        row(label, "ok", note)
        working += 1
        return data

    def unavailable(label, why):
        nonlocal skipped
        row(label, "-", "skipped (%s)" % why)
        skipped += 1

    # Content layouts hang off a content type, two requests deep.
    type_id = first_id(as_items(collected.get("contenttype")))
    layout_id, stubs = None, None
    if type_id is None:
        unavailable("layout/contenttype/{id}/{lang}", "no content type to ask about")
        unavailable("layout/{id}/{lang}", "no content type to ask about")
    else:
        stubs = probe("layout/contenttype/{id}/{lang}",
                      "layout/contenttype/%s/%s" % (type_id, language))
        layout_id = first_id(as_items(stubs))
        if layout_id is None:
            unavailable("layout/{id}/{lang}",
                        "content type %s has no layouts" % type_id)
        else:
            layout = probe("layout/{id}/{lang}",
                           "layout/%s/%s" % (layout_id, language),
                           want="elements")
            # Content layouts are where media tags usually live, so this is
            # the likeliest place to find an id for the media probe below.
            for item in as_items(layout):
                for value in (item.get("elements") or {}).values() \
                        if isinstance(item, dict) else []:
                    if isinstance(value, str):
                        bodies.append(value)

    # The section tree, which the inspector is built on. The root is asked
    # about rather than a child: it is the one section certain to exist.
    tree = collected.get("sitestructure")
    section_id = first_id([tree]) if isinstance(tree, dict) else None
    if section_id is None:
        section_id = first_id(as_items(tree))
    if section_id is None:
        for label in ("hierarchy/{id}/{lang}",
                      "hierarchy/{id}/{lang}/subsections",
                      "hierarchy/{id}/{lang}/contents"):
            unavailable(label, "no section to ask about")
    else:
        probe("hierarchy/{id}/{lang}",
              "hierarchy/%s/%s" % (section_id, language), want="channels")
        probe("hierarchy/{id}/{lang}/subsections",
              "hierarchy/%s/%s/subsections" % (section_id, language))
        probe("hierarchy/{id}/{lang}/contents",
              "hierarchy/%s/%s/contents" % (section_id, language))

    # Media has no collection endpoint: ids are found by reading media tags
    # out of the layouts, so a layout has to have been fetched to have one.
    # Still nothing to go on: look through a few more content layouts
    # rather than give up on the first. These are quiet -- they are a search
    # for an id, not a probe, and a failure here says nothing about the
    # endpoint being probed.
    if layout_id is not None and not any(MEDIA_TAG.search(b or "")
                                         for b in bodies):
        for other in _more_layout_ids(stubs, layout_id, limit=4):
            try:
                extra = client.get_json("layout/%s/%s" % (other, language))
            except Exception:                         # noqa: BLE001
                break
            for item in as_items(extra):
                if isinstance(item, dict):
                    bodies.extend(v for v in (item.get("elements") or {}).values()
                                  if isinstance(v, str))
            if any(MEDIA_TAG.search(b or "") for b in bodies):
                break

    media_id = media_id_from_manifest(project, env_name)
    for text in [] if media_id else bodies:
        found = MEDIA_TAG.findall(text or "")
        if found:
            media_id = found[0]
            break
    if media_id is None:
        unavailable("media/{id}/{lang}",
                    "no media tag found, and nothing pulled yet to take an "
                    "id from")
    else:
        probe("media/{id}/{lang}", "media/%s/%s" % (media_id, language))

    return working, failing, skipped


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
    language = env.get("language", "en")
    working = 0
    http_failures = []
    unreachable = []
    # Kept so the id-bearing paths below have something to ask about, rather
    # than fetching the same collections a second time.
    collected = {"_bodies": []}

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
        collected[endpoint] = data
        note = "%d item(s)" % len(items)

        # Confirm the fields the tool extracts are actually present, since a
        # DTO change between releases would show up here rather than as an
        # error -- a silent empty pull is the failure mode worth catching.
        if ENDPOINTS[endpoint].get("source") and items:
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
                    collected["_bodies"].extend(detail[f] for f in present)
                except Exception:
                    note += ", detail fetch failed"

        print("  %-13s ok    %s" % (endpoint, note))
        working += 1

    # When the host is unreachable the probe stops early, so count every
    # endpoint as failing rather than only the one that was actually tried.
    failing = len(ENDPOINTS) - working if unreachable else len(http_failures)

    skipped = 0
    if not unreachable:
        print("\nPaths that take an id")
        more_ok, more_bad, skipped = probe_derived(
            client, collected, language, project, env_name)
        working += more_ok
        failing += more_bad

    print("\n%d path(s) working, %d failing%s."
          % (working, failing,
             ", %d not checked" % skipped if skipped else ""))

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
