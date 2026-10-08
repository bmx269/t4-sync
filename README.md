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

### Local copy of the site

```sh
t4 mirror                   # uses the environment's published_url
t4 mirror https://webtest.example.edu/
t4 serve                    # http://127.0.0.1:8321/
```

The mirror saves files **verbatim**. They are byte-identical to what T4
published, so markup copied out of them pastes straight back into a layout.
URL rewriting happens at serve time instead, from `.t4/rewrites.conf`:

```
# <regex><TAB><replacement>
https?://www\.example\.edu/	/
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

All verified against a live T4 8.4.3 instance.

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
