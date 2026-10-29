#!/usr/bin/env python3
"""Build the documentation site from README.md.

    python3 docs/site.py [--out _site]

One source of truth: the README is what people read on GitHub, so the site is
generated from it rather than kept beside it to drift. Each `##` section
becomes a page, each `###` an entry in that page's contents.

The Markdown support here is a subset -- exactly what the README uses, and no
more. A general parser would be a dependency or a thousand lines, and neither
is worth it to render one file we control. Anything unrecognized is passed
through as HTML, which is also how the `<picture>` blocks survive.

Standard library only, like the rest of the project.
"""
import argparse
import html
import pathlib
import re
import shutil

HERE = pathlib.Path(__file__).resolve().parent
ROOT = HERE.parent

# Sections that are reference rather than narrative, grouped in the sidebar
# so the walkthrough is not buried in them.
GROUPS = [
    ("Start here", ["What it does", "Getting started"]),
    ("Using it", ["How it is organized", "Commands"]),
    ("Knowing the limits", ["What this does not do", "Things that will bite you"]),
    ("The project", ["Development", "Licence"]),
]


# -- a very small Markdown subset ---------------------------------------

INLINE = [
    (re.compile(r"`([^`]+)`"), lambda m: "<code>%s</code>" % html.escape(m.group(1))),
    (re.compile(r"\*\*([^*]+)\*\*"), lambda m: "<strong>%s</strong>" % m.group(1)),
    (re.compile(r"(?<![\w*])\*([^*\n]+)\*(?![\w*])"), lambda m: "<em>%s</em>" % m.group(1)),
    (re.compile(r"\[([^\]]+)\]\(([^)]+)\)"),
     lambda m: '<a href="%s">%s</a>' % (html.escape(m.group(2), quote=True), m.group(1))),
]


def inline(text):
    """Inline markup, code spans first so their contents are left alone."""
    out, last = [], 0
    for match in re.finditer(r"`[^`]+`", text):
        out.append(_inline_rest(text[last:match.start()]))
        out.append("<code>%s</code>" % html.escape(match.group(0)[1:-1]))
        last = match.end()
    out.append(_inline_rest(text[last:]))
    return "".join(out)


def _inline_rest(text):
    text = html.escape(text, quote=False)
    for pattern, repl in INLINE[1:]:
        text = pattern.sub(repl, text)
    return text


def slug(text):
    return re.sub(r"[^a-z0-9]+", "-", text.lower()).strip("-")


def render(markdown):
    """Markdown to HTML, plus the `###` headings found, in order."""
    lines = markdown.split("\n")
    out, headings = [], []
    index, pending = 0, []

    def flush():
        if pending:
            out.append("<p>%s</p>" % inline(" ".join(pending).strip()))
            pending.clear()

    while index < len(lines):
        line = lines[index]

        if line.startswith("```"):
            flush()
            language = line[3:].strip()
            body = []
            index += 1
            while index < len(lines) and not lines[index].startswith("```"):
                body.append(lines[index])
                index += 1
            out.append('<pre class="code%s"><code>%s</code></pre>'
                       % (" lang-" + slug(language) if language else "",
                          highlight("\n".join(body), language)))
            index += 1
            continue

        # Raw HTML at block level -- this is how <picture> survives.
        if line.lstrip().startswith("<") and not line.lstrip().startswith("<t4"):
            flush()
            block = [line]
            index += 1
            opened = re.match(r"\s*<(\w+)", line)
            if opened and ("</%s>" % opened.group(1)) not in line:
                closing = "</%s>" % opened.group(1)
                while index < len(lines) and closing not in lines[index]:
                    block.append(lines[index])
                    index += 1
                if index < len(lines):
                    block.append(lines[index])
                    index += 1
            out.append("\n".join(block))
            continue

        heading = re.match(r"^(#{1,6})\s+(.*)$", line)
        if heading:
            flush()
            level, text = len(heading.group(1)), heading.group(2)
            anchor = slug(re.sub(r"[`*]", "", text))
            if level == 3:
                headings.append((text, anchor))
            out.append('<h%d id="%s">%s</h%d>' % (level, anchor, inline(text), level))
            index += 1
            continue

        if line.startswith("|") and index + 1 < len(lines) and re.match(
                r"^\|[\s:|-]+\|$", lines[index + 1]):
            flush()
            header = [c.strip() for c in line.strip("|").split("|")]
            index += 2
            rows = []
            while index < len(lines) and lines[index].startswith("|"):
                rows.append([c.strip() for c in lines[index].strip("|").split("|")])
                index += 1
            out.append("<table><thead><tr>%s</tr></thead><tbody>%s</tbody></table>" % (
                "".join("<th>%s</th>" % inline(c) for c in header),
                "".join("<tr>%s</tr>" % "".join("<td>%s</td>" % inline(c) for c in row)
                        for row in rows)))
            continue

        bullet = re.match(r"^[-*]\s+(.*)$", line)
        number = re.match(r"^\d+\.\s+(.*)$", line)
        if bullet or number:
            flush()
            tag = "ul" if bullet else "ol"
            items = []
            while index < len(lines):
                match = re.match(r"^[-*]\s+(.*)$" if bullet else r"^\d+\.\s+(.*)$",
                                 lines[index])
                if not match:
                    break
                items.append(match.group(1))
                index += 1
            out.append("<%s>%s</%s>" % (
                tag, "".join("<li>%s</li>" % inline(i) for i in items), tag))
            continue

        if line.startswith(">"):
            flush()
            quote = []
            while index < len(lines) and lines[index].startswith(">"):
                quote.append(lines[index].lstrip("> ").rstrip())
                index += 1
            out.append("<blockquote>%s</blockquote>" % inline(" ".join(quote)))
            continue

        if not line.strip():
            flush()
        else:
            pending.append(line.strip())
        index += 1

    flush()
    return "\n".join(out), headings


# -- syntax highlighting ------------------------------------------------
#
# A tokeniser per language rather than a dependency: the README uses five,
# and highlighting them badly is worse than not highlighting at all, so each
# pattern set is deliberately small and only claims what it is sure of.
#
# Everything is escaped as it is emitted, never before, so a token's text
# cannot be mistaken for the markup wrapping it.

GRAMMARS = {
    "sh": [
        ("comment", r"#[^\n]*"),
        ("string", r"\"[^\"\n]*\"|'[^'\n]*'"),
        ("flag", r"(?<=\s)--?[\w-]+"),
        ("command", r"^(?:\$ )?(?:t4|git|python3|cd|mkdir|open|curl)\b"),
        ("path", r"(?<![\w/])(?:~|\./)?[\w.-]+/[\w./-]+"),
    ],
    "powershell": [
        ("comment", r"#[^\n]*"),
        ("string", r"\"[^\"\n]*\""),
        ("flag", r"(?<=\s)-[A-Za-z]\w*"),
        ("command", r"^(?:\.\\)?[\w.-]+\b"),
    ],
    "html": [
        ("comment", r"&lt;!--.*?--&gt;|<!--.*?-->"),
        ("hbs", r"\{\{[^}]*\}\}"),
        ("tag", r"</?[\w:-]+|/?>"),
        ("attr", r"[\w:-]+(?==)"),
        ("string", r"\"[^\"]*\"|'[^']*'"),
    ],
    "json": [
        ("key", r"\"[^\"]*\"(?=\s*:)"),
        ("string", r"\"[^\"]*\""),
        ("number", r"\b-?\d+(?:\.\d+)?\b"),
        ("literal", r"\b(?:true|false|null)\b"),
    ],
    "diff": [
        ("hunk", r"^@@.*$"),
        ("add", r"^\+.*$"),
        ("del", r"^-.*$"),
    ],
    "handlebars": [
        ("comment", r"\{\{!.*?\}\}"),
        ("hbs", r"\{\{[^}]*\}\}"),
        ("tag", r"</?[\w:-]+|/?>"),
        ("attr", r"[\w:-]+(?==)"),
        ("string", r"\"[^\"]*\"|'[^']*'"),
    ],
}
GRAMMARS["console"] = GRAMMARS["sh"]
GRAMMARS["bash"] = GRAMMARS["sh"]
GRAMMARS["hbs"] = GRAMMARS["handlebars"]

_COMPILED = {
    name: re.compile("|".join("(?P<%s>%s)" % (kind, pattern)
                              for kind, pattern in rules), re.S | re.M)
    for name, rules in GRAMMARS.items()
}


def highlight(code, language):
    """Wrap recognized tokens in spans. Unknown languages pass through."""
    grammar = _COMPILED.get((language or "").lower())
    if not grammar:
        return html.escape(code)
    out, last = [], 0
    for match in grammar.finditer(code):
        out.append(html.escape(code[last:match.start()]))
        out.append('<span class="t-%s">%s</span>'
                   % (match.lastgroup, html.escape(match.group(0))))
        last = match.end()
    out.append(html.escape(code[last:]))
    return "".join(out)


# -- the site ------------------------------------------------------------

def split_sections(markdown):
    """[(title, body)] for each `##` section, with the preamble first."""
    parts = re.split(r"^## (.+)$", markdown, flags=re.M)
    sections = [("Overview", parts[0])]
    for title, body in zip(parts[1::2], parts[2::2]):
        sections.append((title.strip(), body))
    return sections


def nav(sections, current):
    """The sidebar, grouped so the walkthrough is not lost in the reference."""
    titles = [title for title, _body in sections]
    placed, out = set(), []
    for label, wanted in GROUPS:
        items = [t for t in wanted if t in titles]
        if not items:
            continue
        out.append('<div class="navgroup">%s</div>' % html.escape(label))
        for title in items:
            placed.add(title)
            out.append(_nav_link(title, current))
    rest = [t for t in titles if t not in placed and t != "Overview"]
    if rest:
        out.append('<div class="navgroup">More</div>')
        out += [_nav_link(t, current) for t in rest]
    return "\n".join(out)


def _nav_link(title, current):
    return '<a class="navlink%s" href="%s.html">%s</a>' % (
        " on" if title == current else "", slug(title), html.escape(title))


def page(title, body, sections, headings, home=False):
    contents = ""
    if headings:
        contents = ('<nav class="toc"><div class="navgroup">On this page</div>%s</nav>'
                    % "".join('<a href="#%s">%s</a>' % (anchor, html.escape(text))
                              for text, anchor in headings))
    return TEMPLATE % {
        "title": html.escape(title),
        "suffix": "" if home else " &middot; t4-sync",
        "nav": nav(sections, title),
        "toc": contents,
        "body": body,
        "style": STYLE,
    }


def build(out_dir):
    out = pathlib.Path(out_dir)
    if out.exists():
        shutil.rmtree(out)
    out.mkdir(parents=True)

    readme = (ROOT / "README.md").read_text(encoding="utf-8")
    sections = split_sections(readme)

    written = []
    for title, body in sections:
        html_body, headings = render(body)
        name = "index" if title == "Overview" else slug(title)
        if title == "Overview":
            html_body = HERO + html_body
        (out / ("%s.html" % name)).write_text(
            page(title, html_body, sections, headings, home=(name == "index")),
            encoding="utf-8")
        written.append(name)

    shutil.copytree(HERE / "img", out / "docs" / "img")
    # Serve the files as they are: this is not a Jekyll site, and without
    # this GitHub would try to build it as one and drop the _-prefixed paths.
    (out / ".nojekyll").write_text("", encoding="utf-8")
    return written


HERO = '''
<div class="hero">
  <h1>t4-sync</h1>
  <p class="tagline">A T4 CMS Local Development Tool</p>
  <p>Work on a Terminal Four site from your own computer: bring the templates
     down, edit them in a normal editor, see the result immediately, and send
     them back when you are happy.</p>
  <p class="herolinks">
    <a class="cta" href="getting-started.html">Get started</a>
    <a class="cta ghost" href="https://github.com/bmx269/t4-sync">View on GitHub</a>
  </p>
</div>
'''

STYLE = """
:root{color-scheme:light dark;--bg:#fff;--fg:#1a1d25;--dim:#5a6376;
 --line:#e2e5ec;--card:#f7f8fa;--accent:#2b4bbf;--code-bg:#0b0d12;
 --code-fg:#dbe1ee;--btn:#e9edf6;--btn-edge:#6f7c9c}
@media (prefers-color-scheme:dark){:root{--bg:#12141a;--fg:#e8eaf0;
 --dim:#99a1b3;--line:#262b36;--card:#191c24;--accent:#9fb4ff;
 --code-bg:#05070b;--btn:#252b38;--btn-edge:#687490}}
*{box-sizing:border-box}
body{margin:0;background:var(--bg);color:var(--fg);
 font:15px/1.65 ui-sans-serif,system-ui,-apple-system,"Segoe UI",sans-serif}
a{color:var(--accent)}
header{position:sticky;top:0;z-index:5;background:var(--bg);
 border-bottom:1px solid var(--line);padding:.6rem 1.2rem;display:flex;
 gap:1rem;align-items:baseline}
header b{font-size:14px;letter-spacing:.02em}
header .sub{color:var(--dim);font-size:12.5px}
.wrap{display:grid;grid-template-columns:15rem minmax(0,1fr) 13rem;gap:2rem;
 max-width:82rem;margin:0 auto;padding:1.5rem 1.2rem 4rem;align-items:start}
@media (max-width:1100px){.wrap{grid-template-columns:14rem minmax(0,1fr)}
 .toc{display:none}}
@media (max-width:760px){.wrap{grid-template-columns:minmax(0,1fr)}
 nav.side{position:static}}
nav.side,.toc{position:sticky;top:3.6rem;font-size:13.5px}
.navgroup{color:var(--dim);font-size:10.5px;font-weight:700;
 text-transform:uppercase;letter-spacing:.07em;margin:1rem 0 .3rem}
.navgroup:first-child{margin-top:0}
.navlink,.toc a{display:block;padding:.18rem 0;color:var(--fg);
 text-decoration:none;border-left:2px solid transparent;padding-left:.6rem}
.navlink:hover,.toc a:hover{color:var(--accent);border-left-color:var(--line)}
.navlink.on{color:var(--accent);border-left-color:var(--accent);font-weight:600}
.toc a{color:var(--dim);font-size:12.5px}
main{min-width:0}
h1{font-size:1.9rem;line-height:1.2;margin:.2rem 0 1rem}
h2{font-size:1.35rem;margin:2rem 0 .6rem;padding-top:.4rem}
h3{font-size:1.1rem;margin:1.6rem 0 .5rem}
h4{font-size:1rem;margin:1.2rem 0 .4rem;color:var(--dim)}
p{margin:.7rem 0}
code{font-family:ui-monospace,SFMono-Regular,Menlo,monospace;font-size:.88em;
 background:var(--card);padding:.1rem .3rem;border-radius:4px}
pre.code{background:var(--code-bg);color:var(--code-fg);padding:.8rem 1rem;
 border-radius:8px;overflow-x:auto;font-size:13px;line-height:1.55}
pre.code code{background:none;padding:0;color:inherit;font-size:inherit}
.t-comment{color:#7d8799;font-style:italic}
.t-string{color:#9ece6a}
.t-flag{color:#e0af68}
.t-command{color:#7aa2f7;font-weight:600}
.t-path{color:#89ddff}
.t-tag{color:#f7768e}
.t-attr{color:#e0af68}
.t-hbs{color:#bb9af7;font-weight:600}
.t-key{color:#7aa2f7}
.t-number,.t-literal{color:#ff9e64}
.t-hunk{color:#7aa2f7;font-weight:600}
.t-add{color:#9ece6a}
.t-del{color:#f7768e}
table{border-collapse:collapse;width:100%;margin:1rem 0;font-size:14px;
 display:block;overflow-x:auto}
th,td{border:1px solid var(--line);padding:.4rem .6rem;text-align:left;
 vertical-align:top}
th{background:var(--card);font-size:12.5px;text-transform:uppercase;
 letter-spacing:.04em;color:var(--dim)}
blockquote{margin:1rem 0;padding:.6rem .9rem;border-left:3px solid var(--accent);
 background:var(--card);border-radius:0 6px 6px 0}
blockquote p{margin:0}
img,picture{max-width:100%;height:auto}
picture{display:block;margin:1.2rem 0}
ul,ol{padding-left:1.2rem}
li{margin:.25rem 0}
.hero{padding:1.5rem 0 .5rem;border-bottom:1px solid var(--line);
 margin-bottom:1.5rem}
.hero h1{font-size:2.4rem;margin:0 0 .2rem}
.hero .tagline{font-size:1rem;color:var(--accent);font-weight:600;
 margin:0 0 .8rem;letter-spacing:.01em}
.hero p{font-size:1.05rem;color:var(--dim);max-width:44rem}
.herolinks{margin-top:1.2rem;display:flex;gap:.6rem;flex-wrap:wrap}
.cta{display:inline-block;padding:.5rem 1.1rem;border-radius:7px;
 background:var(--accent);color:var(--bg);text-decoration:none;font-weight:600;
 font-size:14px}
.cta.ghost{background:var(--btn);color:var(--fg);
 border:1px solid var(--btn-edge)}
footer{border-top:1px solid var(--line);color:var(--dim);font-size:12.5px;
 padding:1.2rem 1.2rem 2.5rem;text-align:center}
footer p{margin:.4rem auto;max-width:46rem}
footer .disclaimer{font-size:11.5px}
"""

TEMPLATE = """<!doctype html>
<html lang="en">
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>%(title)s%(suffix)s</title>
<meta name="description" content="Work on a Terminal Four site from your own
 computer: pull the templates, edit them, see the result, push them back.">
<style>%(style)s</style>
<header>
  <b><a href="index.html" style="color:inherit;text-decoration:none">t4-sync</a></b>
  <span class="sub">T4 CMS Local Development Tool</span>
</header>
<div class="wrap">
  <nav class="side">%(nav)s</nav>
  <main>%(body)s</main>
  %(toc)s
</div>
<footer>
  <p class="disclaimer">An independent, unofficial tool. Not produced,
  endorsed or supported by TERMINALFOUR Solutions Ltd, and not TERMINALFOUR
  software. &ldquo;T4&rdquo; and &ldquo;TERMINALFOUR&rdquo; are their
  trademarks, used here only to identify the software this works with.</p>
  <p>Copyright &copy; 2026 Trent Stromkins &middot; Apache-2.0 &middot;
  <a href="https://github.com/bmx269/t4-sync">github.com/bmx269/t4-sync</a></p>
</footer>
</html>
"""


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--out", default=str(ROOT / "_site"))
    args = parser.parse_args()
    written = build(args.out)
    print("built %d page(s) into %s" % (len(written), args.out))
    for name in written:
        print("  %s.html" % name)


if __name__ == "__main__":
    main()
