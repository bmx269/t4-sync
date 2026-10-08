"""Project configuration and token storage.

A project is any directory containing `.t4/config.json`, found by walking up
from the working directory -- the same way git finds `.git` and ddev finds
`.ddev`. The tool is generic; the configuration and the pulled source belong to
the project, not to the tool's own checkout.
"""
import json
import os
import pathlib
import stat

from .errors import T4Error

CONFIG_DIR = ".t4"
CONFIG_FILE = "config.json"
TOKEN_DIR = "tokens"

DEFAULT_CONFIG = {
    "source_dir": "t4-source",
    "environments": {},
}


def find_project(start=None):
    """Walk up from `start` looking for a .t4 directory."""
    here = pathlib.Path(start or os.getcwd()).resolve()
    for candidate in [here, *here.parents]:
        if (candidate / CONFIG_DIR / CONFIG_FILE).is_file():
            return candidate
    raise T4Error(
        "No .t4/config.json found in %s or any parent.\n"
        "Run `t4 init` in your project directory to create one." % here
    )


class Project:
    def __init__(self, root):
        self.root = pathlib.Path(root)
        self.config_path = self.root / CONFIG_DIR / CONFIG_FILE
        self.config = json.loads(self.config_path.read_text(encoding="utf-8"))

    @classmethod
    def find(cls, start=None):
        return cls(find_project(start))

    @property
    def source_root(self):
        return self.root / self.config.get("source_dir", "t4-source")

    def env_names(self):
        return sorted(self.config.get("environments", {}))

    def env(self, name):
        envs = self.config.get("environments", {})
        if name not in envs:
            raise T4Error("Unknown environment %r. Configured: %s"
                          % (name, ", ".join(sorted(envs)) or "none"))
        env = dict(envs[name])
        env["name"] = name
        env.setdefault("label", name)
        env.setdefault("push_allowed", False)
        return env

    def default_env(self):
        names = self.env_names()
        if not names:
            raise T4Error("No environments configured. Run `t4 env add <name> <url>`.")
        return self.config.get("default_environment", names[0])

    def env_dir(self, name):
        return self.source_root / name

    # -- tokens ------------------------------------------------------------
    # Kept beside the config rather than in the user's home directory, so a
    # machine can hold several projects without them colliding. Never committed.

    def token_path(self, name):
        return self.root / CONFIG_DIR / TOKEN_DIR / ("%s.token" % name)

    def read_token(self, name):
        path = self.token_path(name)
        try:
            token = path.read_text(encoding="utf-8").strip()
        except OSError:
            return None
        return token or None

    def require_token(self, name):
        token = os.environ.get("T4_TOKEN") or self.read_token(name)
        if not token:
            raise T4Error(
                "No token stored for %r.\n"
                "  t4 token set %s\n"
                "Generate one in T4: Administration > User Management."
                % (name, name))
        return token

    def write_token(self, name, token):
        path = self.token_path(name)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(token.strip() + "\n", encoding="utf-8")
        try:
            os.chmod(path, stat.S_IRUSR | stat.S_IWUSR)  # no-op on Windows
        except OSError:
            pass
        return path

    def save(self):
        self.config_path.parent.mkdir(parents=True, exist_ok=True)
        self.config_path.write_text(
            json.dumps(self.config, indent=2, sort_keys=True) + "\n", encoding="utf-8")
