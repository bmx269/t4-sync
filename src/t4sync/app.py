"""t4 app -- the commands, in a browser, for people who do not live in a terminal.

A page served on the loopback interface rather than a desktop window. The
reasons are practical: it needs no dependency beyond the standard library the
rest of the tool already restricts itself to, it behaves the same on macOS and
Windows, and the two things a terminal is genuinely bad at here -- choosing
which of forty changed files to deploy, and watching a long pull scroll past
-- are a checkbox list and a log pane, which is to say a web page.

**Every action shells out to the CLI.** `t4 push` from this page runs the same
`t4 push` a terminal would, as a subprocess. Nothing here reimplements a
command, so nothing here can quietly disagree with one: the per-environment
`push_allowed` flag, the uncommitted-source gate and the pre-push backups all
still happen, because the code that enforces them is the code being run.

Binding to loopback is not on its own a security boundary -- any page in any
browser tab can POST to 127.0.0.1. So every API call carries a token minted at
startup and handed out only in the URL this command opens. Without it the
answer is 403, which keeps a stray web page from triggering a deploy.
"""
import html
import json
import os
import queue
import re
import secrets
import subprocess
import sys
import threading
import webbrowser
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from . import history
from .compare import load_manifest
from .vcs import open_repo, requires_commit

# Deliberately not serve's 8321: running the panel and the site at once is the
# normal case, not a clash to resolve.
DEFAULT_PORT = 8320

# `t4 serve` prints "URL       http://127.0.0.1:8321/".
URL_LINE = re.compile(r"\b(https?://(?:127\.0\.0\.1|localhost)(?::\d+)?/\S*)")


def child_env():
    """Environment for a subprocess running the CLI.

    `python -m t4sync` only works if t4sync is importable, and whether it is
    depends on how the tool was installed -- a `pip install` puts it on the
    path, a source checkout run with PYTHONPATH does not pass that on. So the
    directory this very module was imported from is handed to the child
    explicitly: the panel then runs the same code that is running it, rather
    than whatever happens to be installed, or nothing at all.
    """
    here = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    existing = os.environ.get("PYTHONPATH")
    return dict(
        os.environ,
        PYTHONUNBUFFERED="1",
        PYTHONPATH=os.pathsep.join([here] + ([existing] if existing else [])))


class Job:
    """One CLI invocation, with its output kept for anyone watching."""

    def __init__(self, job_id, argv, cwd):
        self.id = job_id
        self.argv = argv
        self.cwd = cwd
        self.lines = []
        self.done = False
        self.returncode = None
        self.proc = None
        # Read out of the command's own output rather than assumed: serve
        # takes --port, so the panel guessing 8321 would send you to the
        # wrong place exactly when you had a reason to change it.
        self.url = None
        self._watchers = []
        self._lock = threading.Lock()

    def start(self):
        threading.Thread(target=self._run, daemon=True).start()
        return self

    def _run(self):
        self._emit("$ t4 %s" % " ".join(self.argv))
        try:
            self.proc = subprocess.Popen(
                [sys.executable, "-m", "t4sync"] + self.argv,
                cwd=self.cwd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                # Line-buffered and unbuffered on the child's side, so a long
                # pull reports progress instead of arriving all at once.
                bufsize=1, universal_newlines=True, env=child_env())
            for line in self.proc.stdout:
                self._emit(line.rstrip("\n"))
            self.returncode = self.proc.wait()
        except Exception as exc:                      # noqa: BLE001 - reported
            self._emit("failed to run: %s" % exc)
            self.returncode = -1
        finally:
            self.done = True
            self._emit(None)

    def _emit(self, line):
        with self._lock:
            if line is not None:
                self.lines.append(line)
                if self.url is None:
                    found = URL_LINE.search(line)
                    if found:
                        self.url = found.group(1)
                        # Tell anyone watching straight away. The URL turns
                        # up seconds after the job starts, so a UI that only
                        # learns it when the job *ends* would leave "View
                        # site" dead for the entire time the site is up.
                        for watcher in self._watchers:
                            watcher.put({"meta": self.summary()})
            for watcher in self._watchers:
                watcher.put(line)

    def watch(self):
        """A queue fed the backlog, then anything further.

        The backlog includes the URL if one has already been found. `t4 serve`
        prints its address within a second or two of starting -- often before
        the browser has finished opening the event stream -- and a meta event
        sent in that gap reached nobody. The symptom was "View site" staying
        dead until the page was reloaded, which is exactly when the backlog
        was replayed and the URL finally arrived.
        """
        channel = queue.Queue()
        with self._lock:
            for line in self.lines:
                channel.put(line)
            if self.url:
                channel.put({"meta": self.summary()})
            if self.done:
                channel.put(None)
            else:
                self._watchers.append(channel)
        return channel

    def stop(self):
        if self.proc and self.proc.poll() is None:
            self.proc.terminate()

    def summary(self):
        return {"id": self.id, "argv": self.argv, "done": self.done,
                "returncode": self.returncode, "lines": len(self.lines),
                "lane": LANES.get(self.argv[0]), "label": self.label(),
                "url": self.url}

    def label(self):
        return "t4 " + " ".join(a for a in self.argv if not a.startswith("--"))


# What a command writes, and therefore what it cannot run beside.
#
# Serving the site is a long-running task, so anything that blocks on it
# blocks for the whole session -- and there is no reason it should. Pulling
# while the site is served is the normal way to work. The only real conflicts
# are between commands writing the same thing:
#
#   source   pull, sync, sections write t4-source; save and push read it and
#            write the git index and _backup. Two at once race.
#   mirror   two crawls into one directory.
#   serve    one server, one port.
#
# Everything absent from this table is read-only and never blocked.
LANES = {
    "pull": "source", "sync": "source", "sections": "source",
    "save": "source", "push": "source",
    "mirror": "mirror",
    "serve": "serve",
}

LANE_REASON = {
    "source": "%s is already running, and both write the pulled source. "
              "Let it finish first.",
    "mirror": "%s is already running, into the same directory.",
    "serve": "%s is already running. Stop it to start another.",
}


class Conflict(Exception):
    """A command cannot start because one it would race with is running."""


class Jobs:
    def __init__(self, cwd):
        self.cwd = cwd
        self.items = {}
        self._next = 0
        self._lock = threading.Lock()

    def blocker(self, argv):
        """The running job `argv` would race with, or None.

        Separate from `start` so the answer can be had without launching
        anything -- the UI asks it to decide what to grey out, and it is the
        part worth testing on its own.
        """
        lane = LANES.get(argv[0])
        if not lane:
            return None
        for other in self.items.values():
            if not other.done and LANES.get(other.argv[0]) == lane:
                return other
        return None

    def start(self, argv):
        with self._lock:
            blocking = self.blocker(argv)
            if blocking is not None:
                raise Conflict(LANE_REASON[LANES[argv[0]]]
                               % ("`%s`" % blocking.label()))
            self._next += 1
            job = Job(str(self._next), argv, self.cwd)
            self.items[job.id] = job
        return job.start()

    def get(self, job_id):
        return self.items.get(job_id)

    def running(self):
        return [j for j in self.items.values() if not j.done]

    def busy_lanes(self):
        return sorted({LANES[j.argv[0]] for j in self.running()
                       if j.argv[0] in LANES})


def mirror_stats(project):
    root = project.root / project.config.get("mirror_dir", "site-mirror")
    if not root.is_dir():
        return None
    for host in sorted(p for p in root.glob("*") if p.is_dir()):
        pages = sum(1 for _ in host.rglob("index.html"))
        if pages:
            return {"host": host.name, "pages": pages}
    return None


def state(project, jobs):
    """Everything the page shows before you ask it to do anything.

    Offline by design: opening the panel must not reach for the VPN. The parts
    that need T4 -- what differs from the remote -- are asked for explicitly.
    """
    envs = []
    for name in project.env_names():
        env = project.env(name)
        source = project.env_dir(name)
        try:
            tracked = len(load_manifest(project, name))
        except Exception:                             # noqa: BLE001 - not pulled
            tracked = 0
        envs.append({
            "name": name,
            "label": env.get("label", name),
            "url": env.get("url"),
            "published_url": env.get("published_url"),
            "push_allowed": bool(env.get("push_allowed")),
            "requires_commit": requires_commit(project, name),
            "has_token": bool(project.read_token(name)),
            "tracked": tracked,
            "source": str(source),
        })

    repo = open_repo(project)
    default = project.default_env() if project.env_names() else None

    # What happened here, and when. Per environment, because "last pushed"
    # means nothing if it cannot say pushed to where.
    events = {}
    for name in project.env_names():
        latest = history.latest(project, name)
        edited_at, edited_file = history.last_edit(project, name)
        if edited_at:
            latest["edit"] = {"time": edited_at, "file": edited_file}
        events[name] = {
            action: dict(entry, ago=history.ago(entry.get("time")))
            for action, entry in latest.items()
        }

    sha, branch = repo.head() if repo.available else (None, None)
    return {
        "root": str(project.root),
        "default_env": default,
        "environments": envs,
        "mirror": mirror_stats(project),
        "events": events,
        "recent": [dict(e, ago=history.ago(e.get("time")))
                   for e in history.read(project)[-12:]][::-1],
        "repo": {"available": repo.available,
                 "summary": repo.describe(
                     [project.env_dir(default)] if default else None),
                 "branch": branch, "sha": sha[:8] if sha else None,
                 "dirty": (len(repo.dirty([project.env_dir(default)]))
                           if repo.available and default else 0)},
        # Running jobs first: a page reload has to pick the streams back up,
        # and a serve started ten minutes ago must not vanish from the UI.
        "jobs": [j.summary() for j in jobs.items.values()],
        "busy": jobs.busy_lanes(),
        "lanes": LANES,
    }


class AppHandler(BaseHTTPRequestHandler):
    project = None
    jobs = None
    token = None

    server_version = "t4-sync-app"

    # -- plumbing ----------------------------------------------------------

    def _authorized(self):
        sent = (self.headers.get("X-T4-Token")
                or self._query().get("t", [None])[0])
        return bool(self.token) and secrets.compare_digest(str(sent), self.token)

    def _query(self):
        import urllib.parse
        return urllib.parse.parse_qs(urllib.parse.urlparse(self.path).query)

    def _path(self):
        import urllib.parse
        return urllib.parse.urlparse(self.path).path

    def _send(self, code, body, content_type="application/json"):
        if not isinstance(body, bytes):
            body = body.encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        # The panel can trigger a deploy; never let it be framed or cached.
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Frame-Options", "DENY")
        self.end_headers()
        self.wfile.write(body)

    def _json(self, data, code=200):
        self._send(code, json.dumps(data), "application/json")

    def _body(self):
        length = int(self.headers.get("Content-Length") or 0)
        if not length:
            return {}
        try:
            return json.loads(self.rfile.read(length).decode("utf-8"))
        except ValueError:
            return {}

    def log_message(self, fmt, *args):
        if self._path().startswith("/api/jobs/"):
            return                       # the log stream would log itself
        sys.stderr.write("%s %s\n" % (self.address_string(), fmt % args))

    # -- routes ------------------------------------------------------------

    def do_GET(self):
        path = self._path()
        if path in ("/", "/index.html"):
            if not self._authorized():
                return self._send(403, FORBIDDEN, "text/html; charset=utf-8")
            return self._send(200, PAGE % {"token": html.escape(self.token)},
                              "text/html; charset=utf-8")
        if not self._authorized():
            return self._json({"error": "bad or missing token"}, 403)
        if path == "/api/state":
            return self._json(state(self.project, self.jobs))
        if path == "/api/changes":
            return self._changes()
        if path == "/api/diff":
            return self._diff()
        if path == "/api/tags":
            return self._tags()
        if path.startswith("/api/jobs/") and path.endswith("/stream"):
            return self._stream(path.split("/")[3])
        return self._json({"error": "not found"}, 404)

    def do_POST(self):
        if not self._authorized():
            return self._json({"error": "bad or missing token"}, 403)
        path = self._path()
        if path == "/api/run":
            argv = self._body().get("argv")
            if not isinstance(argv, list) or not argv or not all(
                    isinstance(a, str) for a in argv):
                return self._json({"error": "argv must be a list of strings"}, 400)
            if argv[0] not in ALLOWED:
                return self._json(
                    {"error": "%r is not one of %s"
                              % (argv[0], ", ".join(sorted(ALLOWED)))}, 400)
            try:
                return self._json(self.jobs.start(argv).summary())
            except Conflict as exc:
                # 409, not 400: the request is fine, the timing is not.
                return self._json({"error": str(exc), "conflict": True}, 409)
        if path == "/api/tag":
            from . import tags as tagdata
            spec = self._body()
            if not isinstance(spec, dict):
                return self._json({"error": "expected an object"}, 400)
            handlebars, note = tagdata.build_handlebars(spec)
            return self._json({"tag": tagdata.build_tag(spec),
                               "handlebars": handlebars, "note": note})
        if path.startswith("/api/jobs/") and path.endswith("/stop"):
            job = self.jobs.get(path.split("/")[3])
            if not job:
                return self._json({"error": "no such job"}, 404)
            job.stop()
            return self._json(job.summary())
        return self._json({"error": "not found"}, 404)

    def _env_arg(self):
        name = self._query().get("env", [None])[0]
        return name or self.project.default_env()

    def _changes(self):
        """Which files differ from T4, so they can be chosen one by one.

        Reads the pulled source against the manifest hash rather than asking
        T4: that is offline and instant, where comparing 753 records costs a
        request each over a VPN and takes minutes. It answers a slightly
        different question -- "what have I changed since the pull" rather
        than "what differs from T4 right now" -- which is the one you are
        asking when deciding what to deploy. `t4 diff` still does the real
        comparison.
        """
        from .extract import digest, read_text

        env_name = self._env_arg()
        try:
            manifest = load_manifest(self.project, env_name)
        except Exception as exc:                      # noqa: BLE001
            return self._json({"error": str(exc)}, 400)

        root = self.project.env_dir(env_name)
        repo = open_repo(self.project)
        uncommitted = set()
        if repo.available:
            # dirty() gives paths relative to the repository root; the
            # manifest keys are relative to the environment directory.
            prefix = repo.rel(root)
            prefix = "" if prefix in (".", "") else prefix + "/"
            for entry in repo.dirty([root]):
                uncommitted.add(entry[len(prefix):] if entry.startswith(prefix)
                                else entry)

        found = []
        for rel, meta in sorted(manifest.items()):
            path = root / rel
            if not path.is_file():
                continue
            try:
                local = read_text(path)
            except OSError:
                continue
            since_pull = digest(local) != meta.get("sha")
            dirty = rel in uncommitted
            if not (since_pull or dirty):
                continue
            why = []
            if since_pull:
                why.append("edited since the pull")
            if dirty:
                why.append("not committed")
            found.append({
                "path": rel,
                "endpoint": meta.get("endpoint"),
                "id": meta.get("id"),
                "name": meta.get("name"),
                "bytes": len(local),
                "since_pull": since_pull,
                "committed": not dirty,
                "why": ", ".join(why),
            })
        return self._json({"env": env_name, "changed": found})

    def _tags(self):
        """Everything the tag builder offers, all of it read from the pull."""
        from . import tags as tagdata

        env_dir = str(self.project.env_dir(self._env_arg()))
        return self._json({
            "types": tagdata.content_types(env_dir),
            "navigation": tagdata.named_objects(env_dir, "navigation"),
            "lists": tagdata.named_objects(env_dir, "list"),
            "vocabulary": tagdata.vocabulary(env_dir),
            "mixing_warning": tagdata.MIXING_WARNING,
        })

    def _diff(self):
        """One file, against the last commit or against T4.

        Two different questions, and the local one gets asked far more often:
        "what have I changed" needs no network and is what you check before
        committing. "What differs from T4" is the deployment question, costs
        a request, and is worth asking deliberately -- so it is the toggle
        rather than the default.
        """
        against = self._query().get("against", ["local"])[0] or "local"
        if against == "local":
            return self._local_diff()
        return self._remote_diff()

    def _local_diff(self):
        """What changed since the last commit, from git.

        git already holds the local baseline. A pull keeps no per-file
        snapshot, so without it there is nothing on disk to compare against
        and the only honest answer would be a request to T4.
        """
        from .opener import Refused, safe_path

        env_name = self._env_arg()
        rel = (self._query().get("path", [""])[0] or "").replace("\\", "/")
        root = self.project.env_dir(env_name)
        try:
            target = safe_path(root, rel)
        except Refused as exc:
            return self._json({"error": str(exc)}, 400)

        repo = open_repo(self.project)
        if not repo.available:
            return self._json({
                "path": rel, "against": "local", "diff": None,
                "error": "%s, so there is no local baseline. Use "
                         "'compare to T4'." % repo.reason})
        sha = self._query().get("sha", [None])[0]
        try:
            if sha:
                # One commit's change to this file.
                return self._json({"path": rel, "against": "local",
                                   "sha": sha, "state": "commit",
                                   "diff": repo.show(sha, target),
                                   "history": repo.log(target)})
            diff, state = repo.diff(target)
            # Always the history too: "no uncommitted changes" is a dead end,
            # and the file plainly changed at some point. When and why is the
            # answer worth having.
            history = repo.log(target)
        except ValueError as exc:
            return self._json({"error": str(exc)}, 400)
        except Exception as exc:                      # noqa: BLE001 - reported
            return self._json({"path": rel, "against": "local", "diff": None,
                               "error": "git failed: %s" % exc})
        return self._json({"path": rel, "against": "local", "diff": diff,
                           "state": state, "identical": not diff.strip(),
                           "history": history})

    def _remote_diff(self):
        """One file against what T4 holds right now.

        Against T4 rather than a local copy, because that is the question
        that matters before deploying and the only one with a truthful
        answer: the pull keeps no per-file snapshot, so anything else would
        be diffing against a guess. One file, one request, on demand -- the
        cost that makes `t4 push` slow over a whole project is fine here.
        """
        import difflib

        from .api import Client
        from .compare import read_field, remote_path
        from .extract import read_text
        from .opener import Refused, safe_path

        env_name = self._env_arg()
        rel = (self._query().get("path", [""])[0] or "").replace("\\", "/")
        root = self.project.env_dir(env_name)
        try:
            target = safe_path(root, rel)
        except Refused as exc:
            return self._json({"error": str(exc)}, 400)

        try:
            manifest = load_manifest(self.project, env_name)
        except Exception as exc:                      # noqa: BLE001
            return self._json({"error": str(exc)}, 400)
        meta = manifest.get(rel)
        if not meta:
            return self._json({"error": "%s is not a pulled record" % rel}, 400)

        local = read_text(target)
        try:
            env = self.project.env(env_name)
            client = Client(env, self.project.require_token(env_name), timeout=30)
            record = client.fetch_path(remote_path(meta))
            remote = read_field(record, meta["field"]) or ""
        except Exception as exc:                      # noqa: BLE001 - reported
            return self._json({
                "path": rel, "meta": meta, "diff": None,
                "error": "Could not reach T4: %s" % exc,
            })

        diff = "".join(difflib.unified_diff(
            remote.splitlines(keepends=True), local.splitlines(keepends=True),
            fromfile="T4  %s" % rel, tofile="local  %s" % rel))
        return self._json({"path": rel, "meta": meta, "diff": diff,
                           "against": "t4", "identical": remote == local})

    def _stream(self, job_id):
        job = self.jobs.get(job_id)
        if not job:
            return self._json({"error": "no such job"}, 404)
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream")
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        channel = job.watch()
        try:
            while True:
                line = channel.get()
                if line is None:
                    self.wfile.write(b"event: end\ndata: %s\n\n"
                                     % json.dumps(job.summary()).encode("utf-8"))
                    self.wfile.flush()
                    return
                if isinstance(line, dict):            # a summary update
                    self.wfile.write(b"event: meta\ndata: %s\n\n"
                                     % json.dumps(line["meta"]).encode("utf-8"))
                    self.wfile.flush()
                    continue
                self.wfile.write(b"data: %s\n\n"
                                 % json.dumps(line).encode("utf-8"))
                self.wfile.flush()
        except (BrokenPipeError, ConnectionResetError):
            pass


# Only commands that make sense from a button, and no `env`/`token`: an
# environment's URL and its credentials are set up once, deliberately, in a
# terminal -- not somewhere a mis-click can reach them.
ALLOWED = {"pull", "push", "save", "mirror", "serve", "status", "diff",
           "sync", "sections", "doctor", "layouts", "compare"}


def run_app(project, port=DEFAULT_PORT, host="127.0.0.1", open_browser=True):
    AppHandler.project = project
    AppHandler.jobs = Jobs(str(project.root))
    AppHandler.token = secrets.token_urlsafe(24)

    url = "http://%s:%d/?t=%s" % (host, port, AppHandler.token)
    with ThreadingHTTPServer((host, port), AppHandler) as httpd:
        httpd.daemon_threads = True
        print("t4-sync control panel")
        print("Project   %s" % project.root)
        print("URL       %s" % url)
        print("\nThe token in that URL is what authorizes the page; it is new")
        print("each time this starts. Ctrl-C to stop.")
        if open_browser:
            threading.Thread(target=webbrowser.open, args=(url,),
                             daemon=True).start()
        try:
            httpd.serve_forever()
        except KeyboardInterrupt:
            print("\nStopping...")
            for job in AppHandler.jobs.running():
                job.stop()
            print("Stopped.")
    return 0


FORBIDDEN = """<!doctype html><meta charset="utf-8">
<title>t4-sync</title>
<style>body{font:14px/1.6 system-ui,sans-serif;margin:3rem auto;max-width:34rem;
padding:0 1rem;color:#1a1d25}code{background:#f1f3f7;padding:.1rem .3rem;
border-radius:3px}</style>
<h1>Not authorized</h1>
<p>This page needs the one-time token printed by <code>t4 app</code>. Open the
full URL from that command's output rather than typing the address by hand.</p>
<p>The token exists so that another page in your browser cannot reach this one
and start a deployment.</p>
"""


PAGE = r"""<!doctype html>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>t4-sync</title>
<style>
:root{color-scheme:light dark;--bg:#fff;--fg:#1a1d25;--dim:#5a6376;
 --line:#e2e5ec;--card:#f7f8fa;--accent:#2b4bbf;--warn:#9a3412;--ok:#17603a;
 /* Buttons get their own tokens, measured rather than eyeballed: the
    border carries the affordance, so it clears 3:1 against the card it
    sits on (WCAG 1.4.11), and the label clears 4.5:1 on the face. With a
    1.19:1 boundary they read as disabled text inputs, which is what they
    looked like. */
 --btn:#e9edf6;--btn-edge:#6f7c9c;--btn-fg:#15181f;
 --btn-hover:#dbe3f4;--btn-active:#ccd7ee;--on-accent:#fff}
@media (prefers-color-scheme:dark){:root{--bg:#12141a;--fg:#e8eaf0;
 --dim:#8b93a7;--line:#262b36;--card:#191c24;--accent:#9fb4ff;--warn:#fca5a5;
 --ok:#8ff0a6;
 --btn:#252b38;--btn-edge:#687490;--btn-fg:#e8eaf0;
 --btn-hover:#2f374a;--btn-active:#39435a;--on-accent:#12141a}}
*{box-sizing:border-box}
body{margin:0;background:var(--bg);color:var(--fg);
 font:14px/1.55 ui-sans-serif,system-ui,-apple-system,"Segoe UI",sans-serif}
/* Sticky, so switching environment or refreshing stays reachable without
   trapping the content in a pane of its own. */
header{display:flex;align-items:center;gap:1rem;flex-wrap:wrap;
 position:sticky;top:0;z-index:5;background:var(--bg);
 padding:.6rem 1rem;border-bottom:1px solid var(--line)}
h1{font-size:15px;margin:0;letter-spacing:.02em}
h2{font-size:13px;margin:0 0 .5rem;color:var(--dim);font-weight:600;
 text-transform:uppercase;letter-spacing:.06em}
/* What you can do, across the top; what changed beside what happened; the
   log across the bottom where a long one does no harm. The page does the
   scrolling -- panes that scroll inside a scrolling page make the wheel
   behave differently depending on where the pointer is. */
main{display:grid;gap:.75rem;padding:.75rem;align-items:start;
 grid-template-columns:minmax(0,70fr) minmax(0,30fr);
 grid-template-areas:"project project" "changes history" "build build"
                     "tasks tasks"}
.area-project h2{margin-top:.6rem}
.area-project{padding-bottom:.85rem}
.area-project{grid-area:project}
.area-changes{grid-area:changes}
.area-history{grid-area:history}
.area-build{grid-area:build}
.area-tasks{grid-area:tasks}
.build{display:grid;gap:.5rem;
 grid-template-columns:repeat(auto-fit,minmax(11rem,1fr))}
.build label{display:flex;flex-direction:column;gap:.15rem;font-size:11px;
 color:var(--dim);min-width:0}
.build select,.build input{width:100%%;font-size:12px}
.build input{background:var(--bg);color:var(--fg);border:1px solid var(--btn-edge);
 border-radius:6px;padding:.3rem .5rem}
.build label[hidden]{display:none}
.mods{display:flex;flex-wrap:wrap;gap:.3rem .7rem;margin:.5rem 0 .2rem;
 font-size:11.5px}
.mods label{display:flex;gap:.3rem;align-items:center;cursor:pointer}
.tagout{margin-top:.4rem}
.taghead{display:flex;justify-content:space-between;align-items:baseline;
 font-size:11px;margin:.5rem 0 .2rem}
.tagcode{display:block;background:#0b0d12;color:#dbe1ee;border-radius:6px;
 padding:.5rem .6rem;font-size:12px;white-space:pre-wrap;word-break:break-all;
 font-family:ui-monospace,SFMono-Regular,Menlo,monospace}
.t-tag{color:#f7768e}
.t-attr{color:#e0af68}
.t-string{color:#9ece6a}
.t-hbs{color:#bb9af7;font-weight:600}
.t-comment{color:#7d8799;font-style:italic}
@media (max-width:980px){
 main{grid-template-columns:minmax(0,1fr);
  grid-template-areas:"project" "changes" "history" "build" "tasks"}
}

/* Project details are reference, not the thing you came for. */
#details summary{cursor:pointer;list-style:none;display:flex;gap:.5rem;
 align-items:baseline;padding:.1rem 0 .3rem}
#details summary::-webkit-details-marker{display:none}
#details summary::before{content:"\25b8";color:var(--dim);
 display:inline-block;transition:transform .15s}
#details[open] summary::before{transform:rotate(90deg)}
#details[open] #summary{padding-bottom:.4rem;border-bottom:1px solid var(--line)}
.card{background:var(--card);border:1px solid var(--line);border-radius:8px;
 padding:.7rem .85rem}
.row{display:flex;gap:.6rem;align-items:baseline;padding:.25rem 0;
 border-top:1px solid var(--line)}
.row:first-of-type{border-top:0}
.key{flex:0 0 7.5rem;color:var(--dim)}
.val{flex:1 1 auto;min-width:0;word-break:break-word}
button{font:inherit;font-weight:500;color:var(--btn-fg);background:var(--btn);
 border:1px solid var(--btn-edge);border-radius:6px;padding:.3rem .7rem;
 cursor:pointer;line-height:1.3}
button:hover:not(:disabled){background:var(--btn-hover);
 border-color:var(--accent);color:var(--btn-fg)}
button:active:not(:disabled){background:var(--btn-active)}
/* Never only a colour change: disabled is also flatter and unclickable. */
button:disabled{opacity:.5;cursor:not-allowed;background:transparent;
 border-style:dashed}
button.primary{background:var(--accent);color:var(--on-accent);
 border-color:var(--accent)}
button.primary:hover:not(:disabled){background:var(--accent);
 color:var(--on-accent);filter:brightness(1.12)}
button.danger{border-color:var(--warn);color:var(--warn)}
button.danger:hover:not(:disabled){color:var(--warn)}
/* Keyboard focus has to be visible on every surface, including the fills. */
button:focus-visible,select:focus-visible,input:focus-visible,
summary:focus-visible{outline:2px solid var(--accent);outline-offset:2px}
select{font:inherit;color:var(--fg);background:var(--bg);
 border:1px solid var(--btn-edge);border-radius:6px;padding:.3rem .5rem;
 cursor:pointer}
.actions{display:flex;flex-wrap:wrap;gap:.4rem;margin-top:.6rem}
/* One action per row, with what it does beside it: the buttons are not
   self-explanatory and a tooltip is no use to someone deciding. */
/* Three columns, one per stage, each a stack of what you do at that stage.
   The order is the order you work in, so the eye has somewhere to start.
   Capped at 300px: a button stretched across a wide screen is harder to
   aim at, not easier, and the row has no more meaning for being wider. */
.phases{display:grid;gap:.9rem;margin-top:.4rem;justify-content:start;
 grid-template-columns:repeat(3,minmax(0,300px))}
@media (max-width:980px){
 .phases{grid-template-columns:repeat(3,minmax(0,1fr))}
}
@media (max-width:760px){.phases{grid-template-columns:minmax(0,1fr)}}
.phase h3{margin:0 0 .3rem;font-size:11px;color:var(--dim);font-weight:600;
 text-transform:uppercase;letter-spacing:.06em}
.acts{display:flex;flex-direction:column;gap:.25rem}
/* Button plus a ? that opens what it does. The explanations were worth
   having but not worth a third of the column every time. */
.act{position:relative;display:flex;gap:.25rem;align-items:stretch}
.act > button:first-child{flex:1 1 auto;text-align:left;padding:.25rem .5rem;
 display:flex;align-items:center;gap:.45rem}
/* Sized in ems so it tracks the label, and never squeezed by a long one. */
.ic{flex:0 0 auto;width:1.05em;height:1.05em;opacity:.85}
button:hover:not(:disabled) .ic{opacity:1}
.act > button:first-child span{flex:1 1 auto;min-width:0;overflow:hidden;
 text-overflow:ellipsis;white-space:nowrap}
.qm{flex:0 0 auto;width:1.6rem;padding:.25rem 0;text-align:center;
 color:var(--btn-fg);line-height:1;font-weight:700}
.qm:hover:not(:disabled){color:var(--accent)}
.act.open .qm{background:var(--accent);color:#fff;border-color:var(--accent)}
.help{display:none;position:absolute;top:calc(100%% + .3rem);left:0;right:0;
 z-index:20;background:var(--bg);border:1px solid var(--accent);
 border-radius:6px;padding:.5rem .6rem;font-size:12px;line-height:1.5;
 color:var(--fg);box-shadow:0 6px 20px rgba(0,0,0,.18)}
.act.open .help{display:block}
.help code{background:var(--card);padding:.05rem .25rem;border-radius:3px}
@media (max-width:980px){.help{position:static;margin:.2rem 0 .4rem}}
.when{display:flex;gap:.5rem;align-items:baseline;padding:.25rem 0;
 border-top:1px solid var(--line)}
.when:first-child{border-top:0}
.when .what{flex:0 0 6.5rem;color:var(--dim)}
.when .ago{flex:1 1 auto}
.when .detail{color:var(--dim);font-size:12px}
.never{color:var(--dim);font-style:italic}
.chg{display:flex;gap:.5rem;align-items:baseline;padding:.3rem 0;
 border-top:1px solid var(--line);cursor:pointer}
.chg:first-child{border-top:0}
.chg:hover{color:var(--accent)}
.chg.on{background:rgba(43,75,191,.07);border-radius:4px}
.chg input{cursor:pointer;margin:0}
.chg .p{flex:1 1 auto;min-width:0;word-break:break-all;font-family:ui-monospace,
 SFMono-Regular,Menlo,monospace;font-size:12px}
.chg .rec{flex:0 0 auto;color:var(--dim);font-size:11px}
.chg .tog{flex:0 0 auto;padding:.1rem .5rem;font-size:11px}
.chg .one{color:var(--accent);font-weight:600}
.chg .tog .caret{display:inline-block;transition:transform .15s}
.chg.open .tog .caret{transform:rotate(90deg)}
.dwrap{padding:0 0 .5rem 1.6rem}
.dwrap[hidden]{display:none}
.dbar{display:flex;gap:.5rem;align-items:baseline;padding:.1rem 0 .35rem;
 font-size:11px}
.dbar .dnote{flex:1 1 auto;min-width:0}
.hist{margin-top:.5rem;border-top:1px solid var(--line)}
.histhead{font-size:11px;padding:.35rem 0 .2rem}
/* A log row is a list item you can click, not a control to press. */
.histrow{display:flex;gap:.5rem;align-items:baseline;width:100%%;
 text-align:left;background:none;border:0;border-left:2px solid transparent;
 border-radius:0;padding:.2rem .3rem;font-size:11.5px;cursor:pointer;
 color:var(--fg);font-weight:400}
.histrow:hover{background:rgba(43,75,191,.1);color:var(--fg);
 border-left-color:var(--accent);border-color:transparent;
 border-left-color:var(--accent)}
.histrow.on{background:rgba(43,75,191,.14)}
.histrow .when{flex:0 0 5.5rem;font-family:ui-monospace,monospace}
.histrow .subj{flex:1 1 auto;min-width:0;overflow:hidden;
 text-overflow:ellipsis;white-space:nowrap}
.histrow .who{flex:0 0 auto;font-family:ui-monospace,monospace;font-size:10.5px}
/* A git-style diff: the shape is the information, so keep the gutters. */
.diff{margin:0;font-family:ui-monospace,SFMono-Regular,Menlo,monospace;
 font-size:12px;line-height:1.45;overflow-x:auto;
 border:1px solid var(--line);border-radius:6px}
.diff div{padding:0 .5rem;white-space:pre-wrap;word-break:break-word}
.diff .add{background:rgba(25,135,84,.16)}
.diff .del{background:rgba(220,53,69,.16)}
.diff .hunk{background:rgba(43,75,191,.12);color:var(--accent)}
.diff .meta{color:var(--dim)}
@media (prefers-color-scheme:dark){
 .diff .add{background:rgba(143,240,166,.14)}
 .diff .del{background:rgba(252,165,165,.14)}
}
code,pre{font-family:ui-monospace,SFMono-Regular,Menlo,monospace}
pre{margin:0;padding:.6rem .7rem;background:#0b0d12;color:#dbe1ee;
 border-radius:6px;max-height:28rem;overflow:auto;font-size:12px;
 white-space:pre-wrap;word-break:break-word}
.dim{color:var(--dim)}
.ok{color:var(--ok)}
.warn{color:var(--warn)}
.pill{display:inline-block;padding:.05rem .45rem;border-radius:999px;
 border:1px solid var(--line);font-size:11px;color:var(--dim)}
.task{cursor:pointer;align-items:center}
.task:hover{color:var(--accent)}
.task.on{font-weight:600}
.task button{padding:.1rem .5rem;font-size:11px}
.dot{flex:0 0 auto;width:8px;height:8px;border-radius:50%%;background:var(--dim)}
.dot.ok{background:var(--ok)}
.dot.bad{background:var(--warn)}
.dot.live{background:var(--accent);animation:pulse 1.4s ease-in-out infinite}
@keyframes pulse{0%%,100%%{opacity:1}50%%{opacity:.25}}
footer{padding:.5rem 1rem 1.5rem;color:var(--dim);font-size:11px}
.legal{margin-top:.35rem;opacity:.8}
</style>

<header>
  <h1>t4-sync</h1>
  <label>Environment
    <select id="env"></select>
  </label>
  <span id="envnote" class="pill"></span>
  <span style="flex:1 1 auto"></span>
  <button id="refresh">Refresh</button>
</header>

<main>
  <section class="card area-project">
    <details id="details">
      <summary><b>Project</b> <span id="detailsline" class="dim"></span></summary>
      <div id="summary"></div>
    </details>

    <div class="phases">
      <div class="phase">
        <h3>1 &middot; Get the files</h3>
        <div class="acts">
      <div class="act">
        <button data-run='["pull"]'><svg class="ic" viewBox="0 0 24 24" fill="none" aria-hidden="true" stroke="currentColor" stroke-width="1.7" stroke-linecap="round" stroke-linejoin="round"><path d="M12 3v10m0 0 4-4m-4 4-4-4M4 17v2a2 2 0 0 0 2 2h12a2 2 0 0 0 2-2v-2"/></svg><span>Pull source</span></button>
        <button class="qm" type="button" aria-label="What this does">?</button>
        <div class="help">Downloads the templates from the CMS &mdash; page layouts, content layouts, content types and the Media Library CSS and JS. These are the files you edit. Needs the CMS reachable, so the VPN if you use one. It will not overwrite a file you have edited since the last pull.</div>
      </div>
      <div class="act">
        <button data-run='["mirror"]'><svg class="ic" viewBox="0 0 24 24" fill="none" aria-hidden="true" stroke="currentColor" stroke-width="1.7" stroke-linecap="round" stroke-linejoin="round"><path d="M3 12a9 9 0 1 0 18 0 9 9 0 0 0-18 0m9-9a14 14 0 0 1 0 18a14 14 0 0 1 0-18M3.5 9h17M3.5 15h17"/></svg><span>Mirror site</span></button>
        <button class="qm" type="button" aria-label="What this does">?</button>
        <div class="help">Crawls the <em>published</em> site and saves the finished pages, so there is something real to look at. A different server from the pull. Only needed when the published pages have changed a lot &mdash; editing templates does not require a fresh mirror.</div>
      </div>
      <div class="act">
        <button data-run='["sections"]'><svg class="ic" viewBox="0 0 24 24" fill="none" aria-hidden="true" stroke="currentColor" stroke-width="1.7" stroke-linecap="round" stroke-linejoin="round"><path d="M9 4h6v4H9zM3 16h6v4H3zM15 16h6v4h-6zM12 8v4M6 16v-2h12v2"/></svg><span>Map sections</span></button>
        <button class="qm" type="button" aria-label="What this does">?</button>
        <div class="help">Walks the site tree recording which layout each section uses and which content items sit in it. This is what lets the inspector name exact files instead of guessing. Slow &mdash; a few thousand requests &mdash; but it resumes where it left off and is only needed occasionally.</div>
      </div>
      <div class="act">
        <button data-run='["sync"]'><svg class="ic" viewBox="0 0 24 24" fill="none" aria-hidden="true" stroke="currentColor" stroke-width="1.7" stroke-linecap="round" stroke-linejoin="round"><path d="M3 12a9 9 0 0 1 15-6.7L21 8M21 4v4h-4M21 12a9 9 0 0 1-15 6.7L3 16M3 20v-4h4"/></svg><span>Sync</span></button>
        <button class="qm" type="button" aria-label="What this does">?</button>
        <div class="help">Pulls, then reports what changed on each side. Never writes to the CMS. The usual way to start a session.</div>
      </div>
    </div>
      </div>
      <div class="phase">
        <h3>2 &middot; Work on it</h3>
        <div class="acts">
      <div class="act">
        <button data-run='["serve"]'><svg class="ic" viewBox="0 0 24 24" fill="none" aria-hidden="true" stroke="currentColor" stroke-width="1.7" stroke-linecap="round" stroke-linejoin="round"><path d="M8 5.5v13l10-6.5z"/></svg><span>Start local site</span></button>
        <button class="qm" type="button" aria-label="What this does">?</button>
        <div class="help">Runs the site on your machine from the mirrored pages, with your edits applied and the inspector switched on. It keeps running until you stop it, and everything else here still works meanwhile.</div>
      </div>
      <div class="act">
        <button id="openserve" disabled><svg class="ic" viewBox="0 0 24 24" fill="none" aria-hidden="true" stroke="currentColor" stroke-width="1.7" stroke-linecap="round" stroke-linejoin="round"><path d="M14 4h6v6M20 4l-9 9M18 14v5a2 2 0 0 1-2 2H6a2 2 0 0 1-2-2V8a2 2 0 0 1 2-2h5"/></svg><span>View site</span></button>
        <button class="qm" type="button" aria-label="What this does">?</button>
        <div class="help">Opens the local site in a new tab. It becomes available a moment after the server reports its address.</div>
      </div>
      <div class="act">
        <button data-run='["status"]'><svg class="ic" viewBox="0 0 24 24" fill="none" aria-hidden="true" stroke="currentColor" stroke-width="1.7" stroke-linecap="round" stroke-linejoin="round"><path d="M4 6h2M4 12h2M4 18h2M9 6h11M9 12h11M9 18h7"/></svg><span>Status</span></button>
        <button class="qm" type="button" aria-label="What this does">?</button>
        <div class="help">What is pending, grouped by kind, with the repository state. Read only &mdash; it changes nothing.</div>
      </div>
      <div class="act">
        <button data-run='["diff"]'><svg class="ic" viewBox="0 0 24 24" fill="none" aria-hidden="true" stroke="currentColor" stroke-width="1.7" stroke-linecap="round" stroke-linejoin="round"><path d="M4 5h7v14H4zM13 5h7v14h-7M15 10h3M15 14h3M6 12h3"/></svg><span>What differs</span></button>
        <button class="qm" type="button" aria-label="What this does">?</button>
        <div class="help">Compares every file against what the CMS holds right now and lists those that differ. Read only, and slow over a VPN: one request per record. For a single file, use the diff button in the Changes list.</div>
      </div>
    </div>
      </div>
      <div class="phase">
        <h3>3 &middot; Ship it</h3>
        <div class="acts">
      <div class="act">
        <button data-run='["save"]'><svg class="ic" viewBox="0 0 24 24" fill="none" aria-hidden="true" stroke="currentColor" stroke-width="1.7" stroke-linecap="round" stroke-linejoin="round"><path d="M3 12h5m8 0h5M12 8.5a3.5 3.5 0 1 0 0 7 3.5 3.5 0 0 0 0-7"/></svg><span>Commit to git</span></button>
        <button class="qm" type="button" aria-label="What this does">?</button>
        <div class="help">Records your edits in the repository, with a message naming the CMS record behind each file. Whether a push requires this first is set per environment in <code>.t4/config.json</code>.</div>
      </div>
      <div class="act">
        <button id="push" class="primary"><svg class="ic" viewBox="0 0 24 24" fill="none" aria-hidden="true" stroke="currentColor" stroke-width="1.7" stroke-linecap="round" stroke-linejoin="round"><path d="M12 21V9m0 0 4 4m-4-4-4 4M4 7V5a2 2 0 0 1 2-2h12a2 2 0 0 1 2 2v2"/></svg><span>Push to T4</span></button>
        <button class="qm" type="button" aria-label="What this does">?</button>
        <div class="help">Sends the ticked files to the CMS, backing up each record first. Only environments marked writable accept it. To send one file on its own, use the push button on its row in Changes.</div>
      </div>
    </div>
      </div>
    </div>
  </section>

  <section class="card area-changes">
    <h2>Changes
      <span id="changecount" class="pill"></span>
      <button id="rescan" style="float:right">Rescan</button>
    </h2>
    <p class="dim" style="margin:.1rem 0 .5rem;font-size:12px">
      Files you have edited since the last pull. <b>diff</b> opens what would
      change since your last commit, with a toggle to compare against what T4
      holds instead. <b>push</b> deploys that one file; tick several and use
      Push to T4 to send them together.
    </p>
    <div id="changes"></div>
  </section>

  <section class="card area-history">
    <h2>History</h2>
    <div id="events"></div>
    <h2 style="margin-top:1rem">Recent activity</h2>
    <div id="recent"></div>
  </section>

  <section class="card area-build">
    <h2>Tag builder <span id="tagcount" class="pill"></span></h2>
    <p class="dim" style="margin:.1rem 0 .5rem;font-size:12px">
      Builds a T4 tag from this project's own content types, so the element
      name and the attributes are the ones that exist. Composes only &mdash;
      T4 evaluates tags at publish time, nothing local can.
    </p>
    <div class="build">
      <label>Tag<select id="tg-type"></select></label>
      <label id="tg-ctwrap">Content type<select id="tg-ct"></select></label>
      <label id="tg-elwrap">Element<select id="tg-el"></select></label>
      <label id="tg-objwrap" hidden>Object<select id="tg-obj"></select></label>
      <label id="tg-metawrap" hidden>Meta<select id="tg-meta"></select></label>
      <label id="tg-outwrap">Output<select id="tg-out"></select></label>
      <label>Date format
        <input id="tg-date" type="text" placeholder="dd MMMM yyyy">
      </label>
    </div>
    <div id="tg-mods" class="mods"></div>
    <div class="tagout">
      <div class="taghead"><span class="dim">T4 tag</span>
        <button class="tog" data-copytag="tag">copy</button></div>
      <code id="tg-tag" class="tagcode"></code>
      <div class="taghead"><span class="dim">Handlebars</span>
        <button class="tog" data-copytag="hbs">copy</button></div>
      <code id="tg-hbs" class="tagcode"></code>
      <p id="tg-note" class="dim" style="margin:.35rem 0 0;font-size:11.5px"></p>
    </div>
  </section>

  <section class="card area-tasks">
    <h2>Tasks</h2>
    <div id="tasks"><p class="dim" style="margin:.2rem 0">Nothing running.</p></div>
    <h2 style="margin-top:1rem">Output
      <span id="jobstate" class="pill"></span>
      <button id="stop" class="danger" style="float:right" disabled>Stop</button>
    </h2>
    <pre id="log">Pick an action to begin.</pre>
  </section>
</main>

<footer><div id="foot"></div><div id="legal" class="legal"></div></footer>

<script>
var TOKEN = "%(token)s";
var state = null;

// Tasks run side by side. A long `t4 serve` must not stop you pulling,
// committing or pushing while it runs -- that is the normal way to work.
// Each job keeps its own log; the pane shows whichever is selected.
var jobs = {};          // id -> {summary, lines, source}
var shown = null;       // id of the job whose log is on screen

function api(path, options) {
  options = options || {};
  options.headers = Object.assign({"X-T4-Token": TOKEN,
                                   "Content-Type": "application/json"},
                                  options.headers || {});
  return fetch(path, options).then(function (r) {
    if (!r.ok) return r.json().then(function (d) { throw new Error(d.error || r.status); });
    return r.json();
  });
}

function el(id) { return document.getElementById(id); }
function text(s) { return document.createTextNode(s == null ? "" : String(s)); }
var text_ = text;   // alias, for scopes with a local named `text`

function row(key, valueNode) {
  var d = document.createElement("div"); d.className = "row";
  var k = document.createElement("span"); k.className = "key"; k.appendChild(text(key));
  var v = document.createElement("span"); v.className = "val";
  v.appendChild(typeof valueNode === "string" ? text(valueNode) : valueNode);
  d.appendChild(k); d.appendChild(v);
  return d;
}

function envByName(name) {
  for (var i = 0; i < state.environments.length; i++) {
    if (state.environments[i].name === name) return state.environments[i];
  }
  return null;
}

function selected() { return el("env").value; }

function render() {
  var pick = el("env");
  var keep = pick.value || state.default_env;
  pick.innerHTML = "";
  state.environments.forEach(function (e) {
    var o = document.createElement("option");
    o.value = e.name; o.textContent = e.label || e.name;
    pick.appendChild(o);
  });
  if (keep) pick.value = keep;

  var env = envByName(pick.value) || {};
  var note = el("envnote");
  note.textContent = env.push_allowed ? "writable" : "pull-only";
  note.className = "pill " + (env.push_allowed ? "warn" : "");

  var box = el("summary");
  box.innerHTML = "";
  box.appendChild(row("Project", state.root));
  box.appendChild(row("CMS", env.url || "not configured"));
  box.appendChild(row("Published", env.published_url || "not configured"));
  box.appendChild(row("Token", env.has_token ? "stored" : "missing - run: t4 token set " + env.name));
  box.appendChild(row("Source", env.tracked ? env.tracked + " files tracked"
                                            : "nothing pulled yet"));
  box.appendChild(row("Mirror", state.mirror
    ? state.mirror.pages + " pages (" + state.mirror.host + ")"
    : "none - Mirror site, or just start the local site"));
  box.appendChild(row("Repository", state.repo.summary));
  box.appendChild(row("Before push", env.push_allowed
    ? (env.requires_commit
        ? "a commit is required"
        : "a commit is not required for this environment")
    : "pushing is disabled for this environment"));

  el("push").title = env.push_allowed ? ""
    : "Pushing is disabled for this environment in .t4/config.json";

  var line = el("detailsline");
  if (line) {
    line.textContent = [
      env.tracked ? env.tracked + " files" : "nothing pulled",
      state.mirror ? state.mirror.pages + " pages mirrored" : "no mirror",
      state.repo.available ? state.repo.summary : "no repository"
    ].join("  \u00b7  ");
  }

  renderHistory(env.name);
  el("foot").textContent = "Every button runs the matching t4 command as a "
    + "subprocess in " + state.root + ". Tasks run side by side; only "
    + "commands writing the same files wait for each other.";
  var legal = el("legal");
  if (legal) {
    legal.textContent = "t4-sync is an independent, unofficial tool. Not "
      + "produced, endorsed or supported by TERMINALFOUR Solutions Ltd. "
      + "\u201cT4\u201d and \u201cTERMINALFOUR\u201d are their trademarks.";
  }
}

function load() {
  return api("/api/state").then(function (s) {
    state = s;
    // Reloading the page must not lose a serve started ten minutes ago, nor
    // orphan its log: re-attach to anything still running.
    s.jobs.forEach(function (summary) {
      var entry = jobs[summary.id];
      if (!entry) {
        if (summary.done) return;       // finished before we ever saw it
        track(summary);
      } else {
        entry.summary = summary;
      }
    });
    render();
    syncButtons();
    renderTasks();
    loadChanges();
    if (!tagData) loadTagData();
  });
}

// "When did we last pull?" is the question people actually ask, and neither
// the CLI nor T4 could answer it before. One line per kind of event, so it
// says which of them it was rather than collapsing to "last activity".
var EVENTS = [
  ["pull", "Pulled", function (e) {
    return e.files ? e.files + " files tracked" : null; }],
  ["push", "Pushed", function (e) {
    return (e.files || 0) + " deployed"
         + (e.commit ? " from " + e.commit.slice(0, 8) : "")
         + (e.failures ? ", " + e.failures + " failed" : ""); }],
  ["save", "Committed", function (e) {
    return (e.commit ? e.commit.slice(0, 8) + " - " : "")
         + (e.message || e.files + " files"); }],
  ["edit", "Edited", function (e) { return e.file || null; }],
  ["mirror", "Mirrored", function (e) {
    return e.pages ? e.pages + " pages" : null; }],
  ["sections", "Mapped", function (e) {
    return e.sections ? e.sections + " sections" : null; }]
];

function whenRow(label, when, detail) {
  var d = document.createElement("div"); d.className = "when";
  var k = document.createElement("span"); k.className = "what";
  k.appendChild(text(label));
  var v = document.createElement("span"); v.className = "ago";
  if (when) {
    v.appendChild(text(when));
    if (detail) {
      var small = document.createElement("div");
      small.className = "detail"; small.appendChild(text(detail));
      v.appendChild(small);
    }
  } else {
    var never = document.createElement("span");
    never.className = "never"; never.appendChild(text("never"));
    v.appendChild(never);
  }
  d.appendChild(k); d.appendChild(v);
  return d;
}

function renderHistory(envName) {
  var events = (state.events || {})[envName] || {};
  var box = el("events");
  box.innerHTML = "";
  EVENTS.forEach(function (spec) {
    var entry = events[spec[0]];
    box.appendChild(whenRow(spec[1], entry && entry.ago,
                            entry ? spec[2](entry) : null));
  });

  var recent = el("recent");
  recent.innerHTML = "";
  var rows = (state.recent || []).filter(function (e) {
    return !e.env || e.env === envName;
  });
  if (!rows.length) {
    var p = document.createElement("p");
    p.className = "dim"; p.style.margin = ".2rem 0";
    p.appendChild(text("Nothing recorded yet. Actions you run here and in the "
                     + "terminal both show up."));
    recent.appendChild(p);
    return;
  }
  rows.forEach(function (e) {
    var spec = null;
    EVENTS.forEach(function (s) { if (s[0] === e.action) spec = s; });
    recent.appendChild(whenRow(spec ? spec[1] : e.action, e.ago,
                               spec ? spec[2](e) : null));
  });
}

function busyLanes() {
  var busy = {};
  Object.keys(jobs).forEach(function (id) {
    var j = jobs[id].summary;
    if (!j.done && j.lane) busy[j.lane] = j.label;
  });
  return busy;
}

function laneFor(command) { return (state && state.lanes || {})[command]; }

// A button is disabled only when something writing the same thing is
// running. Serving the site blocks nothing but another serve.
function syncButtons() {
  var busy = busyLanes();
  document.querySelectorAll("button[data-run]").forEach(function (b) {
    var command = JSON.parse(b.getAttribute("data-run"))[0];
    var lane = laneFor(command);
    var blocker = lane && busy[lane];
    b.disabled = !!blocker;
    b.title = blocker ? blocker + " is running, and both write the same files"
                      : "";
  });

  var serving = null;
  Object.keys(jobs).forEach(function (id) {
    var j = jobs[id].summary;
    if (!j.done && j.argv[0] === "serve") serving = j;
  });
  var open = el("openserve");
  open.disabled = !(serving && serving.url);
  open.dataset.url = serving && serving.url ? serving.url : "";
  open.title = serving
    ? (serving.url ? "Open " + serving.url : "Waiting for the server to report its address")
    : "Start the local site first";

  var env = envByName(selected()) || {};
  var blocked = !env.push_allowed || !!busy[laneFor("push")];
  el("push").disabled = blocked;
  document.querySelectorAll("button[data-pushone]").forEach(function (b) {
    b.disabled = blocked;
    b.title = env.push_allowed ? "Push just this file"
      : "Pushing is disabled for this environment";
  });

  var start = document.querySelector('button[data-run=\'["serve"]\']');
  // The label only: setting textContent on the button would delete its icon.
  var startLabel = start && start.querySelector("span");
  if (startLabel) {
    startLabel.textContent = serving ? "Site is running" : "Start local site";
  }
}

// "exit 0" is a shell detail. The question is whether it worked.
function outcome(job) {
  if (!job.done) return "running";
  if (job.returncode === 0) return "done";
  if (job.returncode === null) return "stopped";
  if (job.returncode < 0 || job.returncode === 143 || job.returncode === 130) {
    return "stopped";
  }
  if (job.returncode === 2) return "refused";
  return "failed";
}

function renderTasks() {
  var box = el("tasks");
  box.innerHTML = "";
  var ids = Object.keys(jobs).sort(function (a, b) { return b - a; });
  var live = ids.filter(function (id) { return !jobs[id].summary.done; });
  if (!ids.length) {
    var p = document.createElement("p");
    p.className = "dim"; p.style.margin = ".2rem 0";
    p.appendChild(text("Nothing running."));
    box.appendChild(p);
    return;
  }
  ids.slice(0, 8).forEach(function (id) {
    var j = jobs[id].summary;
    var d = document.createElement("div");
    d.className = "row task" + (id === shown ? " on" : "");
    d.dataset.job = id;

    var dot = document.createElement("span");
    dot.className = "dot " + (!j.done ? "live" : (j.returncode === 0 ? "ok" : "bad"));
    var name = document.createElement("span");
    name.className = "val"; name.appendChild(text(j.label));
    var status = document.createElement("span");
    status.className = "pill " + (!j.done ? "" : (j.returncode === 0 ? "ok" : "warn"));
    status.appendChild(text(outcome(j)));

    d.appendChild(dot); d.appendChild(name); d.appendChild(status);
    if (!j.done) {
      var stop = document.createElement("button");
      stop.className = "danger"; stop.dataset.stop = id;
      stop.appendChild(text("stop"));
      d.appendChild(stop);
    }
    box.appendChild(d);
  });
  el("jobstate").textContent = live.length
    ? live.length + " running" : "idle";
}

function showLog(id) {
  shown = id;
  var entry = jobs[id];
  el("log").textContent = entry ? entry.lines.join("\n") : "";
  el("log").scrollTop = el("log").scrollHeight;
  el("stop").disabled = !(entry && !entry.summary.done);
  renderTasks();
}

function track(summary) {
  var entry = jobs[summary.id];
  if (!entry) {
    entry = jobs[summary.id] = {summary: summary, lines: [], source: null};
  } else {
    entry.summary = summary;
  }
  if (!entry.source && !summary.done) stream(summary.id);
  return entry;
}

function stream(id) {
  var entry = jobs[id];
  var source = new EventSource("/api/jobs/" + id + "/stream?t=" + TOKEN);
  entry.source = source;
  source.onmessage = function (e) {
    entry.lines.push(JSON.parse(e.data));
    if (shown === id) {
      var log = el("log");
      var atBottom = log.scrollTop + log.clientHeight >= log.scrollHeight - 24;
      log.appendChild(text(entry.lines[entry.lines.length - 1] + "\n"));
      if (atBottom) log.scrollTop = log.scrollHeight;
    }
  };
  source.addEventListener("meta", function (e) {
    // The serve URL arrives a second or two after the job starts; without
    // this the View site button stays dead for as long as the site is up.
    entry.summary = JSON.parse(e.data);
    syncButtons(); renderTasks();
  });
  source.addEventListener("end", function (e) {
    source.close(); entry.source = null;
    entry.summary = JSON.parse(e.data);
    syncButtons(); renderTasks();
    if (shown === id) el("stop").disabled = true;
    load();
  });
  source.onerror = function () { source.close(); entry.source = null; };
}

function run(argv, opts) {
  opts = opts || {};
  argv = argv.slice();
  // Every command here takes --env, except the ones that do not accept it.
  if (["pull", "push", "save", "mirror", "sync", "sections", "status",
       "diff", "serve", "doctor", "layouts"].indexOf(argv[0]) !== -1) {
    argv.push("--env", selected());
  }
  if (opts.extra) argv = argv.concat(opts.extra);

  return api("/api/run", {method: "POST", body: JSON.stringify({argv: argv})})
    .then(function (summary) {
      track(summary);
      showLog(summary.id);
      syncButtons();
      return summary;
    })
    .catch(function (e) {
      // A conflict is a timing problem, not a failure: say so without
      // throwing away the log of whatever is already running.
      el("jobstate").textContent = e.message;
      el("jobstate").className = "pill warn";
    });
}

// The ? panels: one open at a time, dismissed by clicking away or Escape.
document.addEventListener("click", function (e) {
  var qm = e.target.closest(".qm");
  var act = qm && qm.closest(".act");
  document.querySelectorAll(".act.open").forEach(function (open) {
    if (open !== act) open.classList.remove("open");
  });
  if (qm) {
    e.preventDefault();
    act.classList.toggle("open");
    return;
  }
  if (e.target.closest(".help")) return;    // clicking inside it is not a dismiss
});

document.addEventListener("keydown", function (e) {
  if (e.key !== "Escape") return;
  document.querySelectorAll(".act.open").forEach(function (open) {
    open.classList.remove("open");
  });
});

document.addEventListener("click", function (e) {
  var b = e.target.closest("button[data-run]");
  if (b) { run(JSON.parse(b.getAttribute("data-run"))); return; }

  var stop = e.target.closest("button[data-stop]");
  if (stop) {
    e.stopPropagation();
    api("/api/jobs/" + stop.dataset.stop + "/stop", {method: "POST"});
    return;
  }

  var task = e.target.closest(".task");
  if (task) showLog(task.dataset.job);
});

// -- changed files, and the diff for one of them ----------------------
var changes = [];
var picked = {};          // path -> selected

function diffLines(text) {
  // Git-style gutters: the shape is the information, so keep it.
  var pre = document.createElement("div");
  pre.className = "diff";
  text.split("\n").forEach(function (line) {
    var row = document.createElement("div");
    var c = line.charAt(0);
    row.className = line.indexOf("@@") === 0 ? "hunk"
      : (line.indexOf("+++") === 0 || line.indexOf("---") === 0) ? "meta"
      : c === "+" ? "add" : c === "-" ? "del" : "";
    row.appendChild(text_(line || " "));
    pre.appendChild(row);
  });
  return pre;
}

var STATE_NOTE = {
  committed: "against your last commit",
  staged: "against your last commit (staged)",
  untracked: "not committed yet - the whole file is new"
};

function diffHeader(wrap, path, against) {
  var bar = document.createElement("div");
  bar.className = "dbar";

  var label = document.createElement("span");
  label.className = "dim";
  label.appendChild(text_(against === "local"
    ? "Local changes" : "Compared with T4"));

  var note = document.createElement("span");
  note.className = "dim dnote";

  var swap = document.createElement("button");
  swap.className = "tog";
  swap.dataset.against = against === "local" ? "t4" : "local";
  swap.dataset.path = path;
  swap.appendChild(text_(against === "local"
    ? "compare with T4" : "show local changes"));

  bar.appendChild(label); bar.appendChild(note); bar.appendChild(swap);
  wrap.appendChild(bar);
  return note;
}

function historyList(entries, path, current) {
  var box = document.createElement("div");
  box.className = "hist";

  var head = document.createElement("div");
  head.className = "dim histhead";
  head.appendChild(text_(entries.length
    ? "History - " + entries.length + " commit(s) touched this file"
    : "No commits have touched this file yet."));
  box.appendChild(head);

  entries.forEach(function (c) {
    var row = document.createElement("button");
    row.className = "histrow" + (c.sha === current ? " on" : "");
    row.dataset.sha = c.sha;
    row.dataset.path = path;

    var when = document.createElement("span");
    when.className = "dim when";
    when.appendChild(text_((c.date || "").slice(0, 10)));

    var subject = document.createElement("span");
    subject.className = "subj";
    subject.appendChild(text_(c.subject || "(no message)"));

    var who = document.createElement("span");
    who.className = "dim who";
    who.appendChild(text_(c.short + "  " + (c.author || "")));

    row.appendChild(when); row.appendChild(subject); row.appendChild(who);
    box.appendChild(row);
  });
  return box;
}

function fillDiff(wrap, path, against, sha) {
  // Local by default: "what have I changed" is asked far more often, git
  // already holds the answer, and it costs no request. Comparing with T4 is
  // the deployment question, costs one, and is a click away.
  against = against || "local";
  wrap.dataset.against = against;
  wrap.innerHTML = "";
  var note = diffHeader(wrap, path, against);

  var waiting = document.createElement("p");
  waiting.className = "dim"; waiting.style.margin = ".2rem 0";
  waiting.appendChild(text_(against === "local"
    ? "Reading the repository..." : "Asking T4 for its copy..."));
  wrap.appendChild(waiting);

  if (sha) note.textContent = "one commit";

  api("/api/diff?env=" + encodeURIComponent(selected())
      + "&against=" + against + (sha ? "&sha=" + encodeURIComponent(sha) : "")
      + "&path=" + encodeURIComponent(path))
    .then(function (d) {
      waiting.remove();
      if (d.state && STATE_NOTE[d.state]) note.textContent = STATE_NOTE[d.state];
      if (d.error) {
        var bad = document.createElement("p");
        bad.className = "warn"; bad.style.margin = ".2rem 0";
        bad.appendChild(text_(d.error));
        wrap.appendChild(bad);
        return;
      }
      if (d.diff && d.diff.trim()) {
        wrap.appendChild(diffLines(d.diff));
      } else if (against !== "local") {
        var same = document.createElement("p");
        same.className = "dim"; same.style.margin = ".2rem 0";
        same.appendChild(text_("Identical to what T4 holds - nothing to deploy."));
        wrap.appendChild(same);
      }
      if (d.history) wrap.appendChild(historyList(d.history, path, d.sha));
    })
    .catch(function (e) {
      waiting.remove();
      var bad = document.createElement("p");
      bad.className = "warn"; bad.appendChild(text_(e.message));
      wrap.appendChild(bad);
    });
}

function toggleDiff(row) {
  var wrap = row.nextElementSibling;
  if (!wrap || !wrap.classList.contains("dwrap")) return;
  var opening = wrap.hidden;
  wrap.hidden = !opening;
  row.classList.toggle("open", opening);
  // Fetched once, then kept: re-opening a diff should not cost a request.
  if (opening && !wrap.dataset.loaded) {
    wrap.dataset.loaded = "1";
    fillDiff(wrap, row.dataset.path, "local");
  }
}

function renderChanges() {
  var box = el("changes");
  box.innerHTML = "";
  el("changecount").textContent = changes.length
    ? changes.length + " changed" : "none";
  if (!changes.length) {
    var p = document.createElement("p");
    p.className = "dim"; p.style.margin = ".2rem 0";
    p.appendChild(text_("Nothing edited since the last pull."));
    box.appendChild(p);
    syncButtons();
    return;
  }
  changes.forEach(function (c) {
    var row = document.createElement("div");
    row.className = "chg" + (picked[c.path] ? " on" : "");
    row.dataset.path = c.path;

    var tick = document.createElement("input");
    tick.type = "checkbox";
    tick.checked = !!picked[c.path];
    tick.dataset.pick = c.path;

    var path = document.createElement("span");
    path.className = "p"; path.appendChild(text_(c.path));

    var rec = document.createElement("span");
    rec.className = "rec";
    rec.appendChild(text_((c.endpoint || "?") + " #" + (c.id || "?")
      + (c.committed ? "" : "  uncommitted")));

    var tog = document.createElement("button");
    tog.className = "tog"; tog.dataset.diff = c.path;
    var caret = document.createElement("span");
    caret.className = "caret"; caret.appendChild(text_("\u203a"));
    tog.appendChild(caret);
    tog.appendChild(text_(" diff"));

    // One file on its own, for when the rest are not ready to go.
    var one = document.createElement("button");
    one.className = "tog one";
    one.dataset.pushone = c.path;
    one.appendChild(text_("push"));

    row.appendChild(tick); row.appendChild(path);
    row.appendChild(rec); row.appendChild(tog); row.appendChild(one);
    box.appendChild(row);

    var wrap = document.createElement("div");
    wrap.className = "dwrap";
    wrap.hidden = true;
    box.appendChild(wrap);
  });
  syncButtons();
}

function loadChanges() {
  return api("/api/changes?env=" + encodeURIComponent(selected()))
    .then(function (d) {
      changes = d.changed || [];
      var keep = {};
      changes.forEach(function (c) {
        keep[c.path] = picked[c.path] === undefined ? true : picked[c.path];
      });
      picked = keep;
      renderChanges();
    })
    .catch(function (e) { el("changes").textContent = e.message; });
}

document.addEventListener("click", function (e) {
  var pick = e.target.closest("input[data-pick]");
  if (pick) {
    picked[pick.dataset.pick] = pick.checked;
    pick.closest(".chg").classList.toggle("on", pick.checked);
    syncButtons();
    return;                      // ticking a box is not asking for the diff
  }
  var commit = e.target.closest(".histrow");
  if (commit) {
    e.stopPropagation();
    fillDiff(commit.closest(".dwrap"), commit.dataset.path, "local",
             commit.dataset.sha);
    return;
  }

  var swap = e.target.closest("button[data-against]");
  if (swap) {
    e.stopPropagation();
    fillDiff(swap.closest(".dwrap"), swap.dataset.path, swap.dataset.against);
    return;
  }

  var one = e.target.closest("button[data-pushone]");
  if (one) {
    e.stopPropagation();          // not a request to open the diff
    pushPaths([one.dataset.pushone]);
    return;
  }

  var row = e.target.closest(".chg");
  if (row) toggleDiff(row);
});

// -- tag builder -------------------------------------------------------
var tagData = null;

function fillSelect(id, values, label) {
  var sel = el(id);
  if (!sel) return;
  var keep = sel.value;
  sel.innerHTML = "";
  values.forEach(function (v) {
    var o = document.createElement("option");
    o.value = v.value; o.textContent = v.text || v.value;
    if (v.count) o.textContent += "  (" + v.count + ")";
    sel.appendChild(o);
  });
  if (keep && values.some(function (v) { return v.value === keep; })) {
    sel.value = keep;
  }
  if (label) sel.title = label;
}

function tagSpec() {
  var kind = el("tg-type").value;
  var spec = {type: kind};
  if (kind === "content" || kind === "media") {
    spec.name = el("tg-el").value;
    spec.output = el("tg-out").value;
    spec.modifiers = [].slice
      .call(document.querySelectorAll("#tg-mods input:checked"))
      .map(function (b) { return b.value; });
    if (el("tg-date").value) spec.date_format = el("tg-date").value;
  } else if (kind === "meta") {
    spec.meta = el("tg-meta").value;
  } else {
    spec.id = el("tg-obj").value;
  }
  return spec;
}

function showForKind() {
  var kind = el("tg-type").value;
  var element = kind === "content" || kind === "media";
  el("tg-ctwrap").hidden = !element;
  el("tg-elwrap").hidden = !element;
  el("tg-outwrap").hidden = !element;
  el("tg-mods").hidden = !element;
  el("tg-metawrap").hidden = kind !== "meta";
  el("tg-objwrap").hidden = element || kind === "meta";
  if (!el("tg-objwrap").hidden) {
    var source = kind === "navigation" ? tagData.navigation : tagData.lists;
    fillSelect("tg-obj", (source || []).map(function (o) {
      return {value: String(o.id), text: o.name + "  #" + o.id};
    }));
  }
}

function fillElements() {
  var wanted = el("tg-ct").value;
  var type = (tagData.types || []).filter(function (c) {
    return String(c.id) === wanted;
  })[0];
  fillSelect("tg-el", ((type || {}).elements || []).map(function (e) {
    return {value: e.name, text: e.name + "  \u00b7 " + e.type};
  }));
}

// Tokenised into DOM nodes rather than assembled as an HTML string: the
// element names come from the CMS, so a content type named after a script
// tag would be a problem rather than a curiosity if this used innerHTML.
var TOKENS = [
  ["hbs", /\{\{[^}]*\}\}/y],
  ["comment", /<!--[\s\S]*?-->/y],
  ["tag", /<\/?[\w:-]+|\/?>/y],
  ["string", /"[^"]*"|'[^']*'/y],
  ["attr", /[\w:-]+(?==)/y]
];

function paint(target, code) {
  // Try every token at every position and accumulate the rest as plain
  // text. Scanning ahead for "the next interesting character" was subtly
  // wrong -- it landed on the "=" rather than the start of the attribute
  // name before it, so attributes never matched.
  target.textContent = "";
  var at = 0, plain = "";

  function flush() {
    if (plain) { target.appendChild(text_(plain)); plain = ""; }
  }

  while (at < code.length) {
    var hit = null;
    for (var i = 0; i < TOKENS.length && !hit; i++) {
      TOKENS[i][1].lastIndex = at;
      var m = TOKENS[i][1].exec(code);
      if (m && m[0]) hit = [TOKENS[i][0], m[0]];
    }
    if (hit) {
      flush();
      var span = document.createElement("span");
      span.className = "t-" + hit[0];
      span.appendChild(text_(hit[1]));
      target.appendChild(span);
      at += hit[1].length;
    } else {
      plain += code.charAt(at);
      at += 1;
    }
  }
  flush();
}

function renderTag() {
  if (!tagData) return;
  api("/api/tag", {method: "POST", body: JSON.stringify(tagSpec())})
    .then(function (d) {
      paint(el("tg-tag"), d.tag || "");
      if (d.handlebars) {
        paint(el("tg-hbs"), d.handlebars);
      } else {
        el("tg-hbs").textContent = "(no Handlebars equivalent)";
      }
      el("tg-note").textContent = [d.note, tagData.mixing_warning]
        .filter(Boolean).join("  ");
    })
    .catch(function (e) { el("tg-note").textContent = e.message; });
}

function loadTagData() {
  return api("/api/tags?env=" + encodeURIComponent(selected()))
    .then(function (d) {
      tagData = d;
      var v = d.vocabulary || {};
      el("tagcount").textContent = (d.types || []).length + " content types, "
        + (v.tags_seen || 0) + " tags read";

      fillSelect("tg-type", (v.type || []).map(function (o) {
        return {value: o.value, count: o.count};
      }));
      fillSelect("tg-ct", (d.types || []).map(function (c) {
        return {value: String(c.id), text: c.name};
      }));
      fillSelect("tg-out", (v.output || []).map(function (o) {
        return {value: o.value, count: o.count};
      }));
      fillSelect("tg-meta", (v.meta || []).map(function (o) {
        return {value: o.value, count: o.count};
      }));

      var mods = el("tg-mods");
      mods.innerHTML = "";
      (v.modifiers || []).forEach(function (o) {
        var label = document.createElement("label");
        var box = document.createElement("input");
        box.type = "checkbox"; box.value = o.value;
        label.appendChild(box);
        label.appendChild(text_(o.value + (o.count ? "  (" + o.count + ")" : "")));
        mods.appendChild(label);
      });

      fillElements();
      showForKind();
      renderTag();
    })
    .catch(function (e) { el("tagcount").textContent = e.message; });
}

document.addEventListener("change", function (e) {
  if (!e.target.closest(".build, .mods")) return;
  if (e.target.id === "tg-ct") fillElements();
  if (e.target.id === "tg-type") showForKind();
  renderTag();
});
document.addEventListener("input", function (e) {
  if (e.target.id === "tg-date") renderTag();
});
document.addEventListener("click", function (e) {
  var copy = e.target.closest("button[data-copytag]");
  if (!copy) return;
  var node = el(copy.dataset.copytag === "tag" ? "tg-tag" : "tg-hbs");
  var done = function () {
    var old = copy.textContent;
    copy.textContent = "copied";
    setTimeout(function () { copy.textContent = old; }, 1200);
  };
  if (navigator.clipboard) {
    navigator.clipboard.writeText(node.textContent).then(done, done);
  } else { done(); }
});

el("rescan").addEventListener("click", loadChanges);

function pushPaths(paths) {
  var env = envByName(selected()) || {};
  if (!env.push_allowed) {
    el("jobstate").textContent = "Pushing is disabled for " + env.name
      + " in .t4/config.json.";
    el("jobstate").className = "pill warn";
    return;
  }
  var what = paths.length === 1 ? paths[0]
    : paths.length + " selected file(s)";
  if (!confirm("Push " + what + " to " + (env.label || env.name) + "?\n\n"
             + "It is sent to T4 with the current record backed up first."
             + (env.requires_commit
                 ? "\nThis environment requires the file to be committed."
                 : ""))) return;
  run(["push"], {extra: ["--yes"].concat(paths)});
}

el("push").addEventListener("click", function () {
  var env = envByName(selected()) || {};
  var chosen = Object.keys(picked).filter(function (p) { return picked[p]; });
  if (changes.length && !chosen.length) {
    el("jobstate").textContent = "Nothing selected to push.";
    el("jobstate").className = "pill warn";
    return;
  }
  pushPaths(chosen);
});

el("stop").addEventListener("click", function () {
  if (shown) api("/api/jobs/" + shown + "/stop", {method: "POST"});
});

el("openserve").addEventListener("click", function () {
  var url = el("openserve").dataset.url;
  if (url) window.open(url, "_blank");
});

el("refresh").addEventListener("click", load);
el("env").addEventListener("change", function () { render(); syncButtons(); });

load();
</script>
"""
