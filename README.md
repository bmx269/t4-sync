# t4-sync

A command-line tool for Terminal Four. Pull layout source out of the CMS onto
disk, review changes, push them back, and run a local copy of the published
site.

Standard library only — no dependencies beyond Python 3.8+.

```sh
t4 init                                   # set up this project
t4 env add test https://myweb-test.example.edu
t4 token set test                         # paste a token; input hidden
t4 pull                                   # layout source onto disk
t4 sync                                   # pull, then report what moved
t4 diff --show                            # local vs T4
t4 push                                   # per-file confirmation
t4 mirror && t4 serve                     # local copy of the published site
```

## Install

Requires Python 3.8+ and git. Clone, then run the installer for your platform.

```sh
git clone https://github.com/bmx269/t4-sync.git
cd t4-sync
./install.sh            # macOS, Linux
```

```powershell
git clone https://github.com/bmx269/t4-sync.git
cd t4-sync
.\install.ps1           # Windows
```

Both create a virtualenv in the checkout and put `t4` on your PATH
(`~/.local/bin` by default; `--prefix` / `-Prefix` to change it). If that
directory is not on your PATH, the installer tells you how to add it.

To update: `git pull` then re-run the installer.

## How it is organised

A **project** is any directory containing `.t4/config.json`, found by walking
up from the working directory — the way git finds `.git`. The tool is generic;
the configuration, the pulled source and the site mirror belong to the project.

```
your-project/
  .t4/
    config.json          environments and settings — commit this
    tokens/              API tokens — never committed, 0600
    rewrites.conf        serve-time URL rewrites
  t4-source/
    <env>/
      _manifest.json     file -> endpoint/id/field/hash, needed by push
      _raw/              every API response verbatim
      _backup/<stamp>/   pre-push snapshots
      pageLayout/        name.header.html, .footer.html, .stylesheet.css
      contenttype/ navigation/ list/ channel/
  site-mirror/<host>/    published site
  overrides/             shadows the mirror when serving
```

`t4 init` writes a `.gitignore` block covering tokens, raw responses, backups
and the mirror.

## Commands

### Pull and push

```sh
t4 pull                     # the default environment
t4 pull --env prod
t4 pull --all               # every environment that has a token
t4 pull --only pageLayout
t4 pull --no-detail         # metadata only: fast, no source
t4 pull --force             # overwrite files you have edited locally
```

A pull **will not overwrite a file you have edited** since the last pull. The
manifest records a hash of every file as pulled, which is what distinguishes
"you changed this" from "the remote changed". Protected files are listed at the
end of the run; `--force` discards them.

```sh
t4 sync                     # pull, then report both sides
t4 diff [paths...] [--show]
t4 push [paths...] [--dry-run]
```

`sync` is the daily command: it refreshes, then tells you what changed in T4,
what is new or gone, and what you have edited locally but not pushed. It never
writes to T4.

`push` offers only files that genuinely differ, shows a diff, and asks per file
(`y`/`N`/`q`). Every record is snapshotted to `_backup/` before being modified.
Pushing is **disabled per environment by default** — set `"push_allowed": true`
in `.t4/config.json` for the environments you want writable, and leave
production off unless you mean it.

### The editing loop

```sh
t4 serve                              # start the local site
t4 edit media/css/site.css            # copy a file in to edit it
# edit overrides/media/css/site.css — the browser refreshes on save
t4 status                             # what is pending, both file sets
```

`t4 edit` copies a mirrored file into `overrides/` at the right path, so you
do not have to reproduce a deep path by hand. `--list` shows what is
overridden, `--revert` drops one, and a wrong path suggests near matches.

`t4 serve` watches `overrides/` and refreshes the browser on save, via an
injected server-sent-events listener. `--no-reload` turns it off.

### Finding the source file

```sh
t4 layouts                    every published layout, its pages, its local files
t4 layouts about/admissions   just that page
```

`t4 serve` also reports it per page, in two forms — an overlay and
Drupal-style theme-debug comments in the served HTML:

```html
<!-- T4 DEBUG -->
<!-- PAGE LAYOUT: 'm_open' #1027039 -->
<!-- SOURCE FILES:
   x t4-source/test/pageLayout/m-open-2.header.html
   x t4-source/test/pageLayout/m-open-2.footer.html
-->
<!-- ASSETS (23, 1 overridden):
   x media/web/css/brand.css
   * media/web/css/site.css
   * not overridden - copy it in with: t4 edit <path>
-->
<!-- CHILD PAGES (25):
   u_webpage: admissions, advising, aging, agriculture, ...
   m_open: arts, alumni-weekend
-->
```

`--inspect comments` for comments only, `panel` for the overlay only, `both`
(the default), `off` for neither. The comments are added at serve time; files
under the mirror are never modified.

#### Components marked where they start

T4 does not delimit content items in published output. Many sites do comment
them from their own content layouts, and where that convention exists it can be
read back, so each component is marked at the point it begins:

```html
<!-- ct:322 Contact Footer --><!-- T4 COMPONENT: 'Contact footer' -> t4-source/test/contenttype/contact-footer.json -->
<div class="row ct-322" id="contact-bar">
```

Navigation objects too:

```html
<!-- n:Footer-Contact (273) --><!-- T4 NAVIGATION: 'Footer-Contact' -> t4-source/test/navigation/footer-contact-273.json -->
```

The site's own markers are left in place; the note is added after them. A
marker whose id is not in the local pull says so rather than pointing at the
wrong file.

**These conventions are per-site, not a T4 feature.** The defaults recognise
`<!-- ct:ID Name -->`, `<!-- ct-ID Name -->`, `<!-- content type: Name -->` and
`<!-- n:Name (ID) -->`. Set `component_markers` in `.t4/config.json` for other
conventions — a list of `[regex, endpoint]` pairs, where the regex has an `id`
group, a `name` group, or both:

```json
{
  "component_markers": [
    ["<!--\\s*component:(?P<name>[^>]+?)\\s*-->", "contenttype"]
  ]
}
```

A content layout's *template* still cannot be linked, because the REST API does
not expose Content Layouts at all — the link is to the content type's
definition.

#### Exact resolution

```sh
t4 sections --root <site-root-id>     # walk the tree, record each assignment
```

T4 knows exactly which layout each section uses — every section carries, per
channel, `{"pageLayout": 1100982, "inheritedPageLayout": 1100998}`. With that
pulled, the inspector reports the real assignment instead of inferring it:

```html
<!-- NOTE: assigned in T4 to section 228911 (Applied Business Technology) -->
<!-- SOURCE FILES:
   x t4-source/test/pageLayout/u-webpage.production.header.html
-->
```

Two requests per section, so it is a separate, opt-in pull. It is resumable:
`--limit` stops early and re-running continues from the cache. Without it, the
inference below is used instead.

#### How the layout is identified when there is no section map

T4 publishes a layout *name* into nearly every page, but the name need not
identify one layout: a site can publish `u_webpage` from any of several
`u_webpage.*` layouts. Rather than asking for a mapping, the layout is
identified from the page itself:

| Signal | What it does |
|---|---|
| `t4-layout-id` | Exact, when the page carries it (often it does not) |
| **Alignment** | A layout's literal markup must appear, in order, in the page. One that did not produce the page usually fails outright |
| **Tag ids** | Among survivors, score the ids that *differ* between them. T4 layouts commonly echo media and navigation ids into the output |

On a real site this resolved 297 of 300 pages; the other three carry no meta
tag at all. Where two layouts are genuinely indistinguishable -- identical
literals, identical tags -- it says so instead of picking one:

```html
<!-- NOTE: indistinguishable from the published page: u_webpage.a, u_webpage.b -->
```

### The inspector

`t4 serve` injects a small **T4** badge in the corner of every page. Expand it
and it answers "what rendered this, and what do I edit":

```
Page layout   m_open  #1027039
              pageLayout/m-open-2.header.html      [edit]
              pageLayout/m-open-2.footer.html      [edit]
Assets        23, 1 overridden
              media/web/css/site.css           [edit]
              media/web/css/brand.css        [editing]
              ...
```

T4 publishes `<meta name="t4-layout">` and `<meta name="t4-layout-id">` into
every page. The id is matched against the pull manifest, so the panel names the
exact file on disk rather than leaving you to guess which of 58 layouts with
names like `u_webpage.production` and `m_open_alt_no_menu` produced the page.
Layout *names* are not reliably unique after slugging; the id is.

Each row's **edit** button copies the matching `t4 edit …` command. Assets
already overridden are highlighted and read **editing**.

**Highlight components** outlines every content item and navigation object on
the page, coloured by type, with a legend. The colour is derived from the type
name, so the same component is the same colour on every page. The setting is
remembered per browser.

It works from the marker comments already in the DOM — no markup is injected
into the page, which would risk landing somewhere invalid like `<head>` or
inside a `<table>` and changing how the page renders. A component that resolves
to an element with no height climbs to the nearest ancestor that occupies
space, and two components sharing one element have their labels combined rather
than one overwriting the other.

If a page has no `t4-layout` meta, or the layout has not been pulled yet, the
panel says so rather than showing nothing. `--no-inspect` turns it off.

**Two file sets, two deploy paths.** This is the thing to understand:

| Edited in | Holds | Deployed by |
|---|---|---|
| `overrides/` | Published assets: CSS, JS, images | **The T4 UI.** These are Media Library items and the REST API exposes no media endpoint — about a dozen names and casings were tried, all 500. `t4 push` cannot deploy them. |
| `t4-source/` | Page layouts, content types, navigation | `t4 push` |

`t4 status` shows both together, because the easy mistake is pushing a layout
and forgetting the stylesheet it depends on.

The asymmetry is worth knowing early: CSS and JS can be developed locally with
instant feedback but deploy by hand, while layouts deploy automatically but
cannot be previewed locally at all (see *What this does not do*).

### Previewing a layout edit

Edits to a pulled page layout show up on the local site without a publish
cycle:

```sh
t4 serve
# edit t4-source/<env>/pageLayout/<name>.footer.html
# reload - the change is there
```

This is not rendering. T4 tags are evaluated by T4, and nothing local can run
them. But the mirrored page *is* the result of running them, so the two can be
aligned:

```
source:   <lit A>  <t4 .../>  <lit B>  <t4 .../>  <lit C>
output:   <lit A>  ...????..  <lit B>  ...????..  <lit C>
```

The literal markup appears verbatim in the published page, so whatever sits
between those anchors is what the tags produced. Capture it, then re-emit the
*edited* source with it substituted back. Wrapping a tag in a div, changing a
class, adding or reordering markup — all apply locally.

A no-op edit reproduces the page byte-for-byte; that invariant is tested.

Or as a diff, without a browser:

```sh
t4 preview                     # the home page
t4 preview about/admissions
t4 preview --out /tmp/x.html   # write the previewed HTML instead
```

```diff
index.html
  layout  m_open  #1027039
  edited  pageLayout/m-open-2.footer.html

--- published
+++ with local edits
-<!-- n:Footer-Contact (273) -->
+<div class="footer-contact-bar-ts"><!-- n:Footer-Contact (273) -->
```

This is not a substitute for T4's own Preview, which is authoritative. The
REST API exposes no preview or publish operation (checked on 8.4.3 and 8.4.5),
so seeing the rendered result still means previewing or publishing in T4.

#### What it will tell you it cannot do

**A tag with no published output** — a new tag, or an existing one pointed at a
different id — has never produced anything to reuse:

```html
<!-- T4 PREVIEW: <t4 type="navigation" id="99" /> has no published output to
     reuse, so it cannot be previewed -->
```

**Tags whose output cannot be told apart.** Where several tags sit together
with no literal markup between them, their output is one undivided run. If the
site's layouts bracket a tag's output with comments, that tag can be isolated
(the defaults recognise `<!-- n:Name (ID) -->…<!-- /n:ID -->` and the `ct:`
equivalent; set `span_markers` in `.t4/config.json` for other conventions).
Otherwise, placing markup around one tag of the run would silently wrap all of
them, so it says so:

```html
<!--   WARNING: markup was placed around tags whose individual output cannot be separated -->
<!--   affected: <t4 type="navigation" id="276" />, <t4 type="media" id="1094476" ... -->
<!--   what you see wraps the whole run, which is not what T4 will publish -->
```

That warning is the point. A preview that quietly differs from what T4 will
publish is worse than no preview.

`--no-preview` serves the mirror exactly as published.

### Checking the local copy is faithful

```sh
t4 compare                      # the home page
t4 compare about/admissions
t4 compare --local http://127.0.0.1:8331
```

Fetches the page from the published site and from the local server, removes
everything this tool injected, and diffs the two. The local copy is assembled
from a mirror, plus local edits, plus captured tag output -- several places for
a difference to creep in unnoticed.

Everything injected is fenced with `<!--T4SYNC:BEGIN-->` / `<!--T4SYNC:END-->`
so it can be removed exactly. Stripping by pattern does not work: the
annotations themselves contain `>` and `->`.

Differences are explained rather than reported as failures -- local layout
edits not yet published will show up, and so will content that changed in T4
since the mirror was taken.

### Checking an instance

```sh
t4 doctor                   # read-only probe of the configured environment
t4 doctor --env prod
```

Reports the T4 version, token expiry, and whether each endpoint responds —
including whether the fields the tool extracts source from are still present,
since a DTO change between releases would otherwise show up as a silently
empty pull rather than an error.

Run this first against any instance you have not used before. T4's REST
surface varies by version and deployment, so probing beats assuming.

### Local copy of the site

```sh
t4 mirror                   # uses the environment's published_url
t4 mirror https://webtest.example.edu/
t4 serve                    # http://127.0.0.1:8321/
```

The mirror saves files **verbatim**. They are byte-identical to what T4
published, so markup copied out of them pastes straight back into a layout.
URL rewriting happens at serve time instead.

**Rules for your own channels are derived automatically** from the
`published_url` of each configured environment. T4 writes absolute URLs into
published pages, and they usually point at production even in a test channel —
hosts that are typically unreachable from a workstation. Without rewriting them
the stylesheets never arrive and the page renders unstyled, which looks like a
rendering fault rather than a missing asset.

Add anything else to `.t4/rewrites.conf`; file rules are applied before the
derived ones:

```
# <regex><TAB><replacement>
https?://legacy\.example\.edu/	/
```

Documents (PDF, Office) are skipped by default; `--include-documents` keeps
them.

**Asset proxy.** Anything missing locally is fetched from the live site on
demand and cached, like `stage_file_proxy`. Images and documents dominate a
mirror's size while contributing nothing to front-end work, so you can mirror
pages, CSS and JS and still see a complete site.

```sh
t4 serve                        # proxies to the environment's published_url
t4 serve --proxy https://www.example.edu
t4 serve --no-proxy             # serve only what is mirrored
t4 serve --no-proxy-cache       # proxy without writing into the mirror
```

Files in `overrides/` shadow the mirror, so you can iterate on CSS and JS
against real published markup with no publish cycle. Responses are sent
`Cache-Control: no-store`, so a plain reload picks up edits.

## What this does not do

**It does not render T4 tags or Handlebars.** Nothing local can. `<t4 .../>`
tags, Handlebars expressions and Programmable Layouts are evaluated by T4's own
publish engine, in Java, against the CMS database. There is no standalone
renderer, and T4's helpers (`{{element}}`, `{{media}}`, `{{sectionName}}`, the
whole set) are proprietary to that engine.

So the two halves of this tool hold different things, and neither is a
substitute for the other:

| | What it holds | Tags in it |
|---|---|---|
| `t4 mirror` + `t4 serve` | Published **output** | Already evaluated — real HTML |
| `t4 pull` | Layout **source** | Unevaluated — `{{...}}` as literal text |

The practical consequence: a mirror is the right place to work on CSS, JS and
markup structure, because it is exactly what a visitor's browser receives. It
is the wrong place to test layout logic. Open a mirrored page and the template
is already resolved; open a pulled `.hbs` file in a browser and you see
`{{sectionName}}` printed on the page.

**Layout logic still has to be tested in T4** — Preview, Direct Edit, or a
staging channel. The loop this tool shortens is the front-end one: edit CSS or
JS in `overrides/`, reload, see it against real published markup, with no
publish cycle. The loop it does not shorten is changing a layout's logic.

What it does give you for layout work is version control, diffing and review:
you can see what changed in a layout, who changed it, and push a reviewed
change — you just cannot render it locally first.

## Things that will bite you

Verified against a live T4 **8.4.3** instance. 8.4.4 and 8.4.5 release notes
record no changes to the endpoints used here, but have not been tested
directly — run `t4 doctor` to confirm against yours.

**Some resources need a `{language}` path segment**, and omitting it returns
500 — the same answer as a route that does not exist. This hides whole
resources: Content Layouts are at `/layout/contenttype/{id}/{language}`, and
media at `/media/{id}/{language}`. Both looked absent for a long time because
they were requested without it.

**Endpoint names are case-sensitive and inconsistent.** `/pageLayout` is
camelCase; `/contenttype` is not. A wrong case returns **500** — identical to
an unrouted path — so a typo is indistinguishable from "this endpoint does not
exist". The verified set is in `src/t4sync/endpoints.py`. Do not tidy them.

**`/list` ignores its path segment.** `/list` alone returns 500;
`/list/<anything>` returns every list. The `1` in `list/1` is a placeholder.

**T4 stores CRLF.** All file IO uses `newline=""` to disable Python's
universal-newline translation. Without it every CRLF file looks modified and a
push would rewrite layouts with mangled line endings.

**Content Layouts are not exposed by the REST API.** About 30 route spellings
were tried on 8.4.3; all 500. Page Layouts, Content Types, Navigation and Lists
all work. Export a Package from the UI for Content Layouts.

**8.4.5 deprecates the XML Web Services payload** in favour of JSON. This tool
only ever speaks JSON, so that deprecation does not affect it. 8.4.5 also
reverts a Repeater API change made in 8.4.4, which may alter content type
responses between those two releases; records are written whole, so a field
change shows up as a diff rather than a failure.

**Tokens come from the UI.** Administration → User Management → generate an API
token. `/rs/authorise` returns 500 on at least one 8.4.3 instance, so tokens
cannot reliably be minted from a username and password.

**The network path to a CMS is often a VPN and often unreliable.** Transport
failures are retried with backoff; HTTP statuses are not, since the server
answered.

## Development

```sh
python3 -m unittest discover -s tests
```

The tests need no network and no T4 instance.

## Licence

Copyright © 2026 Trent Stromkins.

Apache License 2.0 — see [LICENSE](LICENSE) and [NOTICE](NOTICE).

Use it, change it, ship it, including commercially. Keep the copyright notice
and the NOTICE file, and say what you changed. The licence includes an express
patent grant from contributors.
