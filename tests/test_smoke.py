"""Smoke tests that need no network and no T4 instance."""
import json
import pathlib
import subprocess
import sys
import tempfile
import unittest

ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from t4sync import extract            # noqa: E402
from t4sync.cli import build_parser    # noqa: E402
from t4sync.config import Project      # noqa: E402
from t4sync.serve import RELOAD_PATH, Watcher, load_rules, transform_html  # noqa: E402


class TestExtract(unittest.TestCase):
    def test_crlf_survives_a_round_trip(self):
        """T4 stores CRLF; text-mode IO would silently rewrite it."""
        with tempfile.TemporaryDirectory() as tmp:
            path = pathlib.Path(tmp) / "x.html"
            original = "<p>one</p>\r\n<p>two</p>\r\n"
            extract.write_text(path, original)
            self.assertEqual(path.read_bytes().count(b"\r\n"), 2)
            self.assertEqual(extract.read_text(path), original)

    def test_extension_detection(self):
        self.assertEqual(extract.guess_extension("{{#if x}}{{/if}}"), "hbs")
        self.assertEqual(extract.guess_extension('<t4 type="content" />'), "html")
        self.assertEqual(extract.guess_extension("<!DOCTYPE html>"), "html")
        self.assertEqual(extract.guess_extension("document.write('x')"), "js")

    def test_duplicate_names_do_not_clobber(self):
        with tempfile.TemporaryDirectory() as tmp:
            manifest = {}
            items = [{"id": 1, "name": "Same", "headerCode": "<p>a</p>"},
                     {"id": 2, "name": "Same", "headerCode": "<p>b</p>"}]
            extract.extract(items, tmp, "pageLayout", manifest)
            self.assertEqual(len(manifest), 2)
            self.assertEqual(len(list(pathlib.Path(tmp).glob("*.html"))), 2)

    def test_local_edits_are_protected(self):
        with tempfile.TemporaryDirectory() as tmp:
            manifest = {}
            items = [{"id": 1, "name": "L", "headerCode": "<p>remote v1</p>"}]
            extract.extract(items, tmp, "pageLayout", manifest)
            rel = "pageLayout/l.header.html"
            target = pathlib.Path(tmp) / "l.header.html"
            self.assertIn(rel, manifest)

            extract.write_text(target, "<p>my local edit</p>")
            fresh = {}
            _, _, _, kept = extract.extract(
                [{"id": 1, "name": "L", "headerCode": "<p>remote v2</p>"}],
                tmp, "pageLayout", fresh, previous=manifest, protect=True)

            self.assertEqual(kept, [rel])
            self.assertEqual(extract.read_text(target), "<p>my local edit</p>")

    def test_force_overwrites(self):
        with tempfile.TemporaryDirectory() as tmp:
            manifest = {}
            extract.extract([{"id": 1, "name": "L", "headerCode": "<p>v1</p>"}],
                            tmp, "pageLayout", manifest)
            target = pathlib.Path(tmp) / "l.header.html"
            extract.write_text(target, "<p>local</p>")
            extract.extract([{"id": 1, "name": "L", "headerCode": "<p>v2</p>"}],
                            tmp, "pageLayout", {}, previous=manifest, protect=False)
            self.assertEqual(extract.read_text(target), "<p>v2</p>")


class TestRules(unittest.TestCase):
    def test_tab_separated_rules(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = pathlib.Path(tmp) / "rewrites.conf"
            path.write_text("# comment\nhttps://www\\.x\\.com/\t/\n\nbad-line-no-tab\n")
            rules = load_rules(str(path))
            self.assertEqual(len(rules), 1)
            pattern, replacement = rules[0]
            self.assertEqual(pattern.sub(replacement, 'href="https://www.x.com/a"'),
                             'href="/a"')


class TestServeTransforms(unittest.TestCase):
    def test_reload_script_goes_before_body_close(self):
        out = transform_html("<html><body><p>hi</p></body></html>",
                             inject_reload=True)
        self.assertIn(RELOAD_PATH, out)
        self.assertLess(out.index(RELOAD_PATH), out.index("</body>"))

    def test_reload_appended_when_no_body_tag(self):
        out = transform_html("<p>fragment</p>", inject_reload=True)
        self.assertIn(RELOAD_PATH, out)

    def test_no_injection_when_disabled(self):
        out = transform_html("<html><body></body></html>", inject_reload=False)
        self.assertNotIn(RELOAD_PATH, out)

    def test_rules_and_injection_combine(self):
        import re
        rules = [(re.compile(r"https://www\.x\.com/"), "/")]
        out = transform_html('<body><a href="https://www.x.com/a"></a></body>',
                             rules, inject_reload=True)
        self.assertIn('href="/a"', out)
        self.assertIn(RELOAD_PATH, out)


class TestServeGating(unittest.TestCase):
    """HTML must be transformed when EITHER rules or the watcher are active.

    Gating on rules alone meant live reload silently did nothing on a project
    with an empty rewrites.conf, which is the default.
    """

    def test_reload_alone_still_transforms(self):
        out = transform_html("<body></body>", rules=(), inject_reload=True)
        self.assertIn(RELOAD_PATH, out)

    def test_rules_alone_still_transforms(self):
        import re
        rules = [(re.compile("a"), "b")]
        out = transform_html("<body>a</body>", rules, inject_reload=False)
        self.assertIn("b", out)
        self.assertNotIn(RELOAD_PATH, out)


class TestWatcher(unittest.TestCase):
    def test_detects_a_changed_file(self):
        with tempfile.TemporaryDirectory() as tmp:
            target = pathlib.Path(tmp) / "a.css"
            target.write_text("body{}")
            watcher = Watcher([tmp], interval=0.05)
            before = watcher._fingerprint()
            target.write_text("body{color:red}")
            self.assertNotEqual(before, watcher._fingerprint())

    def test_ignores_dotfiles(self):
        with tempfile.TemporaryDirectory() as tmp:
            watcher = Watcher([tmp], interval=0.05)
            before = watcher._fingerprint()
            (pathlib.Path(tmp) / ".DS_Store").write_text("junk")
            self.assertEqual(before, watcher._fingerprint())


class TestProject(unittest.TestCase):
    def test_init_then_add_environment(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = pathlib.Path(tmp)
            (root / ".t4").mkdir()
            (root / ".t4" / "config.json").write_text(json.dumps(
                {"source_dir": "t4-source", "environments": {}}))
            project = Project(root)
            project.config["environments"]["test"] = {
                "base": "https://example.test/terminalfour/rs"}
            project.save()

            again = Project(root)
            self.assertEqual(again.env_names(), ["test"])
            self.assertFalse(again.env("test")["push_allowed"])  # safe default

    def test_token_round_trip(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = pathlib.Path(tmp)
            (root / ".t4").mkdir()
            (root / ".t4" / "config.json").write_text(json.dumps({"environments": {}}))
            project = Project(root)
            project.write_token("test", "a.b.c")
            self.assertEqual(project.read_token("test"), "a.b.c")


class TestDoctor(unittest.TestCase):
    def test_version_regex(self):
        from t4sync.commands.doctor import VERSION_RE
        page = "<title>TERMINALFOUR</title><p>Version 8.4.5</p>"
        self.assertEqual(VERSION_RE.search(page).group(1), "8.4.5")
        self.assertIsNone(VERSION_RE.search("<p>no version here</p>"))


class TestCli(unittest.TestCase):
    def test_every_command_parses(self):
        parser = build_parser()
        for argv in (["init"], ["env"], ["env", "add", "t", "https://x"],
                     ["token", "set", "t"], ["pull", "--all"], ["sync"],
                     ["diff", "--show"], ["push", "--dry-run"],
                     ["mirror", "https://x"], ["serve", "--no-proxy"],
                     ["doctor"], ["doctor", "--env", "prod"],
                     ["status"], ["status", "--offline"],
                     ["edit", "a/b.css"], ["edit", "--list"],
                     ["serve", "--no-reload"]):
            with self.subTest(argv=argv):
                parser.parse_args(argv)

    def test_runs_as_a_module(self):
        out = subprocess.run([sys.executable, "-m", "t4sync", "--version"],
                             capture_output=True, text=True,
                             cwd=str(ROOT / "src"))
        self.assertIn("t4-sync", out.stdout)


if __name__ == "__main__":
    unittest.main()
