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
from t4sync.inspector import (ContentTypeIndex, InspectorContext,  # noqa: E402
                              LayoutIndex, annotate_components,
                              annotate_content_items, build_comment,
                              build_panel, strip_injected)
from t4sync.progress import Progress   # noqa: E402
from t4sync.render import (LayoutResolver, align,   # noqa: E402
                           preview, render)
from t4sync.compare import read_field, remote_path, write_field  # noqa: E402
from t4sync.contentlayout import format_key, layout_name  # noqa: E402
from t4sync.commands.pull import media_filename   # noqa: E402
from t4sync import sections as sectionlib   # noqa: E402
from t4sync.sections import (ContentItems, MediaSources,   # noqa: E402
                             SectionLayouts, by_url_path, layout_for)
from t4sync.serve import (RELOAD_PATH, Watcher, load_rules,   # noqa: E402
                          rules_for_published_urls, transform_html)


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

    def test_highlight_toggle_is_present(self):
        panel = build_panel("<html></html>", LayoutIndex({}))
        self.assertIn('id="t4i-hl"', panel)
        self.assertIn("Highlight components", panel)


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
            self.assertEqual(sources.path_for("/media/web/b3/css/v8.css"),
                             str(media / "v8.css"))
            self.assertEqual(sources.path_for("/media/web/b3/css/v8.css?v=2"),
                             str(media / "v8.css"))

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

    def test_a_dropped_request_does_not_end_the_walk(self):
        class Flaky(FakeClient):
            def get_json(self, path):
                if path == "hierarchy/2/en/subsections":
                    raise OSError("connection reset")
                return FakeClient.get_json(self, path)

        found = sectionlib.walk(Flaky(), 1, "en")
        self.assertIn("1", found)
        self.assertIn("3", found)      # the walk carried on past the failure


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
