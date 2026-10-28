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
import secrets
import subprocess
import sys
import threading
import webbrowser
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from .compare import load_manifest
from .vcs import open_repo

# Deliberately not serve's 8321: running the panel and the site at once is the
# normal case, not a clash to resolve.
DEFAULT_PORT = 8320


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
            for watcher in self._watchers:
                watcher.put(line)

    def watch(self):
        """A queue fed the backlog, then anything further."""
        channel = queue.Queue()
        with self._lock:
            for line in self.lines:
                channel.put(line)
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
                "returncode": self.returncode, "lines": len(self.lines)}


class Jobs:
    def __init__(self, cwd):
        self.cwd = cwd
        self.items = {}
        self._next = 0
        self._lock = threading.Lock()

    def start(self, argv):
        with self._lock:
            self._next += 1
            job = Job(str(self._next), argv, self.cwd)
            self.items[job.id] = job
        return job.start()

    def get(self, job_id):
        return self.items.get(job_id)

    def running(self):
        return [j for j in self.items.values() if not j.done]


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
            "has_token": bool(project.read_token(name)),
            "tracked": tracked,
            "source": str(source),
        })

    repo = open_repo(project)
    default = project.default_env() if project.env_names() else None
    return {
        "root": str(project.root),
        "default_env": default,
        "environments": envs,
        "mirror": mirror_stats(project),
        "repo": {"available": repo.available,
                 "summary": repo.describe(
                     [project.env_dir(default)] if default else None)},
        "jobs": [j.summary() for j in jobs.items.values()],
    }


class AppHandler(BaseHTTPRequestHandler):
    project = None
    jobs = None
    token = None

    server_version = "t4-sync-app"

    # -- plumbing ----------------------------------------------------------

    def _authorised(self):
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
            if not self._authorised():
                return self._send(403, FORBIDDEN, "text/html; charset=utf-8")
            return self._send(200, PAGE % {"token": html.escape(self.token)},
                              "text/html; charset=utf-8")
        if not self._authorised():
            return self._json({"error": "bad or missing token"}, 403)
        if path == "/api/state":
            return self._json(state(self.project, self.jobs))
        if path.startswith("/api/jobs/") and path.endswith("/stream"):
            return self._stream(path.split("/")[3])
        return self._json({"error": "not found"}, 404)

    def do_POST(self):
        if not self._authorised():
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
            return self._json(self.jobs.start(argv).summary())
        if path.startswith("/api/jobs/") and path.endswith("/stop"):
            job = self.jobs.get(path.split("/")[3])
            if not job:
                return self._json({"error": "no such job"}, 404)
            job.stop()
            return self._json(job.summary())
        return self._json({"error": "not found"}, 404)

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
        print("\nThe token in that URL is what authorises the page; it is new")
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
<h1>Not authorised</h1>
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
 --line:#e2e5ec;--card:#f7f8fa;--accent:#2b4bbf;--warn:#9a3412;--ok:#17603a}
@media (prefers-color-scheme:dark){:root{--bg:#12141a;--fg:#e8eaf0;
 --dim:#8b93a7;--line:#262b36;--card:#191c24;--accent:#9fb4ff;--warn:#fca5a5;
 --ok:#8ff0a6}}
*{box-sizing:border-box}
body{margin:0;background:var(--bg);color:var(--fg);
 font:14px/1.55 ui-sans-serif,system-ui,-apple-system,"Segoe UI",sans-serif}
header{display:flex;align-items:center;gap:1rem;flex-wrap:wrap;
 padding:.75rem 1rem;border-bottom:1px solid var(--line)}
h1{font-size:15px;margin:0;letter-spacing:.02em}
h2{font-size:13px;margin:0 0 .5rem;color:var(--dim);font-weight:600;
 text-transform:uppercase;letter-spacing:.06em}
main{display:grid;grid-template-columns:minmax(0,1fr) minmax(0,1fr);
 gap:1rem;padding:1rem;align-items:start}
@media (max-width:820px){main{grid-template-columns:minmax(0,1fr)}}
.card{background:var(--card);border:1px solid var(--line);border-radius:8px;
 padding:.75rem .9rem}
.row{display:flex;gap:.6rem;align-items:baseline;padding:.25rem 0;
 border-top:1px solid var(--line)}
.row:first-of-type{border-top:0}
.key{flex:0 0 7.5rem;color:var(--dim)}
.val{flex:1 1 auto;min-width:0;word-break:break-word}
button,select{font:inherit;color:inherit;background:var(--bg);
 border:1px solid var(--line);border-radius:6px;padding:.3rem .7rem;
 cursor:pointer}
button:hover:not(:disabled){border-color:var(--accent);color:var(--accent)}
button:disabled{opacity:.45;cursor:default}
button.primary{background:var(--accent);color:#fff;border-color:var(--accent)}
button.primary:hover:not(:disabled){filter:brightness(1.1);color:#fff}
button.danger{border-color:var(--warn);color:var(--warn)}
.actions{display:flex;flex-wrap:wrap;gap:.4rem;margin-top:.6rem}
code,pre{font-family:ui-monospace,SFMono-Regular,Menlo,monospace}
pre{margin:0;padding:.6rem .7rem;background:#0b0d12;color:#dbe1ee;
 border-radius:6px;max-height:26rem;overflow:auto;font-size:12px;
 white-space:pre-wrap;word-break:break-word}
.dim{color:var(--dim)}
.ok{color:var(--ok)}
.warn{color:var(--warn)}
.pill{display:inline-block;padding:.05rem .45rem;border-radius:999px;
 border:1px solid var(--line);font-size:11px;color:var(--dim)}
footer{padding:.5rem 1rem 2rem;color:var(--dim);font-size:12px}
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
  <section class="card">
    <h2>Project</h2>
    <div id="summary"></div>

    <h2 style="margin-top:1rem">Get</h2>
    <div class="actions">
      <button data-run='["pull"]'>Pull source</button>
      <button data-run='["mirror"]'>Mirror site</button>
      <button data-run='["sections"]'>Map sections</button>
      <button data-run='["sync"]'>Sync</button>
    </div>

    <h2 style="margin-top:1rem">Look</h2>
    <div class="actions">
      <button data-run='["serve"]' data-keep>Start local site</button>
      <button id="openserve" disabled>Open it</button>
      <button data-run='["status"]'>Status</button>
      <button data-run='["diff"]'>What differs</button>
    </div>

    <h2 style="margin-top:1rem">Deploy</h2>
    <div class="actions">
      <button data-run='["save"]'>Commit to git</button>
      <button id="push" class="primary">Push to T4</button>
    </div>
    <p class="dim" style="margin:.5rem 0 0;font-size:12px">
      Push runs the same command a terminal would, so it still refuses an
      environment that is not writable and source that is not committed.
    </p>
  </section>

  <section class="card">
    <h2>Output <span id="jobstate" class="pill"></span>
      <button id="stop" class="danger" style="float:right" disabled>Stop</button>
    </h2>
    <pre id="log">Pick an action to begin.</pre>
  </section>
</main>

<footer id="foot"></footer>

<script>
var TOKEN = "%(token)s";
var state = null;
var current = null;
var serveJob = null;

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

  el("push").disabled = !env.push_allowed;
  el("push").title = env.push_allowed ? ""
    : "Pushing is disabled for this environment in .t4/config.json";
  el("foot").textContent = "Every button runs the matching t4 command as a "
    + "subprocess in " + state.root + ".";
}

function load() {
  return api("/api/state").then(function (s) { state = s; render(); });
}

function setRunning(job) {
  current = job;
  var running = job && !job.done;
  document.querySelectorAll("button[data-run]").forEach(function (b) {
    b.disabled = !!running;
  });
  el("push").disabled = !!running || !(envByName(selected()) || {}).push_allowed;
  el("stop").disabled = !running;
  el("jobstate").textContent = job
    ? (running ? "running: t4 " + job.argv.join(" ")
               : "t4 " + job.argv.join(" ") + " - exit " + job.returncode)
    : "";
  el("jobstate").className = "pill" + (job && job.done
    ? (job.returncode === 0 ? " ok" : " warn") : "");
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

  el("log").textContent = "";
  return api("/api/run", {method: "POST", body: JSON.stringify({argv: argv})})
    .then(function (job) {
      setRunning(job);
      if (argv[0] === "serve") { serveJob = job; el("openserve").disabled = false; }
      stream(job);
      return job;
    })
    .catch(function (e) { el("log").textContent = "Error: " + e.message; });
}

function stream(job) {
  var log = el("log");
  var source = new EventSource("/api/jobs/" + job.id + "/stream?t=" + TOKEN);
  source.onmessage = function (e) {
    log.appendChild(text(JSON.parse(e.data) + "\n"));
    log.scrollTop = log.scrollHeight;
  };
  source.addEventListener("end", function (e) {
    source.close();
    var done = JSON.parse(e.data);
    setRunning(done);
    if (serveJob && done.id === serveJob.id) {
      serveJob = null; el("openserve").disabled = true;
    }
    load();
  });
  source.onerror = function () { source.close(); };
}

document.addEventListener("click", function (e) {
  var b = e.target.closest("button[data-run]");
  if (!b) return;
  run(JSON.parse(b.getAttribute("data-run")));
});

el("push").addEventListener("click", function () {
  var env = envByName(selected()) || {};
  if (!confirm("Push local changes to " + (env.label || env.name) + "?\n\n"
             + "Each differing file is sent to T4. Files that are not "
             + "committed will be refused.")) return;
  run(["push"], {extra: ["--yes"]});
});

el("stop").addEventListener("click", function () {
  if (!current) return;
  api("/api/jobs/" + current.id + "/stop", {method: "POST"});
});

el("openserve").addEventListener("click", function () {
  window.open("http://127.0.0.1:8321/", "_blank");
});

el("refresh").addEventListener("click", load);
el("env").addEventListener("change", render);

load();
</script>
"""
