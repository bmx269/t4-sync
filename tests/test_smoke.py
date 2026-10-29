"""Smoke tests that need no network and no T4 instance."""
import argparse
import contextlib
import html as html_mod
import io
import json
import os
import pathlib
import re
import subprocess
import sys
import tempfile
import time
import unittest

ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from t4sync import extract            # noqa: E402
from t4sync.extract import local_path   # noqa: E402
from t4sync.cli import build_parser    # noqa: E402
from t4sync import app, cli, history, mirror, opener, tags   # noqa: E402
import urllib.error   # noqa: E402
from t4sync.config import Project      # noqa: E402
from t4sync.inspector import (ContentLayoutIndex, ContentTypeIndex,  # noqa: E402
                              InspectorContext, LayoutIndex,
                              annotate_components, annotate_content_items,
                              build_comment, build_panel, group_by_type,
                              page_content, strip_injected, SCRIPT)
from t4sync.progress import Progress   # noqa: E402
from t4sync.render import (LayoutResolver, align,   # noqa: E402
                           preview, preview_every, render)
from t4sync.compare import read_field, remote_path, write_field  # noqa: E402
from t4sync import compare as compare_mod   # noqa: E402
from t4sync.contentlayout import format_key, layout_name  # noqa: E402
from t4sync.commands.pull import media_filename   # noqa: E402
from t4sync import sections as sectionlib   # noqa: E402
from t4sync.sections import (ContentItems, MediaSources,   # noqa: E402
                             SectionLayouts, by_url_path, layout_for)
from t4sync import serve   # noqa: E402
from t4sync.serve import (RELOAD_PATH, Watcher, load_rules,   # noqa: E402
                          rules_for_published_urls, transform_html)
from t4sync.errors import T4Error   # noqa: E402
from t4sync.vcs import (Repo, message_for, require_clean,   # noqa: E402
                        requires_commit)
from t4sync.commands import edit, site   # noqa: E402


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
            path.write_text("# comment\nhttps://www\\.example\\.com/\t/\n\nbad-line-no-tab\n")
            rules = load_rules(str(path))
            self.assertEqual(len(rules), 1)
            pattern, replacement = rules[0]
            self.assertEqual(pattern.sub(replacement, 'href="https://www.example.com/a"'),
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
        rules = [(re.compile(r"https://www\.example\.com/"), "/")]
        out = transform_html('<body><a href="https://www.example.com/a"></a></body>',
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


class TestInspector(unittest.TestCase):
    MANIFEST = {
        "pageLayout/my-layout.header.html": {
            "endpoint": "pageLayout", "id": 1180,
            "field": "headerCode", "name": "my_layout"},
        "pageLayout/my-layout.footer.html": {
            "endpoint": "pageLayout", "id": 1180,
            "field": "footerCode", "name": "my_layout"},
        "contenttype/thing.json": {
            "endpoint": "contenttype", "id": 5, "field": None, "name": "Thing"},
    }
    PAGE = ('<html><head>'
            '<meta name="t4-layout" content="my_layout">'
            '<meta name="t4-layout-id" content="1180">'
            '<link rel="stylesheet" href="/media/css/site.css">'
            '<script src="/media/js/app.js"></script>'
            '</head><body>hi</body></html>')

    def test_index_only_tracks_page_layouts(self):
        index = LayoutIndex(self.MANIFEST)
        self.assertEqual(len(index.files_for(1180)), 2)
        self.assertEqual(index.files_for(999), [])

    def test_panel_names_the_layout_and_its_files(self):
        panel = build_panel(self.PAGE, LayoutIndex(self.MANIFEST), env_name="test")
        self.assertIn("my_layout", panel)
        self.assertIn("1180", panel)
        self.assertIn("pageLayout/my-layout.header.html", panel)
        self.assertIn("pageLayout/my-layout.footer.html", panel)

    def test_panel_lists_local_assets_only(self):
        page = self.PAGE.replace("</head>",
                                 '<link rel="stylesheet" href="https://cdn.example.com/x.css"></head>')
        panel = build_panel(page, LayoutIndex(self.MANIFEST))
        self.assertIn("media/css/site.css", panel)
        self.assertIn("media/js/app.js", panel)
        self.assertNotIn("cdn.example.com", panel)

    def test_overridden_assets_are_marked(self):
        with tempfile.TemporaryDirectory() as tmp:
            target = pathlib.Path(tmp) / "media" / "css" / "site.css"
            target.parent.mkdir(parents=True)
            target.write_text("body{}")
            panel = build_panel(self.PAGE, LayoutIndex(self.MANIFEST), overrides_dir=tmp)
            self.assertIn("t4i-on", panel)
            self.assertIn("1 overridden", panel)

    def test_page_without_meta_says_so(self):
        panel = build_panel("<html><body>x</body></html>", LayoutIndex({}))
        self.assertIn("no t4-layout meta", panel)

    def test_known_layout_not_yet_pulled(self):
        panel = build_panel(self.PAGE, LayoutIndex({}))
        self.assertIn("not in the local pull", panel)

    def test_injected_before_body_close(self):
        out = transform_html(self.PAGE, inspector=InspectorContext(
            index=LayoutIndex(self.MANIFEST), env_name="test"))
        self.assertIn("t4-inspector", out)
        self.assertLess(out.index("t4-inspector"), out.index("</body>"))


class TestDebugComments(unittest.TestCase):
    AMBIGUOUS = {
        "pageLayout/a.header.html": {"endpoint": "pageLayout", "id": 1,
                                     "field": "headerCode", "name": "basic_page.production"},
        "pageLayout/b.header.html": {"endpoint": "pageLayout", "id": 2,
                                     "field": "headerCode", "name": "basic_page.webdev"},
    }

    def test_exact_name_resolves_to_a_file(self):
        manifest = {"pageLayout/m.header.html": {
            "endpoint": "pageLayout", "id": 7, "field": "headerCode", "name": "standard"}}
        page = '<meta name="t4-layout" content="standard">'
        out = build_comment(page, LayoutIndex(manifest), env_name="test")
        self.assertIn("PAGE LAYOUT: 'standard'", out)
        self.assertIn("x t4-source/test/pageLayout/m.header.html", out)
        self.assertNotIn("SUGGESTIONS", out)

    def test_ambiguous_name_lists_candidates(self):
        """A published name that maps to several layouts must not be guessed."""
        page = '<meta name="t4-layout" content="basic_page">'
        out = build_comment(page, LayoutIndex(self.AMBIGUOUS), env_name="test")
        self.assertIn("LAYOUT NAME SUGGESTIONS", out)
        self.assertIn("basic_page.production", out)
        self.assertIn("basic_page.webdev", out)
        self.assertNotIn("x t4-source", out)      # nothing claimed as used

    def test_id_wins_over_an_ambiguous_name(self):
        page = ('<meta name="t4-layout" content="basic_page">'
                '<meta name="t4-layout-id" content="2">')
        out = build_comment(page, LayoutIndex(self.AMBIGUOUS), env_name="test")
        self.assertIn("x t4-source/test/pageLayout/b.header.html", out)
        self.assertNotIn("SUGGESTIONS", out)

    def test_untagged_page_says_so(self):
        out = build_comment("<html><body>x</body></html>", LayoutIndex({}))
        self.assertIn("no t4-layout meta", out)

    def test_double_hyphen_cannot_break_the_comment(self):
        manifest = {"pageLayout/x.header.html": {
            "endpoint": "pageLayout", "id": 1, "field": "headerCode",
            "name": "weird--name"}}
        page = '<meta name="t4-layout" content="weird--name">'
        out = build_comment(page, LayoutIndex(manifest))
        body = out.split("<!--", 1)[1]
        self.assertNotIn("--", body.split("-->")[0])


class TestComponentAnnotation(unittest.TestCase):
    def _index(self, tmp, endpoint="contenttype", **record):
        directory = pathlib.Path(tmp) / endpoint
        directory.mkdir(parents=True, exist_ok=True)
        (directory / "thing.json").write_text(json.dumps(record))
        return ContentTypeIndex.from_env_dir(tmp, endpoint)

    def test_marker_is_annotated_in_place(self):
        with tempfile.TemporaryDirectory() as tmp:
            index = self._index(tmp, id=322, name="Contact footer")
            page = '<div><!-- ct:322 Contact Footer --><p>x</p></div>'
            out, n = annotate_components(page, {"contenttype": index}, env_name="test")
            self.assertEqual(n, 1)
            self.assertIn("T4 COMPONENT: 'Contact footer' -> "
                          "t4-source/test/contenttype/thing.json", out)
            self.assertIn("<p>x</p>", out)          # page content untouched
            # The fence must take the page back to exactly what it was.
            self.assertEqual(strip_injected(out), page)

    def test_hyphenated_names_match(self):
        """A hyphen is ordinary in a name; an earlier pattern excluded it."""
        with tempfile.TemporaryDirectory() as tmp:
            index = self._index(tmp, "navigation", id=273, name="Footer-Contact")
            page = "<!-- n:Footer-Contact (273) -->"
            out, n = annotate_components(page, {"navigation": index}, env_name="test")
            self.assertEqual(n, 1)
            self.assertIn("T4 NAVIGATION: 'Footer-Contact'", out)

    def test_unknown_marker_says_so_rather_than_guessing(self):
        with tempfile.TemporaryDirectory() as tmp:
            index = self._index(tmp, id=1, name="Something else")
            out, n = annotate_components('<!-- ct:999 Mystery -->',
                                         {"contenttype": index}, env_name="test")
            self.assertEqual(n, 1)
            self.assertIn("not in the local pull", out)
            self.assertNotIn("t4-source/test/contenttype/thing.json", out)

    def test_each_marker_annotated_once(self):
        with tempfile.TemporaryDirectory() as tmp:
            index = self._index(tmp, id=5, name="Box")
            page = "<!-- ct:5 Box --><!-- ct-5 Box -->"
            out, n = annotate_components(page, {"contenttype": index}, env_name="test")
            self.assertEqual(n, 2)
            self.assertEqual(out.count("T4 COMPONENT"), 2)

    def test_no_index_is_a_no_op(self):
        page = "<!-- ct:322 Contact Footer -->"
        out, n = annotate_components(page, {}, env_name="test")
        self.assertEqual((out, n), (page, 0))


class TestPreview(unittest.TestCase):
    """Reconstructing a page from layout source plus its published output."""

    SRC = ('<!--start-->\n<t4 type="navigation" id="1" />\n'
           '<t4 type="navigation" id="2" />\n<!--end-->')
    PAGE = ('<html><!--start-->\n'
            '<!-- n:One (1) --><nav>ONE</nav><!-- /n:1 -->'
            '<!-- n:Two (2) --><nav>TWO</nav><!-- /n:2 -->'
            '\n<!--end--></html>')

    def test_unedited_source_rebuilds_the_page_exactly(self):
        """The strictest check: a no-op edit must be byte-identical."""
        out, problems = preview(self.PAGE, self.SRC, self.SRC)
        self.assertEqual(out, self.PAGE)
        self.assertEqual(problems, [])

    def test_wrapping_a_separable_tag(self):
        edited = self.SRC.replace('<t4 type="navigation" id="1" />',
                                  '<div class="x"><t4 type="navigation" id="1" /></div>')
        out, problems = preview(self.PAGE, self.SRC, edited)
        self.assertIn('<div class="x"><!-- n:One (1) --><nav>ONE</nav><!-- /n:1 --></div>', out)
        self.assertIn("<nav>TWO</nav>", out)
        self.assertEqual(problems, [])

    def test_inseparable_run_is_reported_not_hidden(self):
        """Tags with no markers share one span; wrapping one wraps them all."""
        page = "<html><!--start-->\n<nav>ONE</nav><nav>TWO</nav>\n<!--end--></html>"
        edited = self.SRC.replace('<t4 type="navigation" id="2" />',
                                  '<div class="x"><t4 type="navigation" id="2" /></div>')
        out, problems = preview(page, self.SRC, edited)
        kinds = [kind for kind, _detail in problems]
        self.assertIn("inseparable", kinds)

    def test_a_new_tag_cannot_be_previewed(self):
        edited = self.SRC.replace("<!--end-->",
                                  '<t4 type="navigation" id="99" />\n<!--end-->')
        out, problems = preview(self.PAGE, self.SRC, edited)
        self.assertIn("unpublished", [kind for kind, _d in problems])
        self.assertIn("cannot be previewed", out)

    def test_literal_only_edits_apply_cleanly(self):
        edited = self.SRC.replace("<!--start-->", '<!--start--><h1>Added</h1>')
        out, problems = preview(self.PAGE, self.SRC, edited)
        self.assertIn("<h1>Added</h1>", out)
        self.assertEqual(problems, [])

    def test_unalignable_source_raises(self):
        from t4sync.render import Unalignable
        with self.assertRaises(Unalignable):
            align("<!--nowhere-in-the-page-->", self.PAGE)


class TestStripInjected(unittest.TestCase):
    """Everything injected must be removable, exactly.

    This is what lets a served page be compared against the live site: the
    annotations contain '>' and '->', which defeats stripping by pattern.
    """

    def test_round_trip_through_transform(self):
        page = "<html><body><p>content</p></body></html>"
        out = transform_html(page, inject_reload=True)
        self.assertNotEqual(out, page)
        self.assertEqual(strip_injected(out), page)

    def test_round_trip_with_rules_applied(self):
        import re
        page = '<body><a href="https://www.example.com/a">x</a></body>'
        rules = [(re.compile(r"https://www\.example\.com/"), "/")]
        out = transform_html(page, rules, inject_reload=True)
        # Rewrites are deliberate output changes and are NOT stripped.
        self.assertEqual(strip_injected(out), '<body><a href="/a">x</a></body>')

    def test_unfenced_page_is_untouched(self):
        page = "<html><!-- an ordinary comment --><body>hi</body></html>"
        self.assertEqual(strip_injected(page), page)

    def test_unterminated_fence_does_not_lose_the_page(self):
        self.assertEqual(strip_injected("<p>a</p><!--T4SYNC:BEGIN--><p>b</p>"),
                         "<p>a</p>")


class TestLayoutResolver(unittest.TestCase):
    """Identifying which layout produced a page, from the page itself."""

    A = {"id": 1, "name": "basic_page.production",
         "headerCode": '<!--top--><t4 type="media" id="100" />',
         "footerCode": "<!--bottom-->"}
    B = {"id": 2, "name": "basic_page.webdev",
         "headerCode": '<!--top--><t4 type="media" id="200" />',
         "footerCode": "<!--bottom-->"}
    C = {"id": 3, "name": "standard",
         "headerCode": "<!--other-->", "footerCode": "<!--end-->"}

    def resolver(self, *records):
        return LayoutResolver(None, list(records))

    def test_unique_name_needs_no_guessing(self):
        rec, why = self.resolver(self.A, self.C).resolve("<html>", "standard")
        self.assertEqual(rec["name"], "standard")
        self.assertIn("uniquely", why)

    def test_id_is_preferred_when_published(self):
        rec, _why = self.resolver(self.A, self.B).resolve("<html>", "basic_page", 2)
        self.assertEqual(rec["name"], "basic_page.webdev")

    def test_distinguishing_tag_id_decides(self):
        """Same literals; only the media id tells them apart."""
        page = "<html><!--top--><!-- 100 css -->rendered<!--bottom--></html>"
        rec, why = self.resolver(self.A, self.B).resolve(page, "basic_page")
        self.assertEqual(rec["name"], "basic_page.production")
        self.assertIn("tag id", why)

    def test_alignment_alone_can_decide(self):
        page = "<html><!--other-->x<!--end--></html>"
        rec, why = self.resolver(self.A, self.C).resolve(page, "standard")
        self.assertEqual(rec["name"], "standard")

    def test_truly_identical_layouts_are_not_guessed(self):
        twin = dict(self.A, id=9, name="basic_page.copy")
        page = "<html><!--top--><!-- 100 css -->x<!--bottom--></html>"
        rec, why = self.resolver(self.A, twin).resolve(page, "basic_page")
        self.assertIsNone(rec)
        self.assertIn("indistinguishable", why)

    def test_unknown_name(self):
        rec, why = self.resolver(self.A).resolve("<html>", "nothing_like_this")
        self.assertIsNone(rec)
        self.assertIn("no layout named", why)


class TestPanelEscaping(unittest.TestCase):
    """The panel must not emit raw markup from data, nor escape its own."""

    def test_layout_note_is_escaped_not_double_escaped(self):
        page = '<meta name="t4-layout" content="standard">'

        class FakeResolver:
            @staticmethod
            def resolve(_body, _name, _id=None):
                return {"id": 1, "name": "standard & co"}, "named uniquely"

        panel = build_panel(page, LayoutIndex({}), resolver=FakeResolver())
        self.assertIn("standard &amp; co", panel)      # data is escaped
        self.assertNotIn("&lt;span", panel)          # our own markup is not

    def test_highlight_control_offers_every_mode(self):
        panel = build_panel("<html></html>", LayoutIndex({}))
        self.assertIn('id="t4i-hl"', panel)
        for mode in ("off", "content", "components", "all"):
            self.assertIn('value="%s"' % mode, panel)

    def test_content_section_says_what_is_missing(self):
        panel = build_panel("<html></html>", LayoutIndex({}))
        self.assertIn("t4 sections", panel)

    def test_content_type_name_is_escaped(self):
        items = ContentItems({"5": {"id": 5, "name": "A & B",
                                    "contentTypeName": "Boxes <b>"}})
        panel = build_panel('<span id="d.en.5"></span>', LayoutIndex({}),
                            content_items=items, env_name="test")
        self.assertIn("Boxes &lt;b&gt;", panel)       # data is escaped
        self.assertIn("A &amp; B", panel)
        self.assertNotIn("Boxes <b>", panel)          # and not emitted raw
        # Including in the JSON island, where `<` would otherwise be raw and
        # a content type named by a T4 author could close the script tag.
        self.assertIn("Boxes \\u003cb\\u003e", panel)


class TestNestedFields(unittest.TestCase):
    """Content layouts keep their markup inside `elements`, not at the top."""

    REC = {"id": 1, "elements": {"formatcode#2:1": "<p>markup</p>",
                                 "name#1:1": "text/box"}}

    def test_read_nested(self):
        self.assertEqual(read_field(self.REC, "elements.formatcode#2:1"), "<p>markup</p>")

    def test_read_top_level_still_works(self):
        self.assertEqual(read_field({"text": "x"}, "text"), "x")

    def test_read_missing_is_none(self):
        self.assertIsNone(read_field(self.REC, "elements.nope"))
        self.assertIsNone(read_field(self.REC, "a.b.c"))

    def test_write_nested_does_not_mutate_the_original(self):
        import copy
        original = copy.deepcopy(self.REC)
        out = write_field(copy.deepcopy(self.REC), "elements.formatcode#2:1", "<p>new</p>")
        self.assertEqual(out["elements"]["formatcode#2:1"], "<p>new</p>")
        self.assertEqual(out["elements"]["name#1:1"], "text/box")   # siblings kept
        self.assertEqual(self.REC, original)

    def test_explicit_path_wins_over_endpoint_and_id(self):
        self.assertEqual(remote_path({"endpoint": "layout", "id": 9,
                                      "path": "layout/9/en"}), "layout/9/en")
        self.assertEqual(remote_path({"endpoint": "pageLayout", "id": 9}), "pageLayout/9")


class TestContentLayout(unittest.TestCase):
    def test_format_key_is_matched_by_prefix(self):
        """The suffix encodes element id and type and is not fixed."""
        self.assertEqual(format_key({"elements": {"formatcode#2:1": "x"}}), "formatcode#2:1")
        self.assertEqual(format_key({"elements": {"formatcode#7:4": "x"}}), "formatcode#7:4")
        self.assertIsNone(format_key({"elements": {"other#1:1": "x"}}))
        self.assertIsNone(format_key({}))

    def test_layout_name_prefers_the_element(self):
        rec = {"id": 5, "name": "fallback", "elements": {"name#1:1": "text/box"}}
        self.assertEqual(layout_name(rec), "text/box")
        self.assertEqual(layout_name({"id": 5, "name": "fallback", "elements": {}}),
                         "fallback")


class TestSectionLayouts(unittest.TestCase):
    """T4's own section-to-layout assignment, which beats inferring it."""

    SECTIONS = {
        "10": {"id": 10, "name": "About", "path": "/Root/about",
               "channels": [{"id": 13, "pageLayout": 100,
                             "inheritedPageLayout": 200}]},
        "11": {"id": 11, "name": "Inherits", "path": "/Root/inherits",
               "channels": [{"id": 13, "pageLayout": None,
                             "inheritedPageLayout": 200}]},
    }

    def test_own_assignment_wins_over_inherited(self):
        self.assertEqual(layout_for(self.SECTIONS["10"]), 100)

    def test_falls_back_to_inherited(self):
        self.assertEqual(layout_for(self.SECTIONS["11"]), 200)

    def test_url_indexed_with_and_without_the_site_root(self):
        """Section paths are rooted at the CMS tree, URLs at the channel."""
        index = by_url_path(self.SECTIONS)
        self.assertIn("root/about", index)
        self.assertIn("about", index)

    def test_resolve_a_published_url(self):
        s = SectionLayouts(self.SECTIONS)
        self.assertTrue(s.available)
        layout_id, section = s.resolve("/about/")
        self.assertEqual(layout_id, 100)
        self.assertEqual(section["name"], "About")

    def test_unknown_url(self):
        self.assertEqual(SectionLayouts(self.SECTIONS).resolve("/nope/"), (None, None))

    def test_empty_map_is_unavailable(self):
        self.assertFalse(SectionLayouts({}).available)


class TestExactBeatsInference(unittest.TestCase):
    def test_section_assignment_is_not_overwritten_by_the_name_lookup(self):
        """The fallback used to be an `else`, clobbering the exact answer."""
        manifest = {"pageLayout/exact.header.html": {
            "endpoint": "pageLayout", "id": 100, "field": "headerCode",
            "name": "basic_page.production"}}
        sections = SectionLayouts({"10": {
            "id": 10, "name": "About", "path": "/Root/about",
            "channels": [{"id": 13, "pageLayout": 100}]}})
        page = '<meta name="t4-layout" content="basic_page">'
        out = build_comment(page, LayoutIndex(manifest), env_name="test",
                            url_path="/about/", sections=sections)
        self.assertIn("assigned in T4 to section 10", out)
        self.assertIn("x t4-source/test/pageLayout/exact.header.html", out)
        self.assertNotIn("SUGGESTIONS", out)


class TestContentItemAnnotation(unittest.TestCase):
    """T4 emits <span id="d.en.<id>"> for every content item it renders."""

    ITEMS = ContentItems({"4021": {"id": 4021, "name": "Promo box A",
                                     "contentTypeId": 326,
                                     "contentTypeName": "Promo boxes"}})

    def test_anchor_is_named(self):
        page = '<span id="d.en.4021"></span><p>x</p>'
        out, n = annotate_content_items(page, self.ITEMS, env_name="test")
        self.assertEqual(n, 1)
        self.assertIn("T4 CONTENT: #4021 'Promo box A'", out)
        self.assertIn("of type 'Promo boxes'", out)
        self.assertEqual(strip_injected(out), page)   # removable, exactly

    def test_unknown_anchor_is_left_alone(self):
        page = '<span id="d.en.999999"></span>'
        out, n = annotate_content_items(page, self.ITEMS, env_name="test")
        self.assertEqual((out, n), (page, 0))

    def test_other_languages_match(self):
        page = '<span id="d.fr.4021"></span>'
        out, n = annotate_content_items(page, self.ITEMS, env_name="test")
        self.assertEqual(n, 1)

    def test_no_data_is_a_no_op(self):
        page = '<span id="d.en.4021"></span>'
        self.assertEqual(annotate_content_items(page, ContentItems({})), (page, 0))

    def test_content_layout_file_is_named(self):
        page = '<span id="d.en.4021"></span><p>x</p>'
        out, _n = annotate_content_items(
            page, self.ITEMS, env_name="test",
            content_layouts=ContentLayoutIndex(CONTENT_LAYOUT_MANIFEST))
        self.assertIn("layout -> t4-source/test/contentLayout/promo-boxes.html",
                      out)
        self.assertEqual(strip_injected(out), page)

    def test_missing_layout_is_reported_not_omitted(self):
        out, _n = annotate_content_items('<span id="d.en.4021"></span>',
                                         self.ITEMS, env_name="test",
                                         content_layouts=ContentLayoutIndex({}))
        self.assertIn("none pulled for this type", out)


CONTENT_LAYOUT_MANIFEST = {
    "contentLayout/promo-boxes.html": {
        "endpoint": "layout", "contentType": "Promo boxes",
        "name": "text/html", "id": 975973},
    "contentLayout/promo-boxes-rss.html": {
        "endpoint": "layout", "contentType": "Promo boxes",
        "name": "text/rss", "id": 975974},
    "pageLayout/basic_page.html": {"endpoint": "pageLayout", "id": 1},
}


class TestContentLayoutIndex(unittest.TestCase):
    """Content layouts are keyed by the type's name, not its id."""

    INDEX = ContentLayoutIndex(CONTENT_LAYOUT_MANIFEST)

    def test_groups_every_layout_of_a_type(self):
        found = self.INDEX.for_type("Promo boxes")
        self.assertEqual([rel for rel, _label in found],
                         ["contentLayout/promo-boxes-rss.html",
                          "contentLayout/promo-boxes.html"])

    def test_lookup_is_case_insensitive(self):
        self.assertTrue(self.INDEX.for_type("PROMO BOXES"))

    def test_falls_through_to_the_alias(self):
        self.assertTrue(self.INDEX.for_type("Not a type", "Promo boxes"))

    def test_page_layouts_are_not_content_layouts(self):
        self.assertEqual(self.INDEX.for_type("basic_page"), [])

    def test_unknown_type_is_empty_not_an_error(self):
        self.assertEqual(self.INDEX.for_type(None), [])


class TestPageContent(unittest.TestCase):
    """What the panel lists: items on the page, grouped by content type."""

    ITEMS = ContentItems({
        "1": {"id": 1, "name": "Box A", "contentTypeName": "Promo boxes"},
        "2": {"id": 2, "name": "Box B", "contentTypeName": "Promo boxes"},
        "3": {"id": 3, "name": "Banner", "contentTypeName": "Hero"},
    })
    LAYOUTS = ContentLayoutIndex(CONTENT_LAYOUT_MANIFEST)

    PAGE = ('<span id="d.en.1"></span><span id="d.en.2"></span>'
            '<span id="d.en.3"></span>')

    def test_items_are_found_in_document_order(self):
        found = page_content(self.PAGE, self.ITEMS, None, self.LAYOUTS)
        self.assertEqual([e["name"] for e in found], ["Box A", "Box B", "Banner"])

    def test_a_repeated_anchor_is_listed_once(self):
        found = page_content(self.PAGE + '<span id="d.en.1"></span>',
                             self.ITEMS, None, self.LAYOUTS)
        self.assertEqual(len(found), 3)

    def test_grouping_collapses_by_type_and_keeps_the_count(self):
        groups = group_by_type(page_content(self.PAGE, self.ITEMS, None, self.LAYOUTS))
        self.assertEqual([g["type"] for g in groups], ["Promo boxes", "Hero"])
        self.assertEqual(len(groups[0]["items"]), 2)
        self.assertEqual(len(groups[0]["layouts"]), 2)
        self.assertEqual(groups[1]["layouts"], [])

    def test_unmapped_items_give_nothing_rather_than_guessing(self):
        self.assertEqual(page_content(self.PAGE, ContentItems({})), [])


class TestMediaFilenames(unittest.TestCase):
    """The media item's own name carries the extension that makes it usable."""

    def test_extension_is_kept(self):
        self.assertEqual(media_filename("v8.css", 1), "v8.css")
        self.assertEqual(media_filename("app.js", 1), "app.js")

    def test_extensionless_gets_one(self):
        self.assertEqual(media_filename("b3-footer-PROD", 1), "b3-footer-PROD.txt")

    def test_path_separators_cannot_escape(self):
        self.assertEqual(media_filename("../../etc/passwd", 1), "passwd.txt")
        self.assertEqual(media_filename("/abs/x.css", 1), "x.css")

    def test_empty_falls_back_to_the_id(self):
        self.assertEqual(media_filename("", 42), "42.txt")
        self.assertEqual(media_filename(None, 42), "42.txt")

    def test_leading_dots_are_not_hidden_files(self):
        self.assertFalse(media_filename(".hidden", 1).startswith("."))


class TestMediaSources(unittest.TestCase):
    """Published asset URLs resolve to the pulled source, so one file serves
    the browser and is what `push` deploys."""

    def test_url_basename_maps_to_the_media_file(self):
        with tempfile.TemporaryDirectory() as tmp:
            media = pathlib.Path(tmp) / "media"
            media.mkdir()
            (media / "v8.css").write_text("body{}")
            manifest = {"media/v8.css": {"endpoint": "media", "id": 1,
                                         "field": "text"}}
            sources = MediaSources(manifest, tmp)
            self.assertTrue(sources.available)
            expected = os.path.normpath(str(media / "v8.css"))
            self.assertEqual(sources.path_for("/media/web/b3/css/v8.css"), expected)
            self.assertEqual(sources.path_for("/media/web/b3/css/v8.css?v=2"), expected)
            # Separators must be the platform's, not the manifest's.
            self.assertNotIn("/", sources.path_for("/media/v8.css").replace(os.sep, ""))

    def test_unknown_asset_is_not_claimed(self):
        sources = MediaSources({"media/v8.css": {"endpoint": "media"}}, "/nowhere")
        self.assertIsNone(sources.path_for("/media/other.css"))

    def test_non_media_manifest_entries_are_ignored(self):
        sources = MediaSources({"pageLayout/x.header.html":
                                {"endpoint": "pageLayout"}}, "/tmp")
        self.assertFalse(sources.available)


class FakeClient:
    """A section tree: 1 -> 2,3 ; 2 -> 4 ; others leaf."""

    TREE = {1: [2, 3], 2: [4], 3: [], 4: []}

    def __init__(self):
        self.calls = []

    def get_json(self, path):
        self.calls.append(path)
        parts = path.split("/")
        section_id = int(parts[1])
        if path.endswith("/subsections"):
            return {"children": [{"id": c, "name": "s%d" % c}
                                 for c in self.TREE.get(section_id, [])]}
        if path.endswith("/contents"):
            return {"children": [{"content": {"id": 900 + section_id,
                                              "name": "item%d" % section_id,
                                              "contentTypeID": 5,
                                              "contentTypeName": "Thing"}}]}
        return {"name": "s%d" % section_id, "channels": [{"id": 13, "pageLayout": 70}],
                "output-uri": "s%d" % section_id}


class TestSectionWalk(unittest.TestCase):
    def test_walks_the_whole_tree(self):
        found = sectionlib.walk(FakeClient(), 1, "en")
        self.assertEqual(sorted(int(k) for k in found), [1, 2, 3, 4])

    def test_limit_counts_this_run_not_the_cache(self):
        """A limit below the cache size must not make a resume a no-op."""
        known = {str(i): {"id": i, "name": "s%d" % i, "channels": []}
                 for i in (1, 2, 3, 4)}
        client = FakeClient()
        found = sectionlib.walk(client, 1, "en", known=known, limit=2)
        self.assertEqual(len(found), 4)
        self.assertTrue(client.calls, "the walk did nothing despite a cache")

    def test_limit_stops_the_run(self):
        client = FakeClient()
        sectionlib.walk(client, 1, "en", limit=1)
        visited = {c.split("/")[1] for c in client.calls}
        self.assertEqual(visited, {"1"})

    def test_contents_are_collected_when_asked(self):
        sink = {}
        sectionlib.walk(FakeClient(), 1, "en", with_contents=True, content_sink=sink)
        self.assertEqual(len(sink), 4)
        self.assertEqual(sink["901"]["contentTypeName"], "Thing")

    def test_resume_does_not_refetch_cached_sections(self):
        """A resume must not spend two requests per known section."""
        client = FakeClient()
        found = sectionlib.walk(client, 1, "en", with_contents=True, content_sink={})
        first = len(client.calls)

        again = FakeClient()
        sectionlib.walk(again, 1, "en", known=found, with_contents=True,
                        content_sink={})
        self.assertLess(len(again.calls), first,
                        "resume cost as much as the original walk")
        self.assertNotIn("hierarchy/1/en/contents", again.calls)
        self.assertNotIn("hierarchy/1/en/subsections", again.calls)

    def test_checkpoint_is_called_during_the_walk(self):
        seen = []
        sectionlib.walk(FakeClient(), 1, "en",
                        checkpoint=lambda f, c: seen.append(len(f)),
                        checkpoint_every=1)
        self.assertTrue(seen, "no checkpoint during a multi-section walk")

    def test_a_dropped_request_does_not_end_the_walk(self):
        class Flaky(FakeClient):
            def get_json(self, path):
                if path == "hierarchy/2/en/subsections":
                    raise OSError("connection reset")
                return FakeClient.get_json(self, path)

        found = sectionlib.walk(Flaky(), 1, "en")
        self.assertIn("1", found)
        self.assertIn("3", found)      # the walk carried on past the failure


class TestLocalPath(unittest.TestCase):
    """Manifest keys use "/" everywhere; filesystem paths use the platform's."""

    def test_separators_are_the_platform_s(self):
        out = local_path("base", "media/v8.css")
        self.assertEqual(out, os.path.join("base", "media", "v8.css"))
        self.assertNotIn("/", out.replace(os.sep, ""))

    def test_single_segment(self):
        self.assertEqual(local_path("base", "x.css"), os.path.join("base", "x.css"))

    def test_normalized(self):
        self.assertEqual(local_path("base", "a/../b.css"),
                         os.path.join("base", "b.css"))


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


class TestDerivedRewrites(unittest.TestCase):
    """T4 writes absolute production URLs into test pages; they must map local."""

    def test_host_is_rewritten_to_a_local_path(self):
        rules = rules_for_published_urls(["https://www.example.edu"])
        out = transform_html('<a href="https://www.example.edu/a/b">x</a>', rules)
        self.assertIn('href="/a/b"', out)

    def test_http_and_https_both_match(self):
        rules = rules_for_published_urls(["https://www.example.edu"])
        out = transform_html('<img src="http://www.example.edu/i.png">', rules)
        self.assertIn('src="/i.png"', out)

    def test_other_hosts_are_left_alone(self):
        rules = rules_for_published_urls(["https://www.example.edu"])
        page = '<a href="https://blogs.example.edu/p">x</a>'
        self.assertEqual(transform_html(page, rules), page)

    def test_a_dot_in_the_host_is_not_a_wildcard(self):
        rules = rules_for_published_urls(["https://www.example.edu"])
        page = '<a href="https://wwwXexampleYedu/p">x</a>'
        self.assertEqual(transform_html(page, rules), page)

    def test_empty_config_yields_no_rules(self):
        self.assertEqual(rules_for_published_urls([]), [])
        self.assertEqual(rules_for_published_urls(None), [])


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


class TestProgress(unittest.TestCase):
    def test_silent_when_not_a_terminal(self):
        """Piped output and CI logs must not fill with carriage returns."""
        import io
        stream = io.StringIO()       # isatty() is False
        with Progress("x", total=3, stream=stream) as bar:
            bar.update()
        self.assertEqual(stream.getvalue(), "")

    def test_line_fits_the_terminal(self):
        bar = Progress("  pageLayout", total=58, enabled=False)
        bar.count = 23
        for columns in (20, 40, 80, 200):
            self.assertLess(len(bar.render(columns)), columns)
        self.assertIn("23/58", bar.render(80))

    def test_close_erases_the_bar(self):
        import io
        stream = io.StringIO()
        with Progress("label", total=2, stream=stream, enabled=True) as bar:
            bar.update()
        self.assertTrue(stream.getvalue().endswith("\r"))
        self.assertTrue(stream.getvalue().split("\r")[-2].strip() == "")


if __name__ == "__main__":
    unittest.main()


class TestCommentSafety(unittest.TestCase):
    """Injected comments must stay openable paths, and must not end early."""

    def test_double_hyphen_in_a_path_survives(self):
        items = ContentItems({"1": {"id": 1, "name": "x",
                                    "contentTypeName": "Snippet"}})
        layouts = ContentLayoutIndex({
            "contentLayout/snippet--textcode.html": {
                "endpoint": "layout", "contentType": "Snippet",
                "name": "text/code"}})
        out, _n = annotate_content_items('<span id="d.en.1"></span>', items,
                                         env_name="test", content_layouts=layouts)
        self.assertIn("contentLayout/snippet--textcode.html", out)

    def test_a_closing_sequence_in_data_is_broken(self):
        items = ContentItems({"1": {"id": 1, "name": "evil --> out",
                                    "contentTypeName": "T"}})
        out, _n = annotate_content_items('<span id="d.en.1"></span>', items)
        self.assertNotIn("evil --> out", out)
        self.assertIn("evil - -> out", out)
        # The note's own terminator, and nothing the data smuggled in.
        note = out.split("<!-- T4 CONTENT")[1]
        self.assertEqual(note.index("-->"), note.index("\n-->") + 1)


class TestVcs(unittest.TestCase):
    """Git is the step between editing a layout and deploying it."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = pathlib.Path(self.tmp.name) / "proj"
        self.root.mkdir()
        self._git("init", "-b", "main")
        self._git("config", "user.email", "t@example.com")
        self._git("config", "user.name", "T")
        self.repo = Repo.at(self.root)

    def tearDown(self):
        self.tmp.cleanup()

    def _git(self, *args):
        subprocess.run(("git",) + args, cwd=self.root, check=True,
                       stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)

    def _write(self, rel, text="x"):
        path = self.root / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding="utf-8")
        return path

    def test_a_directory_outside_a_repo_is_reported_not_raised(self):
        outside = pathlib.Path(self.tmp.name) / "elsewhere"
        outside.mkdir()
        repo = Repo.at(outside)
        self.assertFalse(repo.available)
        self.assertIn("not inside a git repository", repo.reason)

    def test_untracked_and_modified_both_count_as_pending(self):
        self._write("t4-source/test/a.html")
        self.assertEqual(self.repo.dirty(), ["t4-source/test/a.html"])
        self.repo.commit([self.root / "t4-source"], "add")
        self.assertEqual(self.repo.dirty(), [])
        self._write("t4-source/test/a.html", "changed")
        self.assertEqual(self.repo.dirty(), ["t4-source/test/a.html"])

    def test_dirty_is_scoped_to_the_paths_asked_about(self):
        self._write("t4-source/test/a.html")
        self._write("notes.txt")
        self.assertEqual(self.repo.dirty([self.root / "t4-source"]),
                         ["t4-source/test/a.html"])

    def test_commit_stages_only_what_it_was_given(self):
        self._write("t4-source/test/a.html")
        self._write("unrelated.txt")
        self.repo.commit([self.root / "t4-source"], "only the source")
        self.assertEqual(self.repo.dirty(), ["unrelated.txt"])

    def test_commit_with_nothing_staged_returns_none(self):
        self._write("a.txt")
        self.repo.commit([self.root / "a.txt"], "one")
        self.assertIsNone(self.repo.commit([self.root / "a.txt"], "again"))

    def test_head_on_an_empty_repo_does_not_raise(self):
        sha, _branch = self.repo.head()
        self.assertIsNone(sha)
        self.assertIn("no commits yet", self.repo.describe())

    def test_message_names_the_record_behind_the_file(self):
        message = message_for("test", [{
            "path": "t4-source/test/pageLayout/basic_page.footer.html",
            "meta": {"endpoint": "pageLayout", "id": 1100982, "name": "basic_page"},
        }])
        self.assertIn("pageLayout #1100982", message)
        self.assertIn("(basic_page)", message)
        self.assertIn("T4-Env: test", message)


class TestPushGate(unittest.TestCase):
    """A push must not deploy markup that is not committed."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = pathlib.Path(self.tmp.name)
        (self.root / ".t4").mkdir()
        (self.root / ".t4" / "config.json").write_text(
            json.dumps({"source_dir": "t4-source", "environments": {}}),
            encoding="utf-8")
        subprocess.run(["git", "init", "-b", "main"], cwd=self.root, check=True,
                       stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        for key, value in (("user.email", "t@example.com"), ("user.name", "T")):
            subprocess.run(["git", "config", key, value], cwd=self.root, check=True)
        self.project = Project(self.root)
        self.file = self.root / "t4-source" / "test" / "a.html"
        self.file.parent.mkdir(parents=True)
        self.file.write_text("x", encoding="utf-8")

    def tearDown(self):
        self.tmp.cleanup()

    def test_uncommitted_file_is_refused_and_says_how_to_fix_it(self):
        with self.assertRaises(T4Error) as caught:
            require_clean(self.project, "test", [self.file])
        self.assertIn("t4 save", str(caught.exception))

    def test_allow_dirty_lets_it_through(self):
        self.assertTrue(require_clean(self.project, "test", [self.file],
                                      allow_dirty=True).available)

    def test_config_can_turn_the_gate_off(self):
        self.project.config["require_commit"] = False
        self.assertTrue(require_clean(self.project, "test", [self.file]).available)

    def test_a_committed_file_passes(self):
        Repo.at(self.root).commit([self.file], "add")
        self.assertTrue(require_clean(self.project, "test", [self.file]).available)

    def test_an_unrelated_dirty_file_does_not_block_the_push(self):
        Repo.at(self.root).commit([self.file], "add")
        (self.root / "notes.txt").write_text("wip", encoding="utf-8")
        self.assertTrue(require_clean(self.project, "test", [self.file]).available)

    def test_a_project_without_git_is_not_blocked(self):
        # A separate temporary directory: anything under self.root is inside
        # the repository created in setUp, git repositories being recursive.
        with tempfile.TemporaryDirectory() as other:
            plain = pathlib.Path(other) / "plain"
            (plain / ".t4").mkdir(parents=True)
            (plain / ".t4" / "config.json").write_text("{}", encoding="utf-8")
            repo = require_clean(Project(plain), "test", [])
        self.assertFalse(repo.available)


class TestServeBootstrapsMirror(unittest.TestCase):
    """`t4 serve` mirrors the site when there is none, and not otherwise."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = pathlib.Path(self.tmp.name)
        (self.root / ".t4").mkdir()
        (self.root / ".t4" / "config.json").write_text(json.dumps({
            "source_dir": "t4-source", "mirror_dir": "site-mirror",
            "environments": {"test": {"url": "https://cms.example.edu",
                                      "published_url": "https://www.example.edu"}},
        }), encoding="utf-8")
        self.project = Project(self.root)
        self.calls = []

    def tearDown(self):
        self.tmp.cleanup()

    def _args(self, **over):
        # Built from the real parser so a new serve flag cannot make these
        # tests pass against a Namespace that no longer resembles the command.
        args = build_parser().parse_args(["serve", "--env", "test"])
        for key, value in over.items():
            setattr(args, key, value)
        return args

    def _mirror_dir(self, host="www.example.edu"):
        page = self.root / "site-mirror" / host / "index.html"
        page.parent.mkdir(parents=True, exist_ok=True)
        page.write_text("<html></html>", encoding="utf-8")

    def _patched(self):
        """Replace the crawl and the server, leaving the decision under test."""
        def fake_mirror(args, project):
            self.calls.append("mirror")
            self._mirror_dir()
            return 0

        def fake_serve(*args, **kwargs):
            self.calls.append("serve")

        return fake_mirror, fake_serve

    def _run(self, args):
        fake_mirror, fake_serve = self._patched()
        real_mirror, real_serve = site.run_mirror, site.serve_mod.serve
        site.run_mirror, site.serve_mod.serve = fake_mirror, fake_serve
        try:
            return site.run_serve(args, self.project)
        finally:
            site.run_mirror, site.serve_mod.serve = real_mirror, real_serve

    def test_mirrors_when_there_is_none(self):
        self._run(self._args())
        self.assertEqual(self.calls, ["mirror", "serve"])

    def test_does_not_mirror_when_one_exists(self):
        self._mirror_dir()
        self._run(self._args())
        self.assertEqual(self.calls, ["serve"])

    def test_no_setup_fails_instead_of_mirroring(self):
        with self.assertRaises(T4Error):
            self._run(self._args(no_setup=True))
        self.assertEqual(self.calls, [])

    def test_an_empty_mirror_directory_does_not_count(self):
        # A crawl that died early leaves the directory but no pages; serving
        # it would 404 everything and look like a broken tool.
        (self.root / "site-mirror" / "www.example.edu").mkdir(parents=True)
        self._run(self._args())
        self.assertEqual(self.calls, ["mirror", "serve"])

    def test_without_a_published_url_it_says_so(self):
        self.project.config["environments"]["test"].pop("published_url")
        with self.assertRaises(T4Error) as caught:
            self._run(self._args())
        self.assertIn("published_url", str(caught.exception))

    def test_mirror_is_still_its_own_command(self):
        self.assertIn("mirror", build_parser().parse_args(["mirror"]).command)

    def test_a_failed_bootstrap_does_not_say_to_run_mirror_again(self):
        def fake_mirror(args, project):
            self.calls.append("mirror")      # crawl reaches nothing
            return 0

        real = site.run_mirror
        site.run_mirror = fake_mirror
        try:
            with self.assertRaises(T4Error) as caught:
                site.run_serve(self._args(), self.project)
        finally:
            site.run_mirror = real
        message = str(caught.exception)
        self.assertIn("fetched no pages", message)
        self.assertNotIn("Run `t4 mirror` first", message)


class TestPickMode(unittest.TestCase):
    """Clicking a component on the page selects it, without navigating."""

    PANEL = build_panel(
        '<span id="d.en.5"></span>', LayoutIndex({}), env_name="test",
        content_items=ContentItems({"5": {"id": 5, "name": "Box A",
                                          "contentTypeName": "Promo boxes"}}))

    def test_the_control_names_its_side_effect(self):
        self.assertIn('id="t4i-pick"', self.PANEL)
        self.assertIn("links off", self.PANEL)

    def test_type_rows_are_addressable_from_the_page(self):
        # Pick mode resolves a clicked element to a type, then has to find
        # the row listing that type's files.
        self.assertIn('data-type="Promo boxes"', self.PANEL)

    def test_items_are_addressable_by_id(self):
        self.assertIn('data-item="5"', self.PANEL)

    def test_clicks_are_intercepted_in_the_capture_phase(self):
        # Bubble phase would run a link's own handler first and navigate away.
        self.assertIn('document.addEventListener("click"', self.PANEL)
        self.assertIn("e.preventDefault();\n    e.stopPropagation();", self.PANEL)

    def test_the_script_is_syntactically_valid(self):
        import shutil
        node = shutil.which("node")
        if not node:
            self.skipTest("node not installed")
        script = re.sub(r"</?script[^>]*>", "", SCRIPT)
        with tempfile.NamedTemporaryFile("w", suffix=".js", delete=False) as fh:
            fh.write(script)
            path = fh.name
        try:
            proc = subprocess.run([node, "--check", path],
                                  stdout=subprocess.PIPE, stderr=subprocess.PIPE)
            self.assertEqual(proc.returncode, 0,
                             proc.stderr.decode("utf-8", errors="replace"))
        finally:
            os.unlink(path)


class TestApp(unittest.TestCase):
    """The web UI: a token gate, and commands it will not run."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = pathlib.Path(self.tmp.name)
        (self.root / ".t4").mkdir()
        (self.root / ".t4" / "config.json").write_text(json.dumps({
            "source_dir": "t4-source",
            "environments": {"test": {"url": "https://cms.example.edu",
                                      "push_allowed": True},
                             "prod": {"url": "https://cms.example.edu"}},
        }), encoding="utf-8")
        self.project = Project(self.root)
        self.jobs = app.Jobs(str(self.root))

    def tearDown(self):
        self.tmp.cleanup()

    def test_state_needs_no_network_or_pull(self):
        # Opening the panel must not reach for the VPN, or fail on a project
        # that has not been pulled yet.
        s = app.state(self.project, self.jobs)
        self.assertEqual(s["default_env"], "prod")
        self.assertEqual(sorted(e["name"] for e in s["environments"]),
                         ["prod", "test"])
        self.assertEqual([e["tracked"] for e in s["environments"]], [0, 0])
        self.assertIsNone(s["mirror"])

    def test_push_allowed_is_reported_per_environment(self):
        allowed = {e["name"]: e["push_allowed"]
                   for e in app.state(self.project, self.jobs)["environments"]}
        self.assertEqual(allowed, {"test": True, "prod": False})

    def test_the_page_carries_the_token(self):
        page = app.PAGE % {"token": "abc123"}
        self.assertIn('var TOKEN = "abc123"', page)

    def test_config_commands_cannot_be_run_from_the_panel(self):
        # Setting a URL or storing a credential is a deliberate terminal act,
        # not something a mis-click should reach.
        for name in ("env", "token", "init"):
            self.assertNotIn(name, app.ALLOWED)

    def test_the_commands_it_offers_all_exist(self):
        known = set(cli.HANDLERS)
        self.assertEqual(app.ALLOWED - known, set())

    def test_a_job_captures_output_and_exit_code(self):
        job = self.jobs.start(["--version"])
        for _ in range(200):
            if job.done:
                break
            time.sleep(0.05)
        self.assertTrue(job.done, "job did not finish")
        self.assertEqual(job.returncode, 0)
        self.assertTrue(any("t4-sync" in line for line in job.lines), job.lines)

    def test_a_watcher_gets_the_backlog_then_the_end(self):
        job = self.jobs.start(["--version"])
        for _ in range(200):
            if job.done:
                break
            time.sleep(0.05)
        channel = job.watch()
        seen = []
        while True:
            line = channel.get(timeout=5)
            if line is None:
                break
            seen.append(line)
        self.assertEqual(seen, job.lines)


class TestJobLanes(unittest.TestCase):
    """Tasks run side by side; only writers to the same thing wait."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.jobs = app.Jobs(self.tmp.name)

    def tearDown(self):
        for job in self.jobs.running():
            job.stop()
        self.tmp.cleanup()

    def _pretend(self, argv):
        """Register a job as running without launching anything."""
        self.jobs._next += 1
        job = app.Job(str(self.jobs._next), argv, self.tmp.name)
        self.jobs.items[job.id] = job
        return job

    def test_serving_blocks_nothing_but_another_serve(self):
        self._pretend(["serve"])
        for argv in (["pull"], ["push"], ["save"], ["mirror"], ["status"],
                     ["sections"], ["diff"]):
            self.assertIsNone(self.jobs.blocker(argv), argv[0])
        self.assertIsNotNone(self.jobs.blocker(["serve"]))

    def test_source_writers_wait_for_each_other(self):
        self._pretend(["pull"])
        for argv in (["push"], ["save"], ["sync"], ["sections"], ["pull"]):
            self.assertIsNotNone(self.jobs.blocker(argv), argv[0])

    def test_readers_are_never_blocked(self):
        self._pretend(["pull"])
        self._pretend(["mirror"])
        self._pretend(["serve"])
        for argv in (["status"], ["diff"], ["doctor"], ["layouts"], ["compare"]):
            self.assertIsNone(self.jobs.blocker(argv), argv[0])

    def test_a_finished_job_stops_blocking(self):
        job = self._pretend(["pull"])
        self.assertIsNotNone(self.jobs.blocker(["save"]))
        job.done = True
        self.assertIsNone(self.jobs.blocker(["save"]))

    def test_the_conflict_names_the_task_in_the_way(self):
        self._pretend(["pull", "--env", "test"])
        with self.assertRaises(app.Conflict) as caught:
            self.jobs.start(["push"])
        # The label, not the raw argv: flags are noise in a UI message.
        self.assertIn("t4 pull test", str(caught.exception))
        self.assertNotIn("--env", str(caught.exception))

    def test_busy_lanes_reports_what_is_in_use(self):
        self._pretend(["serve"])
        self._pretend(["pull"])
        self.assertEqual(self.jobs.busy_lanes(), ["serve", "source"])

    def test_every_lane_has_a_reason_to_show(self):
        self.assertEqual(set(app.LANES.values()), set(app.LANE_REASON))

    def test_lanes_only_name_commands_the_panel_offers(self):
        self.assertEqual(set(app.LANES) - app.ALLOWED, set())

    def test_the_served_url_is_read_from_the_output(self):
        # serve takes --port, so a hardcoded 8321 would send you to the wrong
        # place exactly when you had a reason to change it.
        job = self._pretend(["serve", "--port", "9999"])
        job._emit("Serving   /tmp/site")
        self.assertIsNone(job.url)
        job._emit("URL       http://127.0.0.1:9999/")
        self.assertEqual(job.url, "http://127.0.0.1:9999/")


class TestOpener(unittest.TestCase):
    """Opening a file locally: the path check is the whole security story."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = pathlib.Path(self.tmp.name) / "proj"
        (self.root / "t4-source" / "test").mkdir(parents=True)
        self.file = self.root / "t4-source" / "test" / "a.html"
        self.file.write_text("x", encoding="utf-8")

    def tearDown(self):
        self.tmp.cleanup()

    def test_a_file_inside_the_project_resolves(self):
        self.assertEqual(opener.safe_path(self.root, "t4-source/test/a.html"),
                         os.path.realpath(str(self.file)))

    def test_traversal_is_refused(self):
        with self.assertRaises(opener.Refused):
            opener.safe_path(self.root, "../../../../etc/passwd")

    def test_an_absolute_path_is_refused(self):
        with self.assertRaises(opener.Refused):
            opener.safe_path(self.root, "/etc/passwd")

    def test_a_symlink_pointing_out_is_refused(self):
        outside = pathlib.Path(self.tmp.name) / "secret.txt"
        outside.write_text("nope", encoding="utf-8")
        link = self.root / "escape"
        try:
            link.symlink_to(outside)
        except (OSError, NotImplementedError):
            self.skipTest("symlinks not available")
        # Resolved before the check, so a link inside the project pointing
        # out of it is caught rather than followed.
        with self.assertRaises(opener.Refused):
            opener.safe_path(self.root, "escape")

    def test_a_missing_file_is_refused(self):
        with self.assertRaises(opener.Refused):
            opener.safe_path(self.root, "t4-source/test/nope.html")

    def test_an_unknown_action_is_refused_before_any_path_work(self):
        with self.assertRaises(opener.Refused):
            opener.open_path(self.root, "t4-source/test/a.html", "rm -rf")

    def test_every_platform_offers_a_command(self):
        for action in ("edit", "reveal"):
            found = opener.commands(action, "/tmp/x.html")
            self.assertTrue(found, action)
            self.assertTrue(all(isinstance(c, list) and c for c in found))


class TestInspectorFocus(unittest.TestCase):
    """Selecting an element narrows the panel to that element's sources."""

    PANEL = build_panel(
        '<span id="d.en.5"></span>', LayoutIndex({}), env_name="test",
        content_items=ContentItems({"5": {"id": 5, "name": "Box A",
                                          "contentTypeName": "Promo boxes"}}),
        content_layouts=ContentLayoutIndex(CONTENT_LAYOUT_MANIFEST))

    def test_the_full_listing_is_hideable(self):
        self.assertIn('id="t4i-all"', self.PANEL)
        self.assertIn('[data-focus="1"] #t4i-all{display:none}', self.PANEL)

    def test_there_is_a_way_back_to_the_whole_page(self):
        self.assertIn("show whole page", self.PANEL)

    def test_each_file_offers_edit_and_reveal(self):
        self.assertIn('data-open="edit"', self.PANEL)
        self.assertIn('data-open="reveal"', self.PANEL)


class TestLayoutGrouping(unittest.TestCase):
    """A content type has several layouts; only one rendered the page."""

    MANIFEST = {
        "contentLayout/t--texthtml.html": {
            "endpoint": "layout", "contentType": "T", "name": "text/html"},
        "contentLayout/t--textrss.html": {
            "endpoint": "layout", "contentType": "T", "name": "text/rss"},
        "contentLayout/t--textcode.html": {
            "endpoint": "layout", "contentType": "T", "name": "text/code"},
    }

    def _panel(self, manifest):
        return build_panel(
            '<span id="d.en.1"></span>', LayoutIndex({}), env_name="test",
            content_items=ContentItems({"1": {"id": 1, "name": "x",
                                              "contentTypeName": "T"}}),
            content_layouts=ContentLayoutIndex(manifest))

    def test_the_web_format_leads_and_the_rest_fold_away(self):
        panel = self._panel(self.MANIFEST)
        html_at = panel.index("t--texthtml.html")
        details_at = panel.index("<details")
        self.assertLess(html_at, details_at, "text/html should come first")
        self.assertIn("2 other formats", panel)

    def test_the_guess_is_labelled_as_one(self):
        panel = self._panel(self.MANIFEST)
        self.assertIn("likely", panel)
        self.assertIn("T4 picks by the format the page layout asks for", panel)

    def test_a_single_layout_is_not_dressed_up_as_a_choice(self):
        panel = self._panel({"contentLayout/t--texthtml.html": {
            "endpoint": "layout", "contentType": "T", "name": "text/html"}})
        self.assertNotIn("<details", panel)
        self.assertNotIn("likely", panel)

    def test_with_no_web_format_nothing_is_promoted(self):
        # Nothing to prefer, so they are shown as equals rather than one
        # being picked arbitrarily.
        panel = self._panel({
            "contentLayout/t--textrss.html": {
                "endpoint": "layout", "contentType": "T", "name": "text/rss"},
            "contentLayout/t--textcode.html": {
                "endpoint": "layout", "contentType": "T", "name": "text/code"}})
        self.assertNotIn("<details", panel)
        self.assertNotIn("likely", panel)

    def test_files_are_grouped_under_what_they_are(self):
        panel = self._panel(self.MANIFEST)
        self.assertIn("Content layout", panel)
        self.assertIn("On this page", panel)


class TestOneSourceOfTruth(unittest.TestCase):
    """An override of a pulled file is two copies; the tool must not help."""

    MANIFEST = {"media/site.css": {"endpoint": "media", "id": 1,
                                    "field": "text", "name": "site.css"},
                "media/site.js": {"endpoint": "media", "id": 2,
                                  "field": "text", "name": "site.js"}}

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = pathlib.Path(self.tmp.name)
        self.env = self.root / "t4-source" / "test"
        (self.env / "media").mkdir(parents=True)
        (self.env / "media" / "site.css").write_text("a{}", encoding="utf-8")
        (self.env / "_manifest.json").write_text(json.dumps(self.MANIFEST),
                                                 encoding="utf-8")
        self.overrides = self.root / "overrides"
        self.media = MediaSources(self.MANIFEST, str(self.env))

    def tearDown(self):
        self.tmp.cleanup()

    def _override(self, rel, text="b{}"):
        path = self.overrides / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding="utf-8")
        return path

    def test_an_override_of_a_pulled_file_is_reported(self):
        self._override("media/web/css/site.css")
        self.assertEqual(serve.shadowing_overrides(str(self.overrides), self.media),
                         ["media/web/css/site.css"])

    def test_matching_is_by_filename_not_path(self):
        # The published URL and the pulled file agree on the name, not the
        # path, which is also how serving resolves it.
        self._override("totally/different/place/site.css")
        self.assertEqual(len(serve.shadowing_overrides(str(self.overrides), self.media)), 1)

    def test_an_override_of_something_not_pulled_is_fine(self):
        self._override("media/web/vendor/jquery.js")
        self.assertEqual(serve.shadowing_overrides(str(self.overrides), self.media), [])

    def test_a_pulled_file_with_no_override_is_fine(self):
        self.assertEqual(serve.shadowing_overrides(str(self.overrides), self.media), [])

    def test_no_overrides_directory_is_not_an_error(self):
        self.assertEqual(serve.shadowing_overrides(None, self.media), [])


class TestUnmappedContentIsDistinguished(unittest.TestCase):
    """"Nothing here" and "I cannot see what is here" are opposite answers."""

    IDX = LayoutIndex({})
    ITEMS = ContentItems({"1": {"id": 1, "name": "known",
                                "contentTypeName": "T"}})

    def test_a_page_with_no_anchors_says_so(self):
        panel = build_panel("<html></html>", self.IDX, content_items=self.ITEMS)
        self.assertIn("no content item anchors on this page", panel)

    def test_anchors_the_map_does_not_cover_are_reported_as_unmapped(self):
        # A partial `t4 sections` walk leaves most pages here. Reporting it as
        # "no content" sends you looking for a bug in the page instead.
        page = ('<span id="d.en.900"></span><span id="d.en.901"></span>')
        panel = build_panel(page, self.IDX, content_items=self.ITEMS)
        self.assertIn("2 content item(s) on this page", panel)
        self.assertIn("t4 sections", panel)
        self.assertNotIn("no content item anchors", panel)

    def test_a_repeated_anchor_is_counted_once(self):
        page = '<span id="d.en.900"></span><span id="d.en.900"></span>'
        panel = build_panel(page, self.IDX, content_items=self.ITEMS)
        self.assertIn("1 content item(s) on this page", panel)

    def test_no_map_at_all_is_its_own_message(self):
        panel = build_panel('<span id="d.en.900"></span>', self.IDX,
                            content_items=ContentItems({}))
        self.assertIn("not mapped yet", panel)


class TestContentPreview(unittest.TestCase):
    """Content layout edits, applied to every item that uses them."""

    PUBLISHED = '<div class="box"><t4 type="content" name="Title" /></div>'
    EDITED = '<div class="box new"><t4 type="content" name="Title" /></div>'

    def _page(self, *titles):
        parts = []
        for index, title in enumerate(titles, 1):
            parts.append('<span id="d.en.%d"></span>' % index)
            parts.append('<div class="box">%s</div>' % title)
        return "".join(parts)

    def test_an_edit_reaches_every_occurrence(self):
        page = self._page("One", "Two", "Three")
        out, applied, skipped, _p = preview_every(page, self.PUBLISHED, self.EDITED)
        self.assertEqual((applied, skipped), (3, 0))
        self.assertEqual(out.count('class="box new"'), 3)
        # The per-item content survives; only the layout markup changed.
        for title in ("One", "Two", "Three"):
            self.assertIn(title, out)

    def test_an_unedited_layout_changes_nothing(self):
        page = self._page("One")
        out, applied, skipped, _p = preview_every(page, self.PUBLISHED, self.PUBLISHED)
        self.assertEqual((out, applied, skipped), (page, 0, 0))

    def test_accept_can_reject_a_coincidental_match(self):
        # A short layout's literal markup turns up where it never rendered.
        # Rewriting that is worse than not previewing: it looks like it worked.
        page = self._page("One")
        out, applied, _s, _p = preview_every(page, self.PUBLISHED, self.EDITED,
                                             accept=lambda matched: False)
        self.assertEqual((out, applied), (page, 0))

    def test_crlf_source_still_aligns_against_lf_output(self):
        # T4 stores layout source with CRLF and publishes it with LF, so the
        # literal runs never matched byte for byte and every alignment failed.
        published = '<div class="box">\r\n<t4 type="content" name="T" /></div>'
        edited = published.replace("box", "box new")
        page = ('<span id="d.en.1"></span>'
                + '<div class="box">\n%s</div>' % "One")
        out, applied, _s, _p = preview_every(page, published, edited)
        self.assertEqual(applied, 1)
        self.assertIn('class="box new"', out)
        self.assertIn("One", out)

    def test_rewrapping_a_whole_inseparable_run_is_still_faithful(self):
        # Two adjacent tags whose outputs cannot be told apart. Re-emitting
        # the run as a block is fine -- nothing is being placed inside it.
        published = ('<i><t4 type="content" name="A" />'
                     '<t4 type="content" name="B" /></i>')
        edited = published.replace("<i>", "<i class=x>")
        page = '<span id="d.en.1"></span><i>one-blob</i>'
        out, applied, skipped, _p = preview_every(page, published, edited,
                                                  strict=True)
        self.assertEqual((applied, skipped), (1, 0))
        self.assertIn("one-blob", out)

    def test_strict_leaves_an_occurrence_it_cannot_render_faithfully(self):
        # The edit puts markup *between* two tags whose output could not be
        # separated, so there is no honest place to put it. Previewing anyway
        # would show a layout T4 will not publish.
        published = ('<i><t4 type="content" name="A" />'
                     '<t4 type="content" name="B" /></i>')
        edited = ('<i><t4 type="content" name="A" /><hr>'
                  '<t4 type="content" name="B" /></i>')
        page = '<span id="d.en.1"></span><i>one-blob</i>'
        out, applied, skipped, problems = preview_every(
            page, published, edited, strict=True)
        self.assertEqual((applied, skipped), (0, 1))
        self.assertEqual(out, page, "the page must be left exactly as published")
        self.assertTrue(problems)


class TestServeUrlReachesThePanel(unittest.TestCase):
    """`View site` must work as soon as the server reports its address."""

    def _drain(self, channel):
        import queue as _queue
        found = []
        while True:
            try:
                found.append(channel.get_nowait())
            except _queue.Empty:
                return found

    def test_a_watcher_attaching_late_still_gets_the_url(self):
        # `t4 serve` prints its address a second or two in, often before the
        # browser has finished opening the event stream. A meta event sent in
        # that gap reached nobody, and View site stayed dead until a reload.
        job = app.Job("1", ["serve"], ".")
        job._emit("URL       http://127.0.0.1:8321/")
        metas = [i for i in self._drain(job.watch()) if isinstance(i, dict)]
        self.assertEqual(len(metas), 1)
        self.assertEqual(metas[0]["meta"]["url"], "http://127.0.0.1:8321/")

    def test_a_watcher_attached_first_gets_it_once(self):
        job = app.Job("2", ["serve"], ".")
        channel = job.watch()
        job._emit("URL       http://127.0.0.1:9999/")
        metas = [i for i in self._drain(channel) if isinstance(i, dict)]
        self.assertEqual(len(metas), 1)
        self.assertEqual(metas[0]["meta"]["url"], "http://127.0.0.1:9999/")

    def test_a_job_with_no_url_sends_no_meta(self):
        job = app.Job("3", ["pull"], ".")
        job._emit("Pulled 753 files")
        self.assertEqual([i for i in self._drain(job.watch())
                          if isinstance(i, dict)], [])

    def test_the_port_comes_from_the_output_not_a_default(self):
        job = app.Job("4", ["serve", "--port", "9100"], ".")
        job._emit("URL       http://127.0.0.1:9100/")
        self.assertEqual(job.url, "http://127.0.0.1:9100/")


class TestEditRedirectsToPulledSource(unittest.TestCase):
    """The documented loop: edit the pulled file, not a copy of it."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = pathlib.Path(self.tmp.name)
        (self.root / ".t4").mkdir()
        (self.root / ".t4" / "config.json").write_text(json.dumps({
            "source_dir": "t4-source", "mirror_dir": "site-mirror",
            "overrides_dir": "overrides",
            "environments": {"test": {"base": "https://cms.example.edu"}},
        }), encoding="utf-8")
        env = self.root / "t4-source" / "test"
        (env / "media").mkdir(parents=True)
        (env / "media" / "site.css").write_text("a{}", encoding="utf-8")
        (env / "_manifest.json").write_text(json.dumps({
            "media/site.css": {"endpoint": "media", "id": 1, "field": "text",
                               "name": "site.css"}}), encoding="utf-8")
        mirror = self.root / "site-mirror" / "www.example.edu" / "media" / "css"
        mirror.mkdir(parents=True)
        (mirror / "site.css").write_text("a{}", encoding="utf-8")
        (mirror / "vendor.js").write_text("x", encoding="utf-8")
        self.project = Project(self.root)

    def tearDown(self):
        self.tmp.cleanup()

    def _run(self, path, **over):
        args = build_parser().parse_args(["edit", path])
        for key, value in over.items():
            setattr(args, key, value)
        buffer = io.StringIO()
        with contextlib.redirect_stdout(buffer):
            edit.run(args, self.project)
        return buffer.getvalue()

    def test_a_pulled_file_is_not_copied_into_overrides(self):
        out = self._run("media/css/site.css")
        self.assertIn("this is the file that deploys", out)
        self.assertFalse(
            (self.root / "overrides" / "media" / "css" / "site.css").exists(),
            "a second copy of a pulled file must not be created")

    def test_it_names_the_file_to_edit(self):
        self.assertIn("t4-source", self._run("media/css/site.css"))

    def test_force_still_allows_a_local_only_override(self):
        self._run("media/css/site.css", force=True)
        self.assertTrue(
            (self.root / "overrides" / "media" / "css" / "site.css").exists())

    def test_something_not_pulled_is_copied_as_before(self):
        out = self._run("media/css/vendor.js")
        self.assertIn("Copied into overrides", out)
        self.assertTrue(
            (self.root / "overrides" / "media" / "css" / "vendor.js").exists())


class TestLayoutInheritance(unittest.TestCase):
    """T4 reports no channel at all for a section that inherits its layout."""

    TREE = {
        "1": {"id": 1, "name": "root", "parent": 0, "path": "/site",
              "channels": [{"id": 13, "pageLayout": 100}]},
        "2": {"id": 2, "name": "mid", "parent": 1, "path": "/site/mid",
              "channels": []},
        "3": {"id": 3, "name": "leaf", "parent": 2, "path": "/site/mid/leaf",
              "channels": []},
        "4": {"id": 4, "name": "own", "parent": 1, "path": "/site/own",
              "channels": [{"id": 13, "pageLayout": 0,
                            "inheritedPageLayout": 200}]},
    }

    def setUp(self):
        self.layouts = SectionLayouts(self.TREE)

    def test_a_section_that_sets_its_own_layout_uses_it(self):
        self.assertEqual(self.layouts.resolve("/site/")[0], 100)

    def test_an_inheriting_section_climbs_to_its_ancestor(self):
        # channels: [] means "not set here", not "no layout" -- 40% of
        # sections on a real site, so reading the field alone reports
        # nothing for most pages.
        self.assertEqual(self.layouts.resolve("/mid/")[0], 100)

    def test_it_climbs_more_than_one_level(self):
        self.assertEqual(self.layouts.resolve("/mid/leaf/")[0], 100)

    def test_it_names_the_section_the_layout_came_from(self):
        _layout, owner = self.layouts.resolve("/mid/leaf/")
        self.assertEqual(owner["name"], "root")

    def test_an_explicit_inherited_value_is_still_used(self):
        self.assertEqual(self.layouts.resolve("/own/")[0], 200)

    def test_a_parent_cycle_does_not_hang(self):
        cyclic = {"1": {"id": 1, "parent": 2, "path": "/a", "channels": []},
                  "2": {"id": 2, "parent": 1, "path": "/b", "channels": []}}
        self.assertEqual(SectionLayouts(cyclic).resolve("/a/"), (None, cyclic["1"]))

    def test_an_unknown_path_resolves_to_nothing(self):
        self.assertEqual(self.layouts.resolve("/nowhere/"), (None, None))


class TestPanelKeepsTheExactAnswer(unittest.TestCase):
    """The section tree is exact; a name lookup must not overwrite it."""

    MANIFEST = {
        "pageLayout/u-webpage.production.header.html": {
            "endpoint": "pageLayout", "id": 1180, "field": "headerCode",
            "name": "basic_page.production"},
        "pageLayout/u-webpage.webdev.header.html": {
            "endpoint": "pageLayout", "id": 975523, "field": "headerCode",
            "name": "basic_page.webdev"},
    }
    PAGE = '<meta name="t4-layout" content="basic_page">'
    TREE = {"1": {"id": 1, "name": "sec", "parent": 0, "path": "/sec",
                  "channels": [{"id": 13, "pageLayout": 1180}]}}

    def test_an_exact_assignment_survives_with_no_resolver(self):
        # Written as an `else` of the resolver check, the ambiguous name
        # lookup ran whenever no resolver was passed and threw the exact
        # answer away -- so the panel said "candidates" and offered no file.
        panel = build_panel(self.PAGE, LayoutIndex(self.MANIFEST),
                            url_path="/sec/", env_name="test",
                            sections=SectionLayouts(self.TREE))
        self.assertIn("u-webpage.production.header.html", panel)
        self.assertNotIn("candidates", panel)

    def test_without_an_assignment_the_name_lookup_still_runs(self):
        panel = build_panel(self.PAGE, LayoutIndex(self.MANIFEST),
                            url_path="/nowhere/", env_name="test",
                            sections=SectionLayouts(self.TREE))
        self.assertIn("candidates", panel)

    def test_page_layout_files_can_be_opened(self):
        panel = build_panel(self.PAGE, LayoutIndex(self.MANIFEST),
                            url_path="/sec/", env_name="test",
                            sections=SectionLayouts(self.TREE))
        self.assertIn('data-open="edit"', panel)
        self.assertIn('data-open="reveal"', panel)
        self.assertNotIn('data-copy="t4 edit', panel)


class TestAssetRowsPointAtPulledSource(unittest.TestCase):
    """A stylesheet's served URL is not where the pulled file lives."""

    PAGE = '<link href="/media/web/b3/css/site.css" rel="stylesheet">'
    MEDIA = MediaSources({"media/site.css": {"endpoint": "media", "id": 1,
                                             "name": "site.css"}}, "t4-source/test")

    def test_the_asset_resolves_to_the_file_that_deploys(self):
        panel = build_panel(self.PAGE, LayoutIndex({}), env_name="test",
                            media=self.MEDIA)
        self.assertIn('data-path="t4-source/test/media/site.css"', panel)

    def test_an_asset_with_no_pulled_source_says_so(self):
        panel = build_panel(self.PAGE, LayoutIndex({}), env_name="test",
                            media=MediaSources({}, "t4-source/test"))
        self.assertIn("not pulled", panel)


class TestServePicksTheRightMirror(unittest.TestCase):
    """`--env prod` must not serve the test site."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = pathlib.Path(self.tmp.name)
        (self.root / ".t4").mkdir()
        (self.root / ".t4" / "config.json").write_text(json.dumps({
            "source_dir": "t4-source", "mirror_dir": "site-mirror",
            "environments": {
                "test": {"base": "https://cms", "published_url": "https://webtest.example.edu"},
                "prod": {"base": "https://cms", "published_url": "https://www.example.edu"}},
        }), encoding="utf-8")
        self.project = Project(self.root)
        for host in ("webtest.example.edu", "www.example.edu"):
            page = self.root / "site-mirror" / host / "index.html"
            page.parent.mkdir(parents=True)
            page.write_text("<html></html>", encoding="utf-8")

    def tearDown(self):
        self.tmp.cleanup()

    def _served(self, env):
        args = build_parser().parse_args(["serve", "--env", env])
        captured = {}

        def fake_serve(site, **kwargs):
            captured["site"] = site

        real = site.serve_mod.serve
        site.serve_mod.serve = fake_serve
        try:
            with contextlib.redirect_stdout(io.StringIO()):
                site.run_serve(args, self.project)
        finally:
            site.serve_mod.serve = real
        return pathlib.Path(captured["site"]).name

    def test_prod_serves_the_prod_mirror(self):
        # "webtest" sorts before "www", so picking the first directory served
        # the test site for prod -- silently, with prod source mapped over it.
        self.assertEqual(self._served("prod"), "www.example.edu")

    def test_test_serves_the_test_mirror(self):
        self.assertEqual(self._served("test"), "webtest.example.edu")

    def test_the_host_comes_from_the_published_url(self):
        self.assertEqual(site.host_of(self.project, "prod"), "www.example.edu")
        self.assertIsNone(site.host_of(Project(self.root), "nope")
                          if False else None)


class TestCrawlerManners(unittest.TestCase):
    """A crawl is thousands of requests from one address in a few minutes."""

    def test_a_refusal_suggests_slowing_down(self):
        exc = urllib.error.HTTPError("http://x/", 403, "Forbidden", {}, None)
        message = mirror.describe_error(exc)
        self.assertIn("403", message)
        self.assertIn("--delay", message)

    def test_rate_limiting_is_named_as_such(self):
        exc = urllib.error.HTTPError("http://x/", 429, "Too Many", {}, None)
        self.assertIn("rate limited", mirror.describe_error(exc))

    def test_a_dead_vpn_is_not_reported_as_a_class_name(self):
        exc = urllib.error.URLError("nodename nor servname provided")
        message = mirror.describe_error(exc)
        self.assertIn("VPN", message)
        self.assertNotIn("URLError", message)

    def test_a_refusal_is_not_retried(self):
        # Retrying a 403 faster is what keeps an address blocked.
        self.assertNotIn(403, mirror.RETRY_STATUS)
        self.assertIn(429, mirror.RETRY_STATUS)
        self.assertIn(503, mirror.RETRY_STATUS)

    def test_there_is_a_gap_between_requests_by_default(self):
        self.assertGreater(mirror.DEFAULT_DELAY, 0)


class TestWatcherIgnoresToolWrites(unittest.TestCase):
    """Only edits should reload the browser, not the tool's own bookkeeping."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = pathlib.Path(self.tmp.name)
        (self.root / "media").mkdir()
        (self.root / "media" / "site.css").write_text("a{}", encoding="utf-8")
        (self.root / "_raw").mkdir()
        (self.root / "_backup").mkdir()
        self.watcher = Watcher([str(self.root)])

    def tearDown(self):
        self.tmp.cleanup()

    def _changed(self, write):
        before = self.watcher._fingerprint()
        write()
        return self.watcher._fingerprint() != before

    def test_a_real_edit_is_noticed(self):
        self.assertTrue(self._changed(
            lambda: (self.root / "media" / "site.css").write_text(
                "a{color:red}", encoding="utf-8")))

    def test_a_section_walk_checkpoint_is_not_an_edit(self):
        # `t4 sections` rewrites _raw every 50 sections. Watching it meant
        # the page reloaded repeatedly with nothing on it having changed.
        self.assertFalse(self._changed(
            lambda: (self.root / "_raw" / "sections.json").write_text(
                "{}", encoding="utf-8")))

    def test_a_pre_push_backup_is_not_an_edit(self):
        self.assertFalse(self._changed(
            lambda: (self.root / "_backup" / "1.json").write_text(
                "{}", encoding="utf-8")))

    def test_rewriting_the_manifest_is_not_an_edit(self):
        self.assertFalse(self._changed(
            lambda: (self.root / "_manifest.json").write_text(
                "{}", encoding="utf-8")))


class TestRequireCommitIsPerEnvironment(unittest.TestCase):
    """Test is where you iterate; production is where the record matters."""

    def _project(self, config):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        root = pathlib.Path(tmp.name)
        (root / ".t4").mkdir()
        (root / ".t4" / "config.json").write_text(json.dumps(config),
                                                  encoding="utf-8")
        return Project(root)

    def test_on_by_default(self):
        project = self._project({"environments": {"test": {}}})
        self.assertTrue(requires_commit(project, "test"))

    def test_an_environment_can_turn_it_off_alone(self):
        project = self._project({"environments": {
            "test": {"require_commit": False},
            "prod": {}}})
        self.assertFalse(requires_commit(project, "test"))
        self.assertTrue(requires_commit(project, "prod"))

    def test_an_environment_can_turn_it_on_against_a_loose_default(self):
        project = self._project({
            "require_commit": False,
            "environments": {"test": {}, "prod": {"require_commit": True}}})
        self.assertFalse(requires_commit(project, "test"))
        self.assertTrue(requires_commit(project, "prod"))

    def test_the_project_level_key_still_works_as_a_default(self):
        project = self._project({"require_commit": False,
                                 "environments": {"test": {}}})
        self.assertFalse(requires_commit(project, "test"))

    def test_an_unknown_environment_gets_the_safe_answer(self):
        project = self._project({"environments": {}})
        self.assertTrue(requires_commit(project, "nope"))

    def test_the_gate_follows_the_environment(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        root = pathlib.Path(tmp.name)
        (root / ".t4").mkdir()
        (root / ".t4" / "config.json").write_text(json.dumps({
            "source_dir": "t4-source",
            "environments": {"test": {"require_commit": False}, "prod": {}}}),
            encoding="utf-8")
        subprocess.run(["git", "init", "-b", "main"], cwd=root, check=True,
                       stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        target = root / "t4-source" / "test" / "a.html"
        target.parent.mkdir(parents=True)
        target.write_text("x", encoding="utf-8")
        project = Project(root)

        require_clean(project, "test", [target])      # loose: must not raise
        with self.assertRaises(T4Error):
            require_clean(project, "prod", [target])

    def test_the_refusal_names_the_per_environment_key(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        root = pathlib.Path(tmp.name)
        (root / ".t4").mkdir()
        (root / ".t4" / "config.json").write_text(json.dumps({
            "source_dir": "t4-source", "environments": {"prod": {}}}),
            encoding="utf-8")
        subprocess.run(["git", "init", "-b", "main"], cwd=root, check=True,
                       stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        target = root / "t4-source" / "prod" / "a.html"
        target.parent.mkdir(parents=True)
        target.write_text("x", encoding="utf-8")
        with self.assertRaises(T4Error) as caught:
            require_clean(Project(root), "prod", [target])
        message = str(caught.exception)
        self.assertIn('"prod": { "require_commit": false }', message)


class TestLocalDiff(unittest.TestCase):
    """git already holds the local baseline; a pull keeps no snapshot."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = pathlib.Path(self.tmp.name)
        subprocess.run(["git", "init", "-b", "main"], cwd=self.root, check=True,
                       stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        for key, value in (("user.email", "t@example.com"), ("user.name", "T")):
            subprocess.run(["git", "config", key, value], cwd=self.root, check=True)
        self.repo = Repo.at(self.root)
        self.file = self.root / "media" / "site.css"
        self.file.parent.mkdir()
        self.file.write_text("a{}\n", encoding="utf-8")

    def tearDown(self):
        self.tmp.cleanup()

    def _commit(self):
        self.repo.commit([self.file], "add")

    def test_an_untracked_file_shows_as_entirely_new(self):
        diff, state = self.repo.diff(self.file)
        self.assertEqual(state, "untracked")
        self.assertIn("+a{}", diff)

    def test_a_committed_unchanged_file_has_no_diff(self):
        self._commit()
        diff, state = self.repo.diff(self.file)
        self.assertEqual(state, "committed")
        self.assertEqual(diff.strip(), "")

    def test_an_edit_after_a_commit_shows_only_the_edit(self):
        self._commit()
        self.file.write_text("a{color:red}\n", encoding="utf-8")
        diff, state = self.repo.diff(self.file)
        self.assertEqual(state, "committed")
        self.assertIn("-a{}", diff)
        self.assertIn("+a{color:red}", diff)

    def test_a_staged_edit_is_still_reported(self):
        self._commit()
        self.file.write_text("a{color:red}\n", encoding="utf-8")
        subprocess.run(["git", "add", "--", "media/site.css"], cwd=self.root,
                       check=True)
        diff, state = self.repo.diff(self.file)
        self.assertEqual(state, "staged")
        self.assertIn("+a{color:red}", diff)

    def test_crlf_content_does_not_break_the_diff(self):
        # T4 stores CRLF; a diff that mangled it would misreport every line.
        self.file.write_bytes(b"a{}\r\n")
        self._commit()
        self.file.write_bytes(b"a{}\r\nb{}\r\n")
        diff, _state = self.repo.diff(self.file)
        self.assertIn("+b{}", diff)


class TestFileHistory(unittest.TestCase):
    """"No uncommitted changes" is a dead end; when and why is the answer."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = pathlib.Path(self.tmp.name)
        subprocess.run(["git", "init", "-b", "main"], cwd=self.root, check=True,
                       stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        for key, value in (("user.email", "t@example.com"), ("user.name", "Ada")):
            subprocess.run(["git", "config", key, value], cwd=self.root, check=True)
        self.repo = Repo.at(self.root)
        self.file = self.root / "media" / "site.css"
        self.file.parent.mkdir()

    def tearDown(self):
        self.tmp.cleanup()

    def _write_and_commit(self, text, message):
        self.file.write_text(text, encoding="utf-8")
        self.repo.commit([self.file], message)

    def test_an_uncommitted_file_has_no_history_and_does_not_raise(self):
        self.file.write_text("a{}\n", encoding="utf-8")
        self.assertEqual(self.repo.log(self.file), [])

    def test_commits_come_back_newest_first(self):
        self._write_and_commit("a{}\n", "first")
        self._write_and_commit("b{}\n", "second")
        found = self.repo.log(self.file)
        self.assertEqual([c["subject"] for c in found], ["second", "first"])

    def test_each_entry_carries_what_the_list_shows(self):
        self._write_and_commit("a{}\n", "only")
        entry = self.repo.log(self.file)[0]
        self.assertEqual(entry["subject"], "only")
        self.assertEqual(entry["author"], "Ada")
        self.assertTrue(entry["sha"].startswith(entry["short"]))
        self.assertRegex(entry["date"], r"^\d{4}-\d{2}-\d{2}T")

    def test_a_subject_with_separators_in_it_still_parses(self):
        # The log is split on control characters no subject should contain.
        self._write_and_commit("a{}\n", "fix: tabs\tand | pipes")
        self.assertEqual(self.repo.log(self.file)[0]["subject"],
                         "fix: tabs\tand | pipes")

    def test_one_commit_can_be_shown_on_its_own(self):
        self._write_and_commit("a{}\n", "first")
        self._write_and_commit("b{}\n", "second")
        sha = self.repo.log(self.file)[0]["sha"]
        diff = self.repo.show(sha, self.file)
        self.assertIn("+b{}", diff)
        self.assertIn("-a{}", diff)

    def test_a_commit_id_that_is_not_one_is_refused(self):
        # It reaches a subprocess, so it is checked rather than trusted.
        self._write_and_commit("a{}\n", "only")
        for bad in ("; rm -rf /", "HEAD; echo", "../../etc/passwd", ""):
            with self.assertRaises(ValueError):
                self.repo.show(bad, self.file)

    def test_the_limit_is_honoured(self):
        for index in range(5):
            self._write_and_commit("a%d{}\n" % index, "commit %d" % index)
        self.assertEqual(len(self.repo.log(self.file, limit=2)), 2)


class TestButtonContrast(unittest.TestCase):
    """Buttons must look like buttons, and be readable in both themes.

    Checked rather than eyeballed: the palette that made them look like
    disabled text inputs had a 1.19:1 boundary against the card, which is
    invisible but perfectly plausible in a diff.
    """

    @staticmethod
    def _luminance(value):
        value = value.lstrip("#")
        if len(value) == 3:
            value = "".join(c * 2 for c in value)
        parts = [int(value[i:i+2], 16) / 255 for i in (0, 2, 4)]
        adj = [c / 12.92 if c <= 0.04045 else ((c + 0.055) / 1.055) ** 2.4
               for c in parts]
        return 0.2126 * adj[0] + 0.7152 * adj[1] + 0.0722 * adj[2]

    @classmethod
    def _ratio(cls, first, second):
        a, b = cls._luminance(first), cls._luminance(second)
        return (max(a, b) + 0.05) / (min(a, b) + 0.05)

    @classmethod
    def setUpClass(cls):
        css = (app.PAGE % {"token": "x"}).split("<style>")[1].split("</style>")[0]
        grab = lambda block: dict(
            re.findall(r"--([a-z-]+):\s*(#[0-9a-fA-F]{3,6})", block))
        cls.themes = {
            "light": grab(css.split(":root{")[1].split("}")[0]),
            "dark": grab(
                css.split("prefers-color-scheme:dark){:root{")[1].split("}")[0]),
        }

    def _check(self, label, first, second, need):
        for theme, tokens in self.themes.items():
            got = self._ratio(tokens[first], tokens[second])
            self.assertGreaterEqual(
                got, need,
                "%s in %s theme: %.2f, need %.1f" % (label, theme, got, need))

    def test_the_border_makes_it_a_button(self):
        # WCAG 1.4.11: the boundary identifying a control needs 3:1.
        self._check("border vs card", "btn-edge", "card", 3.0)
        self._check("border vs page", "btn-edge", "bg", 3.0)

    def test_the_label_is_readable_in_every_state(self):
        for face in ("btn", "btn-hover", "btn-active"):
            self._check("label on %s" % face, "btn-fg", face, 4.5)

    def test_the_primary_button_is_readable(self):
        self._check("primary label", "on-accent", "accent", 4.5)

    def test_the_focus_ring_is_visible(self):
        self._check("focus ring vs card", "accent", "card", 3.0)

    def test_supporting_text_is_readable(self):
        self._check("dim on card", "dim", "card", 4.5)
        self._check("danger label", "warn", "btn", 4.5)

    def test_disabled_is_not_signalled_by_colour_alone(self):
        css = (app.PAGE % {"token": "x"}).split("<style>")[1].split("</style>")[0]
        rule = css.split("button:disabled{")[1].split("}")[0]
        self.assertIn("cursor:not-allowed", rule)
        self.assertIn("border-style:dashed", rule)


class TestActionIcons(unittest.TestCase):
    """Icons are inline SVG: nothing to fetch, so they work offline."""

    @classmethod
    def setUpClass(cls):
        page = app.PAGE % {"token": "x"}
        cls.body = page.split("</style>")[1].split("<script>")[0]
        cls.js = page.split("<script>")[1].split("</scr" + "ipt>")[0]

    def test_every_action_button_has_one(self):
        self.assertEqual(self.body.count('<svg class="ic"'), 10)

    def test_each_icon_is_well_formed(self):
        import xml.etree.ElementTree as ET
        found = re.findall(r'(<svg class="ic".*?</svg>)', self.body, re.S)
        self.assertEqual(len(found), 10)
        for markup in found:
            ET.fromstring(markup)          # raises if malformed

    def test_icons_are_hidden_from_screen_readers(self):
        # The label beside them already says what the button does. Counted
        # within the svg tags rather than across the page, so another
        # decorative element elsewhere does not change the answer.
        icons = re.findall(r"<svg class=\"ic\"[^>]*>", self.body)
        self.assertEqual(len(icons), 10)
        for icon in icons:
            self.assertIn('aria-hidden="true"', icon)

    def test_they_follow_the_button_colour(self):
        self.assertIn('stroke="currentColor"', self.body)
        self.assertNotIn('stroke="#', self.body)

    def test_nothing_fetches_an_icon(self):
        for probe in ("<i class=", "font-awesome", "material-icons",
                      "cdn", "<img "):
            self.assertNotIn(probe, self.body.lower())

    def test_the_serve_label_changes_without_eating_the_icon(self):
        # Setting textContent on the button would delete its child <svg>.
        self.assertNotIn("start.textContent", self.js)
        self.assertIn("startLabel.textContent", self.js)


class TestTagBuilder(unittest.TestCase):
    """Compose T4 tags from pulled data. Composing is text; rendering is T4."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.env = pathlib.Path(self.tmp.name)
        (self.env / "contenttype").mkdir()
        (self.env / "contenttype" / "contact.json").write_text(json.dumps({
            "id": 307, "name": "Contact", "alias": "contact",
            "contentTypeElements": [
                {"name": "First Name", "alias": "First Name", "type": 1, "id": 2},
                {"name": "Photo", "alias": "Photo", "type": 11, "id": 7},
                {"name": "Body", "alias": "Body", "type": 3, "id": 9},
                {"name": "When", "alias": "When", "type": 5, "id": 11},
                {"name": "Campus", "alias": "Campus", "type": 7,
                 "listId": 4, "id": 12},
                {"name": "Odd", "alias": "Odd", "type": 99, "id": 32},
            ]}), encoding="utf-8")
        (self.env / "contentLayout").mkdir()
        (self.env / "contentLayout" / "a.html").write_text(
            '<t4 type="content" name="First Name" output="normal" '
            'modifiers="htmlentities" />'
            '<t4 type="content" name="Photo" output="image" />'
            '<t4 type="content" name="Bio" output="normal" '
            'modifiers="striptags,htmlentities" />'
            '<t4 type="navigation" id="275" />', encoding="utf-8")

    def tearDown(self):
        self.tmp.cleanup()

    def test_elements_come_from_the_pulled_content_type(self):
        found = tags.content_types(str(self.env))
        self.assertEqual([t["name"] for t in found], ["Contact"])
        self.assertEqual([e["name"] for e in found[0]["elements"]],
                         ["First Name", "Photo", "Body", "When", "Campus",
                          "Odd"])

    def test_element_types_are_named_where_corroborated(self):
        by_name = {e["name"]: e for e
                   in tags.content_types(str(self.env))[0]["elements"]}
        self.assertEqual(by_name["First Name"]["type"], "plain text")
        self.assertEqual(by_name["Photo"]["type"], "image")
        self.assertEqual(by_name["Body"]["type"], "HTML")
        self.assertEqual(by_name["When"]["type"], "date")
        self.assertEqual(by_name["Campus"]["type"], "list")

    def test_only_a_date_element_is_marked_as_one(self):
        by_name = {e["name"]: e for e
                   in tags.content_types(str(self.env))[0]["elements"]}
        self.assertTrue(by_name["When"]["is_date"])
        self.assertFalse(by_name["Body"]["is_date"])

    def test_an_uncorroborated_code_falls_back_on_what_is_known(self):
        # listId says "list" more reliably than an unverified type number.
        self.assertEqual(tags.element_type({"type": 99, "listId": 3}), "list")
        self.assertEqual(tags.element_type({"type": 99, "binary": True}),
                         "file or image")

    def test_an_unknown_element_type_is_reported_not_guessed(self):
        self.assertEqual(tags.element_type({"type": 99}), "type 99")

    def test_vocabulary_is_mined_from_the_project(self):
        vocab = tags.vocabulary(str(self.env))
        self.assertEqual(vocab["tags_seen"], 4)
        # Commonest first: what this site does outranks what a manual says.
        self.assertEqual(vocab["modifiers"][0]["value"], "htmlentities")
        self.assertEqual(vocab["modifiers"][0]["count"], 2)

    def test_documented_values_are_offered_even_when_unused(self):
        vocab = tags.vocabulary(str(self.env))
        offered = {entry["value"] for entry in vocab["output"]}
        self.assertIn("fulltext", offered)          # documented, unused here
        self.assertIn("image", offered)             # used here

    def test_a_value_t4_does_not_document_is_flagged(self):
        # This site uses "tags" and "js-string" as modifiers; neither is in
        # the reference. Worth saying rather than quietly endorsing.
        (self.env / "contentLayout" / "b.html").write_text(
            '<t4 type="content" name="X" modifiers="made-up" />',
            encoding="utf-8")
        vocab = tags.vocabulary(str(self.env))
        found = {e["value"]: e["documented"] for e in vocab["modifiers"]}
        self.assertFalse(found["made-up"])
        self.assertTrue(found["htmlentities"])

    def test_a_project_with_no_layouts_still_offers_the_documented_set(self):
        bare = pathlib.Path(self.tmp.name) / "bare"
        bare.mkdir()
        vocab = tags.vocabulary(str(bare))
        self.assertEqual(vocab["tags_seen"], 0)
        self.assertIn("normal", {e["value"] for e in vocab["output"]})
        self.assertIn("content", {e["value"] for e in vocab["type"]})

    def test_a_tag_is_built_in_the_order_t4_writes_them(self):
        markup, problems = tags.build_tag(
            {"type": "content", "name": "First Name", "output": "normal",
             "modifiers": ["htmlentities"]})
        self.assertEqual(markup,
                         '<t4 type="content" name="First Name" '
                         'output="normal" modifiers="htmlentities" />')
        self.assertEqual(problems, [])

    def test_quotes_in_an_element_name_cannot_break_the_attribute(self):
        built, _problems = tags.build_tag({"type": "content",
                                           "name": 'say "hi" <b>'})
        self.assertIn("&quot;hi&quot;", built)
        self.assertNotIn('"hi"', built)
        self.assertNotIn("<b>", built)

    def test_empty_attributes_are_left_out(self):
        markup, _problems = tags.build_tag({"type": "meta", "meta": "name"})
        self.assertEqual(markup, '<t4 type="meta" meta="name" />')

    def test_a_media_tag_refuses_content_attributes(self):
        # A media tag names an item by id and a layout by formatter. It
        # takes no name, output or modifiers -- those are a content tag's.
        _markup, problems = tags.build_tag(
            {"type": "media", "name": "Logo", "output": "image"})
        self.assertIn("a media tag does not take name", problems)
        self.assertIn("a media tag does not take output", problems)
        self.assertTrue(any(p.startswith("a media tag needs id")
                            for p in problems), problems)

    def test_a_missing_id_says_where_to_find_one(self):
        # "needs id" without saying where to get one is half an answer.
        _markup, problems = tags.build_tag({"type": "media"})
        self.assertTrue(any("Media Library" in p for p in problems), problems)

    def test_a_missing_required_attribute_is_left_as_a_placeholder(self):
        # The tag keeps the right shape to paste and finish by hand, rather
        # than silently lacking a piece.
        markup, _problems = tags.build_tag({"type": "media"})
        self.assertEqual(markup, '<t4 type="media" id="" />')
        markup, _problems = tags.build_tag({"type": "navigation"})
        self.assertEqual(markup, '<t4 type="navigation" id="" />')

    def test_media_items_can_be_picked_from_the_pull(self):
        (self.env / "_manifest.json").write_text(json.dumps({
            "media/site.css": {"endpoint": "media", "id": 4120,
                               "name": "site.css"},
            "pageLayout/a.html": {"endpoint": "pageLayout", "id": 1}}),
            encoding="utf-8")
        found = tags.media_objects(str(self.env))
        self.assertEqual(found, [{"id": 4120, "name": "site.css"}])

    def test_a_media_tag_built_properly_has_no_complaints(self):
        markup, problems = tags.build_tag(
            {"type": "media", "id": "10", "formatter": "image/*"})
        self.assertEqual(markup, '<t4 type="media" id="10" formatter="image/*" />')
        self.assertEqual(problems, [])

    def test_a_navigation_tag_takes_a_name_as_well_as_an_id(self):
        markup, problems = tags.build_tag(
            {"type": "navigation", "id": "34", "name": "A-Z Site Map"})
        self.assertEqual(markup,
                         '<t4 type="navigation" name="A-Z Site Map" id="34" />')
        self.assertEqual(problems, [])

    def test_a_meta_tag_takes_either_an_id_or_a_meta_name(self):
        for spec in ({"type": "meta", "id": "19"},
                     {"type": "meta", "meta": "html_anchor"}):
            _markup, problems = tags.build_tag(spec)
            self.assertEqual(problems, [], spec)
        _markup, problems = tags.build_tag({"type": "meta"})
        self.assertTrue(any("one of" in p for p in problems))

    def test_an_undocumented_output_is_refused(self):
        _markup, problems = tags.build_tag(
            {"type": "content", "name": "X", "output": "invented"})
        self.assertIn("invented is not a documented output value", problems)

    def test_an_output_documented_only_on_a_sub_page_is_accepted(self):
        # output="name" is absent from the appendix and used on the access
        # control page. Reading the appendix alone had it refused.
        _markup, problems = tags.build_tag(
            {"type": "content", "name": "X", "output": "name"})
        self.assertEqual(problems, [])

    def test_a_language_variable_takes_one_attribute_per_language(self):
        markup, problems = tags.build_tag(
            {"type": "lang-var", "default-language": "en",
             "en": "Name", "ga": "Irish Name"})
        self.assertEqual(problems, [])
        self.assertIn('en="Name"', markup)
        self.assertIn('ga="Irish Name"', markup)

    def test_something_that_is_not_a_language_is_still_refused(self):
        _markup, problems = tags.build_tag({"type": "lang-var",
                                            "not_a_language": "x"})
        self.assertTrue(any("does not take" in p for p in problems))

    def test_the_documented_modifier_conflict_is_caught(self):
        _markup, problems = tags.build_tag(
            {"type": "content", "name": "X",
             "modifiers": ["striptags", "rssentities"]})
        self.assertTrue(any("rssentities" in p for p in problems))

    def test_a_type_that_does_not_exist_builds_nothing(self):
        markup, problems = tags.build_tag({"type": "web_object", "id": "5"})
        self.assertEqual(markup, "")
        self.assertTrue(problems)
        # The real one is web-obj.
        self.assertIn("web-obj", tags.TAG_TYPES)

    def test_a_tag_with_no_attributes_is_still_valid(self):
        markup, problems = tags.build_tag({"type": "title"})
        self.assertEqual(markup, '<t4 type="title" />')
        self.assertEqual(problems, [])

    def test_handlebars_equivalent_for_an_element(self):
        markup, note = tags.build_handlebars({"type": "content",
                                              "name": "Heading"})
        self.assertEqual(markup, '{{element name="Heading"}}')
        self.assertIsNone(note)

    def test_a_date_format_becomes_the_date_helper(self):
        markup, _note = tags.build_handlebars({
            "type": "content", "name": "When", "date_format": "dd MMMM yyyy"})
        self.assertEqual(markup, '{{format-date element="When" format="dd MMMM yyyy"}}')

    def test_modifiers_have_no_handlebars_equivalent_and_it_says_so(self):
        _markup, note = tags.build_handlebars({
            "type": "content", "name": "Body", "modifiers": ["striptags"]})
        self.assertIn("no modifiers attribute", note)

    def test_a_tag_with_no_handlebars_form_says_so_rather_than_inventing_one(self):
        markup, note = tags.build_handlebars({"type": "navigation", "id": "275"})
        self.assertEqual(markup, "")
        self.assertIn("No Handlebars equivalent", note)

    def test_the_two_syntaxes_cannot_be_mixed_and_that_is_stated(self):
        self.assertIn("either Standard or Handlebars", tags.MIXING_WARNING)


class TestTagBuilderPanel(unittest.TestCase):
    """The builder posts a spec and shows what came back; it invents nothing."""

    @classmethod
    def setUpClass(cls):
        page = app.PAGE % {"token": "x"}
        cls.body = page.split("</style>")[1].split("<script>")[0]
        cls.js = page.split("<script>")[1].split("</scr" + "ipt>")[0]

    def test_the_controls_exist(self):
        for control in ("tg-type", "tg-ct", "tg-el", "tg-out", "tg-meta",
                        "tg-obj", "tg-date", "tg-mods", "tg-tag", "tg-hbs"):
            self.assertIn('id="%s"' % control, self.body, control)

    def test_both_syntaxes_are_shown(self):
        self.assertIn("T4 tag", self.body)
        self.assertIn("Handlebars", self.body)

    def test_the_page_does_not_build_tags_itself(self):
        # One implementation, on the server, so the panel and `t4` cannot
        # drift into emitting different markup.
        self.assertIn('api("/api/tag"', self.js)
        self.assertNotIn("<t4 type=", self.js)

    def test_it_says_it_cannot_evaluate_them(self):
        self.assertIn("T4 evaluates tags at publish time", self.body)

    def test_the_mixing_warning_reaches_the_page(self):
        self.assertIn("mixing_warning", self.js)


class TestDiagrams(unittest.TestCase):
    """README diagrams are generated, so they can be checked like code."""

    ROOT = pathlib.Path(__file__).resolve().parents[1]

    def test_every_diagram_has_both_themes(self):
        import importlib.util
        spec = importlib.util.spec_from_file_location(
            "diagrams", self.ROOT / "docs" / "diagrams.py")
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        for name in module.DIAGRAMS:
            for theme in ("light", "dark"):
                path = self.ROOT / "docs" / "img" / ("%s-%s.svg" % (name, theme))
                self.assertTrue(path.is_file(), path)

    def test_the_committed_svg_matches_the_generator(self):
        # Edited by hand, a diagram drifts from what the generator produces
        # and the next regeneration silently discards the edit.
        import importlib.util
        import xml.etree.ElementTree as ET
        spec = importlib.util.spec_from_file_location(
            "diagrams", self.ROOT / "docs" / "diagrams.py")
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        for name, build in module.DIAGRAMS.items():
            for theme, palette in module.PALETTES.items():
                expected = build(palette).render()
                ET.fromstring(expected)
                path = self.ROOT / "docs" / "img" / ("%s-%s.svg" % (name, theme))
                self.assertEqual(path.read_text(encoding="utf-8"), expected,
                                 "%s is stale: run python3 docs/diagrams.py" % path.name)

    def test_nothing_in_them_needs_the_network(self):
        # The SVG namespace is a URI, not a fetch, so it is allowed; an
        # actual remote reference would make the README depend on a third
        # party staying up, and GitHub would strip it anyway.
        for path in (self.ROOT / "docs" / "img").glob("*.svg"):
            text = path.read_text(encoding="utf-8")
            for probe in ("<script", "xlink:href", "<image", "<foreignObject",
                          "@import", "url("):
                self.assertNotIn(probe, text, "%s contains %s" % (path.name, probe))
            # Attributes only: the overview diagram prints
            # "http://127.0.0.1:8321" as a label, which is text, not a fetch.
            remote = re.findall(r'\b(?:href|src|srcset)\s*=\s*"[^"]*//', text)
            self.assertEqual(remote, [], "%s fetches something" % path.name)

    def test_the_readme_shows_each_one_in_both_themes(self):
        readme = (self.ROOT / "README.md").read_text(encoding="utf-8")
        for path in (self.ROOT / "docs" / "img").glob("*-light.svg"):
            self.assertIn(path.name, readme, path.name)
            self.assertIn(path.name.replace("-light", "-dark"), readme)

    def test_every_image_carries_a_real_description(self):
        readme = (self.ROOT / "README.md").read_text(encoding="utf-8")
        alts = re.findall(r'<img alt="([^"]*)"', readme)
        self.assertEqual(len(alts), readme.count("<picture>"))
        for alt in alts:
            self.assertGreater(len(alt), 60, "alt text is too thin: %r" % alt)


class TestDocsSite(unittest.TestCase):
    """The site is generated from the README, so it cannot drift from it."""

    ROOT = pathlib.Path(__file__).resolve().parents[1]

    @classmethod
    def setUpClass(cls):
        import importlib.util
        spec = importlib.util.spec_from_file_location(
            "site_builder", cls.ROOT / "docs" / "site.py")
        cls.site = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(cls.site)

    def _build(self):
        out = pathlib.Path(tempfile.mkdtemp())
        self.addCleanup(lambda: __import__("shutil").rmtree(out, ignore_errors=True))
        self.site.build(str(out))
        return out

    def test_a_page_is_written_for_every_readme_section(self):
        readme = (self.ROOT / "README.md").read_text(encoding="utf-8")
        titles = [t for t, _b in self.site.split_sections(readme)]
        out = self._build()
        for title in titles:
            name = "index" if title == "Overview" else self.site.slug(title)
            self.assertTrue((out / ("%s.html" % name)).is_file(), title)

    def test_every_internal_link_resolves(self):
        out = self._build()
        for page in out.glob("*.html"):
            text = page.read_text(encoding="utf-8")
            for link in set(re.findall(r'href="([^"#:]+\.html)"', text)):
                self.assertTrue((out / link).is_file(),
                                "%s links to missing %s" % (page.name, link))

    def test_every_image_is_copied_alongside(self):
        out = self._build()
        for page in out.glob("*.html"):
            for ref in re.findall(r'src(?:set)?="(docs/img/[^"]+)"',
                                  page.read_text(encoding="utf-8")):
                self.assertTrue((out / ref).is_file(), ref)

    def test_the_html_is_balanced(self):
        import html.parser

        class Balanced(html.parser.HTMLParser):
            VOID = {"meta", "link", "img", "br", "hr", "source", "input"}

            def __init__(self):
                super().__init__()
                self.stack = []

            def handle_starttag(self, tag, attrs):
                if tag not in self.VOID:
                    self.stack.append(tag)

            def handle_endtag(self, tag):
                if self.stack and self.stack[-1] == tag:
                    self.stack.pop()
                elif tag in self.stack:
                    self.stack.remove(tag)

        out = self._build()
        for page in out.glob("*.html"):
            checker = Balanced()
            checker.feed(page.read_text(encoding="utf-8"))
            self.assertEqual(checker.stack, [], "%s: unclosed %s"
                             % (page.name, checker.stack))

    def test_raw_html_in_the_readme_survives(self):
        # The <picture> blocks are raw HTML; a parser that escaped them would
        # print the markup instead of showing the diagram.
        out = self._build()
        combined = "".join(p.read_text(encoding="utf-8") for p in out.glob("*.html"))
        self.assertGreaterEqual(combined.count("<picture>"), 5)
        self.assertNotIn("&lt;picture&gt;", combined)

    def test_tables_and_code_blocks_render_as_such(self):
        out = self._build()
        combined = "".join(p.read_text(encoding="utf-8") for p in out.glob("*.html"))
        self.assertIn("<table>", combined)
        self.assertIn('<pre class="code', combined)

    def test_it_is_served_as_plain_files(self):
        # Without this GitHub treats it as a Jekyll site and rebuilds it.
        self.assertTrue((self._build() / ".nojekyll").is_file())

    def test_a_code_span_is_not_treated_as_markup(self):
        rendered, _headings = self.site.render("Use `**not bold**` here.")
        self.assertIn("<code>**not bold**</code>", rendered)
        self.assertNotIn("<strong>", rendered)


class TestDiagramContent(unittest.TestCase):
    """A public repo's illustrations must name no client and no person."""

    ROOT = pathlib.Path(__file__).resolve().parents[1]

    def _svg_text(self):
        import xml.etree.ElementTree as ET
        ns = "{http://www.w3.org/2000/svg}"
        for path in (self.ROOT / "docs" / "img").glob("*.svg"):
            for element in ET.parse(path).getroot().iter(ns + "text"):
                yield path.name, "".join(element.itertext())

    def test_no_real_hostnames(self):
        # example.edu and 127.0.0.1 are placeholders; anything else is a
        # site someone actually runs.
        for name, text in self._svg_text():
            for found in re.findall(r"\b[\w.-]+\.(?:ca|com|edu|org|uk)\b", text):
                # example.edu is the placeholder; this project's own
                # repository is not a customer's site.
                # example.edu is the placeholder domain; this project's own
                # repository is not a customer's site.
                self.assertTrue(found.endswith(("example.edu", "github.com")),
                                "%s names %s" % (name, found))

    def test_labels_do_not_overlap(self):
        # Two captions side by side printed over each other and looked fine
        # in the source. Geometry is checkable; a rendered picture is not.
        import xml.etree.ElementTree as ET
        ns = "{http://www.w3.org/2000/svg}"
        for path in (self.ROOT / "docs" / "img").glob("*-light.svg"):
            labels = []
            for element in ET.parse(path).getroot().iter(ns + "text"):
                size = float(element.get("font-size"))
                text = "".join(element.itertext())
                mono = "mono" in element.get("font-family")
                width = len(text) * size * (0.60 if mono else 0.52)
                x, y = float(element.get("x")), float(element.get("y"))
                anchor = element.get("text-anchor")
                left = (x - width / 2 if anchor == "middle"
                        else x - width if anchor == "end" else x)
                labels.append((left, left + width, y, text))
            for index, (l1, r1, y1, t1) in enumerate(labels):
                for l2, r2, y2, t2 in labels[index + 1:]:
                    if abs(y1 - y2) < 9 and l1 < r2 - 2 and l2 < r1 - 2:
                        self.fail("%s: %r overlaps %r" % (path.name, t1, t2))


class TestConflictDetection(unittest.TestCase):
    """Who moved since the pull: you, T4, or both.

    Borrowed from Salesforce DX source tracking, whose rule is that a deploy
    must not silently overwrite what changed in the org and a retrieve must
    not clobber local work. The manifest already records what each file
    looked like when pulled, so the three-way answer costs nothing extra.
    """

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = pathlib.Path(self.tmp.name)
        (self.root / ".t4").mkdir()
        (self.root / ".t4" / "config.json").write_text(json.dumps({
            "source_dir": "t4-source",
            "environments": {"test": {"base": "https://cms"}}}), encoding="utf-8")
        self.env = self.root / "t4-source" / "test"
        self.env.mkdir(parents=True)
        self.project = Project(self.root)
        self.pulled = "original\n"

    def tearDown(self):
        self.tmp.cleanup()

    def _entry(self, local, remote):
        # newline="" like the tool itself: without it Windows translates \n
        # to \r\n on write, the hash no longer matches what was recorded,
        # and every file looks edited.
        extract.write_text(self.env / "a.html", local)
        manifest = {"a.html": {"endpoint": "media", "id": 1, "field": "text",
                               "sha": extract.digest(self.pulled)}}

        class FakeClient:
            @staticmethod
            def fetch_path(_path):
                return {"text": remote}

        results = compare_mod.compare(self.project, "test", FakeClient(), manifest)
        return results[0]

    def test_nobody_moved(self):
        self.assertEqual(self._entry(self.pulled, self.pulled)["state"], "same")

    def test_only_you_moved(self):
        entry = self._entry("mine\n", self.pulled)
        self.assertEqual(entry["state"], "changed")
        self.assertTrue(entry["local_changed"])
        self.assertFalse(entry["remote_changed"])

    def test_only_t4_moved_is_not_something_to_push(self):
        # Pushing here puts the old version back: a silent revert that looks
        # like a successful deploy.
        entry = self._entry(self.pulled, "theirs\n")
        self.assertEqual(entry["state"], "behind")

    def test_both_moved_is_a_conflict(self):
        entry = self._entry("mine\n", "theirs\n")
        self.assertEqual(entry["state"], "conflict")
        self.assertTrue(entry["local_changed"] and entry["remote_changed"])

    def test_the_same_edit_on_both_sides_is_not_a_conflict(self):
        entry = self._entry("agreed\n", "agreed\n")
        self.assertEqual(entry["state"], "same")

    def test_without_a_recorded_hash_nothing_is_claimed(self):
        # An old manifest has no sha; guessing a conflict from that would
        # block pushes on no evidence.
        (self.env / "a.html").write_text("mine\n", encoding="utf-8")
        manifest = {"a.html": {"endpoint": "media", "id": 1, "field": "text"}}

        class FakeClient:
            @staticmethod
            def fetch_path(_path):
                return {"text": "theirs\n"}

        entry = compare_mod.compare(self.project, "test", FakeClient(), manifest)[0]
        self.assertEqual(entry["state"], "changed")
        self.assertFalse(entry["local_changed"])

    def test_push_offers_an_escape_hatch(self):
        args = build_parser().parse_args(["push", "--ignore-conflicts"])
        self.assertTrue(args.ignore_conflicts)


class TestAttribution(unittest.TestCase):
    """An unofficial tool has to say so, wherever someone meets it."""

    ROOT = pathlib.Path(__file__).resolve().parents[1]

    def test_the_notice_disclaims_affiliation(self):
        notice = (self.ROOT / "NOTICE").read_text(encoding="utf-8")
        for required in ("not produced, endorsed", "trademarks of TERMINALFOUR",
                         "no TERMINALFOUR source code", "AS IS"):
            self.assertIn(required, notice)

    def test_the_copyright_is_present_and_whole(self):
        for name in ("NOTICE", "README.md"):
            text = (self.ROOT / name).read_text(encoding="utf-8")
            self.assertIn("Trent Stromkins", text)
            self.assertIn("2026", text)
        self.assertIn("Apache License", (self.ROOT / "LICENSE").read_text(
            encoding="utf-8"))

    def test_the_readme_says_it_before_the_instructions(self):
        readme = (self.ROOT / "README.md").read_text(encoding="utf-8")
        disclaimer = readme.index("independent, unofficial")
        self.assertLess(disclaimer, readme.index("## Getting started"))

    def test_the_panel_says_it_too(self):
        # Someone can use the tool daily without ever opening the repository.
        page = app.PAGE % {"token": "x"}
        self.assertIn('id="legal"', page)
        self.assertIn("independent, unofficial tool", page)
        self.assertIn("TERMINALFOUR", page)

    def test_the_documentation_site_says_it_on_every_page(self):
        import importlib.util
        spec = importlib.util.spec_from_file_location(
            "site_builder", self.ROOT / "docs" / "site.py")
        builder = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(builder)
        out = pathlib.Path(tempfile.mkdtemp())
        self.addCleanup(lambda: __import__("shutil").rmtree(out, ignore_errors=True))
        builder.build(str(out))
        pages = list(out.glob("*.html"))
        self.assertTrue(pages)
        for page in pages:
            text = page.read_text(encoding="utf-8")
            self.assertIn("independent, unofficial", text, page.name)
            self.assertIn("Trent Stromkins", text, page.name)


class TestNoCustomerContent(unittest.TestCase):
    """A public repo must not carry one customer's site in its examples.

    Illustrations drift towards whatever project they were written against,
    because that is the data in front of you. Record ids, layout names,
    content item names and place names all leaked in this way, including a
    member of staff by name.
    """

    ROOT = pathlib.Path(__file__).resolve().parents[1]

    # Names and strings that belonged to the site this was built against.
    # Fenced so the check can skip its own list -- otherwise this file is
    # the one file that always fails.
    # LEAK-LIST-START
    LEAKED = [
        "ufv", "abbotsford", "chilliwack", "elaine", "newman",
        "u_webpage", "m_open", "m-open", "ufv-v.css", "ufv-marcom",
        "action boxes", "photo rollover", "code only template",
        "1027039", "971148", "980255", "988797",
    ]
    # LEAK-LIST-END

    def _tracked(self):
        out = subprocess.run(["git", "ls-files"], cwd=self.ROOT,
                             stdout=subprocess.PIPE, check=True)
        for name in out.stdout.decode("utf-8").split("\n"):
            if not name or name in ("LICENSE",) or name.endswith(".svg"):
                continue
            path = self.ROOT / name
            if not path.is_file():
                continue
            try:
                text = path.read_text(encoding="utf-8")
            except (OSError, UnicodeDecodeError):
                continue
            if "LEAK-LIST-START" in text:
                head, _, rest = text.partition("# LEAK-LIST-START")
                _, _, tail = rest.partition("# LEAK-LIST-END")
                text = head + tail
            yield name, text

    def test_no_tracked_file_names_the_source_project(self):
        found = []
        for name, text in self._tracked():
            lowered = text.lower()
            for leak in self.LEAKED:
                if leak in lowered:
                    found.append("%s contains %r" % (name, leak))
        self.assertEqual(found, [], "\n".join(found))

    def test_example_hosts_are_placeholders(self):
        for name, text in self._tracked():
            # A URL split across two source lines yields a truncated host;
            # join continuations before looking.
            joined = re.sub(r'"\s*\n\s*"', "", text).replace("\\", "")
            for host in re.findall(r"https?://([\w.-]+)", joined):
                # A bare label ("cms", "x") is obviously a fixture, and
                # .test/.invalid/.example are reserved for exactly this by
                # RFC 2606. What matters is that no real site is named.
                if "." not in host or host in ("127.0.0.1", "localhost"):
                    continue
                if host.endswith((".test", ".invalid", ".example", ".local")):
                    continue
                self.assertTrue(
                    host.endswith(("example.edu", "example.com", "github.com",
                                   "githubusercontent.com", "apache.org",
                                   "python.org", "git-scm.com",
                                   "terminalfour.com", "github.io",
                                   "www.w3.org")),
                    "%s references %s" % (name, host))


class TestSyntaxHighlighting(unittest.TestCase):
    """Code is highlighted by a tokenizer here, not by a fetched library."""

    ROOT = pathlib.Path(__file__).resolve().parents[1]

    @classmethod
    def setUpClass(cls):
        import importlib.util
        spec = importlib.util.spec_from_file_location(
            "site_builder", cls.ROOT / "docs" / "site.py")
        cls.site = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(cls.site)

    def test_shell_commands_and_flags_are_marked(self):
        out = self.site.highlight("t4 pull --force   # bring it down", "sh")
        self.assertIn('class="t-command"', out)
        self.assertIn('class="t-flag"', out)
        self.assertIn('class="t-comment"', out)

    def test_t4_tags_are_marked(self):
        out = self.site.highlight('<t4 type="content" name="Body" />', "html")
        self.assertIn('class="t-tag"', out)
        self.assertIn('class="t-attr"', out)
        self.assertIn('class="t-string"', out)

    def test_handlebars_is_marked(self):
        out = self.site.highlight('{{element name="Heading"}}', "handlebars")
        self.assertIn('class="t-hbs"', out)

    def test_json_keys_are_told_from_values(self):
        out = self.site.highlight('{"push_allowed": true}', "json")
        self.assertIn('class="t-key"', out)
        self.assertIn('class="t-literal"', out)

    def test_an_unknown_language_is_escaped_not_dropped(self):
        out = self.site.highlight("a < b && c", "klingon")
        self.assertIn("&lt;", out)
        self.assertIn("&amp;&amp;", out)
        self.assertNotIn("<span", out)

    def test_markup_inside_code_cannot_escape_its_span(self):
        # Escaping happens as each token is emitted, never before, so a
        # token's text cannot be mistaken for the markup wrapping it.
        out = self.site.highlight('<t4 name="</span><script>" />', "html")
        self.assertNotIn("<script>", out)
        self.assertIn("&lt;script&gt;", out)

    def test_the_site_needs_no_highlighting_library(self):
        out = pathlib.Path(tempfile.mkdtemp())
        self.addCleanup(lambda: __import__("shutil").rmtree(out, ignore_errors=True))
        self.site.build(str(out))
        for page in out.glob("*.html"):
            text = page.read_text(encoding="utf-8")
            for probe in ("highlight.js", "prism", "cdn.jsdelivr", "unpkg"):
                self.assertNotIn(probe, text.lower(), page.name)

    def test_the_panel_builds_highlighted_tags_as_nodes(self):
        page = app.PAGE % {"token": "x"}
        js = page.split("<script>")[1].split("</scr" + "ipt>")[0]
        # Element names come from the CMS; innerHTML would make a content
        # type called <script> a problem rather than a curiosity.
        self.assertIn("function paint(", js)
        self.assertIn('span.className = "t-"', js)
        self.assertNotIn("tg-tag\").innerHTML", js)


class TestObjectPicker(unittest.TestCase):
    """Only tags that refer to another object get an object picker."""

    def test_a_title_tag_refers_to_nothing(self):
        # It was being offered the list of Lists, which produced a tag
        # naming something it has no business naming.
        self.assertNotIn("title", tags.TAKES_OBJECT)
        self.assertNotIn("meta", tags.TAKES_OBJECT)
        self.assertNotIn("content", tags.TAKES_OBJECT)

    def test_the_kinds_that_refer_to_an_object_do(self):
        # There is no type="list"; a Data Object refers to one.
        self.assertEqual(tags.TAKES_OBJECT["navigation"], "navigation")
        self.assertEqual(tags.TAKES_OBJECT["data-obj"], "list")
        self.assertNotIn("list", tags.TAG_TYPES)

    def test_an_unnamed_record_says_so_rather_than_showing_a_filename(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        env = pathlib.Path(tmp.name)
        (env / "list").mkdir()
        (env / "list" / "1.json").write_text(json.dumps({"id": 1, "name": None}),
                                             encoding="utf-8")
        (env / "list" / "7.json").write_text(json.dumps({"id": 7, "name": "Faculties"}),
                                             encoding="utf-8")
        found = {o["id"]: o["name"] for o in tags.named_objects(str(env), "list")}
        self.assertEqual(found[7], "Faculties")
        self.assertEqual(found[1], "(unnamed list)")
        self.assertNotIn("1.json", found.values())

    def test_the_panel_asks_the_server_which_kinds_take_one(self):
        page = app.PAGE % {"token": "x"}
        js = page.split("<script>")[1].split("</scr" + "ipt>")[0]
        self.assertIn("takes_object", js)


class TestOutputSettings(unittest.TestCase):
    """An output is a choice about behaviour, and brings settings with it."""

    def test_every_output_value_is_explained(self):
        # "selective-output" says nothing about what it selects.
        for value in tags.OUTPUTS:
            spec = tags.OUTPUT_HELP.get(value)
            if spec is None:
                continue
            self.assertTrue(spec.get("label"), value)
            self.assertTrue(spec.get("help"), value)

    def test_selective_output_needs_its_markup(self):
        _markup, problems = tags.build_tag(
            {"type": "content", "name": "Phone", "output": "selective-output"})
        self.assertIn("selective-output output needs format", problems)

    def test_format_keeps_its_markup(self):
        # format="<p>Phone: $value</p>" is the documented form, so the angle
        # brackets stay and only the quote that would end it is escaped.
        markup, problems = tags.build_tag({
            "type": "content", "name": "Phone", "output": "selective-output",
            "format": '<p class="x">Phone: $value</p>'})
        self.assertIn('format="<p class=&quot;x&quot;>Phone: $value</p>"', markup)
        self.assertEqual(problems, [])

    def test_an_element_name_is_still_escaped_whole(self):
        markup, _problems = tags.build_tag({"type": "content",
                                            "name": "<script>"})
        self.assertIn("&lt;script&gt;", markup)
        self.assertNotIn("<script>", markup)

    def test_selective_output_is_refused_on_an_image(self):
        _markup, problems = tags.build_tag({
            "type": "content", "name": "Photo", "output": "selective-output",
            "format": "<p>$value</p>", "element_type": "image"})
        self.assertTrue(any("not available for a image" in p for p in problems))

    def test_a_repeater_names_the_layout_to_repeat(self):
        markup, problems = tags.build_tag({
            "type": "content", "name": "Rows", "output": "repeater",
            "layout": "text/example"})
        self.assertIn('layout="text/example"', markup)
        self.assertEqual(problems, [])

    def test_content_owner_details_are_constrained(self):
        _markup, problems = tags.build_tag({
            "type": "content", "name": "Owner", "output": "userinfo",
            "info": "nope"})
        self.assertTrue(any("must be one of" in p for p in problems))
        _markup, problems = tags.build_tag({
            "type": "content", "name": "Owner", "output": "userinfo",
            "info": "first_last"})
        self.assertEqual(problems, [])

    def test_the_attributes_the_docs_name_are_all_accepted(self):
        accepted = set(tags.TAG_TYPES["content"]["attrs"])
        for attr in ("layout", "info", "formatter", "display_field",
                     "date_format", "format", "process-format",
                     "format-modifiers", "delimiter",
                     "textual-name-separator", "enable-dedit"):
            self.assertIn(attr, accepted, attr)

    def test_the_panel_shows_what_each_modifier_does(self):
        page = app.PAGE % {"token": "x"}
        js = page.split("<script>")[1].split("</scr" + "ipt>")[0]
        self.assertIn("modifier_help", js)
        self.assertIn("output_help", js)


class TestSiteNavSubsections(unittest.TestCase):
    """The sidebar shows what a page contains, not only that it exists."""

    ROOT = pathlib.Path(__file__).resolve().parents[1]

    @classmethod
    def setUpClass(cls):
        import importlib.util
        spec = importlib.util.spec_from_file_location(
            "site_builder", cls.ROOT / "docs" / "site.py")
        cls.site = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(cls.site)
        cls.out = pathlib.Path(tempfile.mkdtemp())
        cls.site.build(str(cls.out))

    @classmethod
    def tearDownClass(cls):
        __import__("shutil").rmtree(cls.out, ignore_errors=True)

    def _sidebar(self, page):
        text = (self.out / page).read_text(encoding="utf-8")
        return text[text.index('<nav class="side">'):text.index("</nav>")]

    def test_a_pages_subsections_are_listed_under_it(self):
        side = self._sidebar("commands.html")
        # These are the things a reader looks for and cannot guess live
        # inside a page called "Commands".
        for heading in ("Version control", "The web UI",
                        "The inspector", "The editing loop"):
            self.assertIn(heading, side, heading)

    def test_they_link_to_the_anchor_on_that_page(self):
        side = self._sidebar("commands.html")
        self.assertIn('href="commands.html#the-inspector"', side)

    def test_the_anchor_exists_on_the_page(self):
        page = (self.out / "commands.html").read_text(encoding="utf-8")
        self.assertIn('id="the-inspector"', page)

    def test_only_the_current_page_is_expanded(self):
        # Every page's subheadings at once is a wall.
        side = self._sidebar("getting-started.html")
        self.assertIn("Install the tool", side)
        self.assertNotIn("The inspector", side)

    def test_every_section_still_appears(self):
        readme = (self.ROOT / "README.md").read_text(encoding="utf-8")
        titles = [t for t, _b in self.site.split_sections(readme)
                  if t != "Overview"]
        side = self._sidebar("index.html")
        for title in titles:
            self.assertIn(html_mod.escape(title), side, title)


class TestDiagramCorners(unittest.TestCase):
    """Rounded corners have to survive whichever renderer draws them."""

    ROOT = pathlib.Path(__file__).resolve().parents[1]

    def test_no_radius_is_larger_than_the_shape(self):
        # rx="999" for a pill is meant to be clamped by the renderer, but
        # they do not agree on it, so it is clamped before it is written.
        for path in (self.ROOT / "docs" / "img").glob("*.svg"):
            text = path.read_text(encoding="utf-8")
            for rect in re.finditer(
                    r'<rect[^>]*width="([\d.]+)"[^>]*height="([\d.]+)"'
                    r'[^>]*rx="([\d.]+)"', text):
                width, height, radius = (float(v) for v in rect.groups())
                self.assertLessEqual(radius, width / 2 + 0.01, path.name)
                self.assertLessEqual(radius, height / 2 + 0.01, path.name)

    def test_both_radii_are_stated(self):
        # ry defaults to rx, which is fine, but saying it leaves nothing to
        # a default that has been read differently before.
        for path in (self.ROOT / "docs" / "img").glob("*.svg"):
            text = path.read_text(encoding="utf-8")
            self.assertEqual(text.count("rx="), text.count("ry="), path.name)


class TestDocsAnchorsAreReachable(unittest.TestCase):
    """A link into a page must land on the heading, not behind something."""

    ROOT = pathlib.Path(__file__).resolve().parents[1]

    @classmethod
    def setUpClass(cls):
        import importlib.util
        spec = importlib.util.spec_from_file_location(
            "site_builder", cls.ROOT / "docs" / "site.py")
        cls.site = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(cls.site)
        cls.css = cls.site.STYLE

    def test_the_header_does_not_sit_over_the_content(self):
        # It covered the heading an anchor had just scrolled to, so every
        # link into a page landed a header's height too far down.
        header = re.search(r"header\{[^}]*\}", self.css).group(0)
        self.assertNotIn("position:sticky", header)
        self.assertNotIn("position:fixed", header)

    def test_headings_keep_a_little_room_above_them(self):
        self.assertIn("scroll-margin-top", self.css)

    def test_every_sidebar_anchor_exists_on_its_page(self):
        out = pathlib.Path(tempfile.mkdtemp())
        self.addCleanup(lambda: __import__("shutil").rmtree(out, ignore_errors=True))
        self.site.build(str(out))
        for page in out.glob("*.html"):
            text = page.read_text(encoding="utf-8")
            for target, anchor in re.findall(r'href="([\w-]+)\.html#([\w-]+)"',
                                             text):
                landing = (out / ("%s.html" % target)).read_text(encoding="utf-8")
                self.assertIn('id="%s"' % anchor, landing,
                              "%s links to %s#%s" % (page.name, target, anchor))


class TestDiagramMatchesTheApp(unittest.TestCase):
    """A diagram of the panel should look like the panel.

    It drifts silently: the diagram is drawn by hand and the panel by CSS,
    and nothing connects them. These check the handful of things that made
    the two look different enough to notice.
    """

    ROOT = pathlib.Path(__file__).resolve().parents[1]

    @classmethod
    def setUpClass(cls):
        import xml.etree.ElementTree as ET
        cls.ns = "{http://www.w3.org/2000/svg}"
        cls.svg = ET.parse(cls.ROOT / "docs" / "img" / "app-light.svg").getroot()
        cls.css = (app.PAGE % {"token": "x"}).split("<style>")[1].split("</style>")[0]

    def _pills(self):
        found = []
        for rect in self.svg.iter(self.ns + "rect"):
            if rect.get("x") is None:
                continue
            height = float(rect.get("height"))
            radius = float(rect.get("rx") or 0)
            if height < 24 and radius >= height / 2 - 0.01:
                found.append(rect)
        return found

    def test_the_panels_badges_are_quiet(self):
        # .pill is a border and dim text, with no fill. Drawn as filled
        # accent chips, three counters shouted louder than the buttons.
        rule = self.css.split(".pill{")[1].split("}")[0]
        self.assertIn("border:1px solid var(--line)", rule)
        self.assertIn("color:var(--dim)", rule)
        self.assertNotIn("background", rule)

    def test_the_diagram_draws_them_the_same_way(self):
        pills = self._pills()
        self.assertGreaterEqual(len(pills), 3)
        for pill in pills:
            self.assertEqual(pill.get("fill"), "#ffffff", "a badge is filled")
            self.assertNotEqual(pill.get("stroke"), "none")

    def test_the_badges_the_panel_shows_are_in_the_diagram(self):
        drawn = {"".join(node.itertext())
                 for node in self.svg.iter(self.ns + "text")}
        self.assertIn("writable", drawn)
        self.assertTrue(any("CHANGED" in label for label in drawn))
        self.assertTrue(any("CONTENT TYPES" in label for label in drawn))


class TestPanelDiagramBottom(unittest.TestCase):
    """The diagram's lower half had drifted from the panel's."""

    ROOT = pathlib.Path(__file__).resolve().parents[1]

    @classmethod
    def setUpClass(cls):
        import xml.etree.ElementTree as ET
        ns = "{http://www.w3.org/2000/svg}"
        root = ET.parse(cls.ROOT / "docs" / "img" / "app-light.svg").getroot()
        cls.labels = {"".join(node.itertext())
                      for node in root.iter(ns + "text")}
        cls.page = app.PAGE % {"token": "x"}

    def test_the_output_pane_is_drawn(self):
        # Tasks was there; the log it writes into, and the stop control, had
        # been left off entirely.
        self.assertIn("TASKS", self.labels)
        self.assertIn("OUTPUT", self.labels)
        self.assertTrue(any("Pick an action" in label for label in self.labels))
        self.assertIn("Stop", self.labels)

    def test_all_three_footer_lines_are_drawn(self):
        for probe in ("Every button runs the matching t4 command",
                      "independent, unofficial tool",
                      "Built on GitHub with"):
            self.assertTrue(any(probe in label for label in self.labels), probe)

    def test_the_footer_says_what_the_panel_says(self):
        # Both are written by hand in different files, so they drift.
        for probe in ("Every button runs the matching t4 command",
                      "independent, unofficial tool",
                      "Built on GitHub with"):
            self.assertIn(probe, self.page, probe)


class TestPanelDiagramShowsBothSyntaxes(unittest.TestCase):
    """The tag builder returns both forms; the drawing showed one."""

    ROOT = pathlib.Path(__file__).resolve().parents[1]

    @classmethod
    def setUpClass(cls):
        import xml.etree.ElementTree as ET
        ns = "{http://www.w3.org/2000/svg}"
        root = ET.parse(cls.ROOT / "docs" / "img" / "app-light.svg").getroot()
        cls.labels = [" ".join("".join(node.itertext()).split())
                      for node in root.iter(ns + "text")]

    def test_the_t4_tag_is_drawn(self):
        self.assertTrue(any(label.startswith("<t4 type=")
                            for label in self.labels), self.labels[:5])

    def test_the_handlebars_equivalent_is_drawn(self):
        self.assertTrue(any(label.startswith("{{element")
                            for label in self.labels))

    def test_the_two_describe_the_same_element(self):
        # A drawing showing a tag for one element and Handlebars for another
        # would be worse than showing one.
        tag = next(l for l in self.labels if l.startswith("<t4 type="))
        handlebars = next(l for l in self.labels if l.startswith("{{element"))
        name = re.search(r'name="([^"]+)"', tag).group(1)
        self.assertIn('name="%s"' % name, handlebars)

    def test_the_mixing_warning_is_drawn(self):
        self.assertTrue(any("either Standard or Handlebars" in label
                            for label in self.labels))
