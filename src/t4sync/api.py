"""HTTP access to the Terminal Four Web Services API."""
import json
import time
import urllib.error
import urllib.request

from .errors import T4Error

RETRYABLE_ATTEMPTS = 4


class Client:
    def __init__(self, env, token, timeout=60):
        self.base = env["base"].rstrip("/")
        self.label = env.get("label", env["name"])
        self.token = token
        self.timeout = timeout
        self._cache = {}

    def _request(self, path, method="GET", payload=None):
        url = "%s/%s" % (self.base, path.lstrip("/"))
        data = json.dumps(payload).encode("utf-8") if payload is not None else None
        req = urllib.request.Request(url, data=data, method=method)
        req.add_header("Authorization", "Bearer %s" % self.token)
        req.add_header("Accept", "application/json")
        if data is not None:
            req.add_header("Content-Type", "application/json")

        # Transport failures are retried; HTTP statuses are not, because the
        # server answered and repeating the call will produce the same answer.
        # The VPN path to a T4 instance is frequently unreliable, and a full
        # pull is several hundred requests.
        for attempt in range(1, RETRYABLE_ATTEMPTS + 1):
            try:
                with urllib.request.urlopen(req, timeout=self.timeout) as resp:
                    body = resp.read().decode("utf-8", errors="replace")
                return body
            except urllib.error.HTTPError:
                raise
            except Exception:
                if attempt == RETRYABLE_ATTEMPTS:
                    raise
                time.sleep(min(2 ** (attempt - 1), 8))

    def get_json(self, path):
        body = self._request(path)
        if not body.strip():
            return None
        try:
            return json.loads(body)
        except json.JSONDecodeError as exc:
            raise T4Error("%s returned non-JSON: %s" % (path, exc))

    def get_bytes(self, path):
        """Fetch a binary media element.

        T4 answers the media path with a short-lived signed download URL
        rather than the bytes, so this is two requests: the URL, then the
        file. The URL is single use, so it cannot be cached.

        The first response is a 302 whose *body* carries the URL and which has
        no Location header, so urllib raises rather than following it. Reading
        the body off the error is the whole trick.
        """
        try:
            location = self._request(path)
        except urllib.error.HTTPError as exc:
            if exc.code not in (301, 302, 303, 307, 308):
                raise
            location = exc.headers.get("Location") or exc.read().decode(
                "utf-8", errors="replace")
        location = (location or "").strip().strip('"')
        if not location.startswith("http"):
            raise T4Error("expected a download URL, got %r" % location[:80])
        req = urllib.request.Request(location)
        req.add_header("Authorization", "Bearer %s" % self.token)
        with urllib.request.urlopen(req, timeout=self.timeout) as resp:
            return resp.read()

    def put_json(self, path, payload):
        body = self._request(path, method="PUT", payload=payload)
        return json.loads(body) if body.strip() else None

    def record(self, endpoint, item_id):
        """Fetch one record, cached, so a multi-field item is fetched once."""
        return self.fetch_path("%s/%s" % (endpoint, item_id))

    def fetch_path(self, path):
        """Fetch an arbitrary path, cached.

        Not every resource is `endpoint/id`: content layouts need a language
        segment, so the manifest can record a full path instead.
        """
        if path not in self._cache:
            data = self.get_json(path)
            # Some endpoints answer with a single-item list.
            if isinstance(data, list) and len(data) == 1:
                data = data[0]
            self._cache[path] = data
        return self._cache[path]


def describe_http_error(exc):
    """Turn an HTTPError into something worth printing.

    A T4 deployment answers 500 for a path it does not route, which is
    indistinguishable from a genuine server error -- and a wrong-cased endpoint
    name produces exactly that. Say so, because it is the most common cause.
    """
    if exc.code in (401, 403):
        return "HTTP %s - token rejected or lacks rights" % exc.code
    if exc.code == 500:
        return "HTTP 500 - not routed (check the exact spelling and case)"
    return "HTTP %s %s" % (exc.code, exc.reason)
