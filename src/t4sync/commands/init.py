"""t4 init -- set up a project directory."""
import pathlib

from ..config import CONFIG_DIR, CONFIG_FILE, DEFAULT_CONFIG, Project
import json

GITIGNORE = """\
# t4-sync
.t4/tokens/
{source}/*/_raw/
{source}/*/_backup/
{mirror}/
"""


def run(args, project=None):
    root = pathlib.Path(args.directory or ".").resolve()
    config_path = root / CONFIG_DIR / CONFIG_FILE

    if config_path.is_file() and not args.force:
        print("Already initialised: %s" % config_path)
        print("Use --force to overwrite.")
        return 0

    config = dict(DEFAULT_CONFIG)
    config["mirror_dir"] = "site-mirror"
    config["overrides_dir"] = "overrides"
    config_path.parent.mkdir(parents=True, exist_ok=True)
    config_path.write_text(json.dumps(config, indent=2, sort_keys=True) + "\n",
                           encoding="utf-8")

    # Append our ignores rather than clobbering an existing .gitignore.
    block = GITIGNORE.format(source=config["source_dir"], mirror=config["mirror_dir"])
    gitignore = root / ".gitignore"
    existing = gitignore.read_text(encoding="utf-8") if gitignore.is_file() else ""
    if ".t4/tokens/" not in existing:
        with open(gitignore, "a", encoding="utf-8") as fh:
            if existing and not existing.endswith("\n"):
                fh.write("\n")
            fh.write("\n" + block)
        print("Updated .gitignore")

    print("Initialised %s" % config_path)
    print("\nNext:")
    print("  t4 env add test https://myweb-test.example.edu")
    print("  t4 token set test")
    print("  t4 pull")
    return 0
