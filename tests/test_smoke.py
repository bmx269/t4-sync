"""Smoke tests that need no network and no T4 instance."""
import argparse
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
from t4sync import app, cli, history, opener   # noqa: E402
from t4sync.config import Project      # noqa: E402
from t4sync.inspector import (ContentLayoutIndex, ContentTypeIndex,  # noqa: E402
                              InspectorContext, LayoutIndex,
                              annotate_components, annotate_content_items,
                              build_comment, build_panel, group_by_type,
                              page_content, strip_injected, SCRIPT)
from t4sync.progress import Progress   # noqa: E402
from t4sync.render import (LayoutResolver, align,   # noqa: E402
                           preview, render)
from t4sync.compare import read_field, remote_path, write_field  # noqa: E402
from t4sync.contentlayout import format_key, layout_name  # noqa: E402
from t4sync.commands.pull import media_filename   # noqa: E402
from t4sync import sections as sectionlib   # noqa: E402
from t4sync.sections import (ContentItems, MediaSources,   # noqa: E402
                             SectionLayouts, by_url_path, layout_for)
from t4sync import serve   # noqa: E402
from t4sync.serve import (RELOAD_PATH, Watcher, load_rules,   # noqa: E402
                          rules_for_published_urls, transform_html)
from t4sync.errors import T4Error   # noqa: E402
from t4sync.vcs import Repo, message_for, require_clean   # noqa: E402
from t4sync.commands import site   # noqa: E402


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


class TestInspector(unittest.TestCase):
    MANIFEST = {
        "pageLayout/my-layout.header.html": {
            "endpoint": "pageLayout", "id": 1027039,
            "field": "headerCode", "name": "my_layout"},
        "pageLayout/my-layout.footer.html": {
            "endpoint": "pageLayout", "id": 1027039,
            "field": "footerCode", "name": "my_layout"},
        "contenttype/thing.json": {
            "endpoint": "contenttype", "id": 5, "field": None, "name": "Thing"},
    }
    PAGE = ('<html><head>'
            '<meta name="t4-layout" content="my_layout">'
            '<meta name="t4-layout-id" content="1027039">'
            '<link rel="stylesheet" href="/media/css/site.css">'
            '<script src="/media/js/app.js"></script>'
            '</head><body>hi</body></html>')

    def test_index_only_tracks_page_layouts(self):
        index = LayoutIndex(self.MANIFEST)
        self.assertEqual(len(index.files_for(1027039)), 2)
        self.assertEqual(index.files_for(999), [])

    def test_panel_names_the_layout_and_its_files(self):
        panel = build_panel(self.PAGE, LayoutIndex(self.MANIFEST), env_name="test")
        self.assertIn("my_layout", panel)
        self.assertIn("1027039", panel)
        self.assertIn("pageLayout/my-layout.header.html", panel)
        self.assertIn("pageLayout/my-layout.footer.html", panel)

    def test_panel_lists_local_assets_only(self):
        page = self.PAGE.replace("</head>",
                                 '<link rel="stylesheet" href="https://cdn.example/x.css"></head>')
        panel = build_panel(page, LayoutIndex(self.MANIFEST))
        self.assertIn("media/css/site.css", panel)
        self.assertIn("media/js/app.js", panel)
        self.assertNotIn("cdn.example", panel)

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
                                     "field": "headerCode", "name": "u_webpage.production"},
        "pageLayout/b.header.html": {"endpoint": "pageLayout", "id": 2,
                                     "field": "headerCode", "name": "u_webpage.webdev"},
    }

    def test_exact_name_resolves_to_a_file(self):
        manifest = {"pageLayout/m.header.html": {
            "endpoint": "pageLayout", "id": 7, "field": "headerCode", "name": "m_open"}}
        page = '<meta name="t4-layout" content="m_open">'
        out = build_comment(page, LayoutIndex(manifest), env_name="test")
        self.assertIn("PAGE LAYOUT: 'm_open'", out)
        self.assertIn("x t4-source/test/pageLayout/m.header.html", out)
        self.assertNotIn("SUGGESTIONS", out)

    def test_ambiguous_name_lists_candidates(self):
        """A published name that maps to several layouts must not be guessed."""
        page = '<meta name="t4-layout" content="u_webpage">'
        out = build_comment(page, LayoutIndex(self.AMBIGUOUS), env_name="test")
        self.assertIn("LAYOUT NAME SUGGESTIONS", out)
        self.assertIn("u_webpage.production", out)
        self.assertIn("u_webpage.webdev", out)
        self.assertNotIn("x t4-source", out)      # nothing claimed as used

    def test_id_wins_over_an_ambiguous_name(self):
        page = ('<meta name="t4-layout" content="u_webpage">'
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
        page = '<body><a href="https://www.x.com/a">x</a></body>'
        rules = [(re.compile(r"https://www\.x\.com/"), "/")]
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

    A = {"id": 1, "name": "u_webpage.production",
         "headerCode": '<!--top--><t4 type="media" id="100" />',
         "footerCode": "<!--bottom-->"}
    B = {"id": 2, "name": "u_webpage.webdev",
         "headerCode": '<!--top--><t4 type="media" id="200" />',
         "footerCode": "<!--bottom-->"}
    C = {"id": 3, "name": "m_open",
         "headerCode": "<!--other-->", "footerCode": "<!--end-->"}

    def resolver(self, *records):
        return LayoutResolver(None, list(records))

    def test_unique_name_needs_no_guessing(self):
        rec, why = self.resolver(self.A, self.C).resolve("<html>", "m_open")
        self.assertEqual(rec["name"], "m_open")
        self.assertIn("uniquely", why)

    def test_id_is_preferred_when_published(self):
        rec, _why = self.resolver(self.A, self.B).resolve("<html>", "u_webpage", 2)
        self.assertEqual(rec["name"], "u_webpage.webdev")

    def test_distinguishing_tag_id_decides(self):
        """Same literals; only the media id tells them apart."""
        page = "<html><!--top--><!-- 100 css -->rendered<!--bottom--></html>"
        rec, why = self.resolver(self.A, self.B).resolve(page, "u_webpage")
        self.assertEqual(rec["name"], "u_webpage.production")
        self.assertIn("tag id", why)

    def test_alignment_alone_can_decide(self):
        page = "<html><!--other-->x<!--end--></html>"
        rec, why = self.resolver(self.A, self.C).resolve(page, "m_open")
        self.assertEqual(rec["name"], "m_open")

    def test_truly_identical_layouts_are_not_guessed(self):
        twin = dict(self.A, id=9, name="u_webpage.copy")
        page = "<html><!--top--><!-- 100 css -->x<!--bottom--></html>"
        rec, why = self.resolver(self.A, twin).resolve(page, "u_webpage")
        self.assertIsNone(rec)
        self.assertIn("indistinguishable", why)

    def test_unknown_name(self):
        rec, why = self.resolver(self.A).resolve("<html>", "nothing_like_this")
        self.assertIsNone(rec)
        self.assertIn("no layout named", why)


class TestPanelEscaping(unittest.TestCase):
    """The panel must not emit raw markup from data, nor escape its own."""

    def test_layout_note_is_escaped_not_double_escaped(self):
        page = '<meta name="t4-layout" content="m_open">'

        class FakeResolver:
            @staticmethod
            def resolve(_body, _name, _id=None):
                return {"id": 1, "name": "m_open & co"}, "named uniquely"

        panel = build_panel(page, LayoutIndex({}), resolver=FakeResolver())
        self.assertIn("m_open &amp; co", panel)      # data is escaped
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
            "name": "u_webpage.production"}}
        sections = SectionLayouts({"10": {
            "id": 10, "name": "About", "path": "/Root/about",
            "channels": [{"id": 13, "pageLayout": 100}]}})
        page = '<meta name="t4-layout" content="u_webpage">'
        out = build_comment(page, LayoutIndex(manifest), env_name="test",
                            url_path="/about/", sections=sections)
        self.assertIn("assigned in T4 to section 10", out)
        self.assertIn("x t4-source/test/pageLayout/exact.header.html", out)
        self.assertNotIn("SUGGESTIONS", out)


class TestContentItemAnnotation(unittest.TestCase):
    """T4 emits <span id="d.en.<id>"> for every content item it renders."""

    ITEMS = ContentItems({"980255": {"id": 980255, "name": "Action box A",
                                     "contentTypeId": 326,
                                     "contentTypeName": "Action boxes"}})

    def test_anchor_is_named(self):
        page = '<span id="d.en.980255"></span><p>x</p>'
        out, n = annotate_content_items(page, self.ITEMS, env_name="test")
        self.assertEqual(n, 1)
        self.assertIn("T4 CONTENT: #980255 'Action box A'", out)
        self.assertIn("of type 'Action boxes'", out)
        self.assertEqual(strip_injected(out), page)   # removable, exactly

    def test_unknown_anchor_is_left_alone(self):
        page = '<span id="d.en.999999"></span>'
        out, n = annotate_content_items(page, self.ITEMS, env_name="test")
        self.assertEqual((out, n), (page, 0))

    def test_other_languages_match(self):
        page = '<span id="d.fr.980255"></span>'
        out, n = annotate_content_items(page, self.ITEMS, env_name="test")
        self.assertEqual(n, 1)

    def test_no_data_is_a_no_op(self):
        page = '<span id="d.en.980255"></span>'
        self.assertEqual(annotate_content_items(page, ContentItems({})), (page, 0))

    def test_content_layout_file_is_named(self):
        page = '<span id="d.en.980255"></span><p>x</p>'
        out, _n = annotate_content_items(
            page, self.ITEMS, env_name="test",
            content_layouts=ContentLayoutIndex(CONTENT_LAYOUT_MANIFEST))
        self.assertIn("layout -> t4-source/test/contentLayout/action-boxes.html",
                      out)
        self.assertEqual(strip_injected(out), page)

    def test_missing_layout_is_reported_not_omitted(self):
        out, _n = annotate_content_items('<span id="d.en.980255"></span>',
                                         self.ITEMS, env_name="test",
                                         content_layouts=ContentLayoutIndex({}))
        self.assertIn("none pulled for this type", out)


CONTENT_LAYOUT_MANIFEST = {
    "contentLayout/action-boxes.html": {
        "endpoint": "layout", "contentType": "Action boxes",
        "name": "text/html", "id": 975973},
    "contentLayout/action-boxes-rss.html": {
        "endpoint": "layout", "contentType": "Action boxes",
        "name": "text/rss", "id": 975974},
    "pageLayout/u_webpage.html": {"endpoint": "pageLayout", "id": 1},
}


class TestContentLayoutIndex(unittest.TestCase):
    """Content layouts are keyed by the type's name, not its id."""

    INDEX = ContentLayoutIndex(CONTENT_LAYOUT_MANIFEST)

    def test_groups_every_layout_of_a_type(self):
        found = self.INDEX.for_type("Action boxes")
        self.assertEqual([rel for rel, _label in found],
                         ["contentLayout/action-boxes-rss.html",
                          "contentLayout/action-boxes.html"])

    def test_lookup_is_case_insensitive(self):
        self.assertTrue(self.INDEX.for_type("ACTION BOXES"))

    def test_falls_through_to_the_alias(self):
        self.assertTrue(self.INDEX.for_type("Not a type", "Action boxes"))

    def test_page_layouts_are_not_content_layouts(self):
        self.assertEqual(self.INDEX.for_type("u_webpage"), [])

    def test_unknown_type_is_empty_not_an_error(self):
        self.assertEqual(self.INDEX.for_type(None), [])


class TestPageContent(unittest.TestCase):
    """What the panel lists: items on the page, grouped by content type."""

    ITEMS = ContentItems({
        "1": {"id": 1, "name": "Box A", "contentTypeName": "Action boxes"},
        "2": {"id": 2, "name": "Box B", "contentTypeName": "Action boxes"},
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
        self.assertEqual([g["type"] for g in groups], ["Action boxes", "Hero"])
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

    def test_normalised(self):
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
                                    "contentTypeName": "Code Only Template"}})
        layouts = ContentLayoutIndex({
            "contentLayout/code-only-template--textcode.html": {
                "endpoint": "layout", "contentType": "Code Only Template",
                "name": "text/code"}})
        out, _n = annotate_content_items('<span id="d.en.1"></span>', items,
                                         env_name="test", content_layouts=layouts)
        self.assertIn("contentLayout/code-only-template--textcode.html", out)

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
            "path": "t4-source/test/pageLayout/u_webpage.footer.html",
            "meta": {"endpoint": "pageLayout", "id": 1100982, "name": "u_webpage"},
        }])
        self.assertIn("pageLayout #1100982", message)
        self.assertIn("(u_webpage)", message)
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
                                          "contentTypeName": "Action boxes"}}))

    def test_the_control_names_its_side_effect(self):
        self.assertIn('id="t4i-pick"', self.PANEL)
        self.assertIn("links off", self.PANEL)

    def test_type_rows_are_addressable_from_the_page(self):
        # Pick mode resolves a clicked element to a type, then has to find
        # the row listing that type's files.
        self.assertIn('data-type="Action boxes"', self.PANEL)

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
    """The control panel: a token gate, and commands it will not run."""

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
                                          "contentTypeName": "Action boxes"}}),
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

    MANIFEST = {"media/ufv-v.css": {"endpoint": "media", "id": 1,
                                    "field": "text", "name": "ufv-v.css"},
                "media/site.js": {"endpoint": "media", "id": 2,
                                  "field": "text", "name": "site.js"}}

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = pathlib.Path(self.tmp.name)
        self.env = self.root / "t4-source" / "test"
        (self.env / "media").mkdir(parents=True)
        (self.env / "media" / "ufv-v.css").write_text("a{}", encoding="utf-8")
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
        self._override("media/web/b3/css/ufv-v.css")
        self.assertEqual(serve.shadowing_overrides(str(self.overrides), self.media),
                         ["media/web/b3/css/ufv-v.css"])

    def test_matching_is_by_filename_not_path(self):
        # The published URL and the pulled file agree on the name, not the
        # path, which is also how serving resolves it.
        self._override("totally/different/place/ufv-v.css")
        self.assertEqual(len(serve.shadowing_overrides(str(self.overrides), self.media)), 1)

    def test_an_override_of_something_not_pulled_is_fine(self):
        self._override("media/web/vendor/jquery.js")
        self.assertEqual(serve.shadowing_overrides(str(self.overrides), self.media), [])

    def test_a_pulled_file_with_no_override_is_fine(self):
        self.assertEqual(serve.shadowing_overrides(str(self.overrides), self.media), [])

    def test_no_overrides_directory_is_not_an_error(self):
        self.assertEqual(serve.shadowing_overrides(None, self.media), [])
