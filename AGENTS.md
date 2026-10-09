# t4-sync — agent notes

A cross-platform CLI for Terminal Four. Read [README.md](README.md) first for
what the commands do; this file covers how to work on the tool itself.

**This repo is client-neutral and must stay that way.** It is a general T4
tool. No client hostnames, layout names, section names, content or data —
examples use `example.edu`. The tool was extracted from a client engagement,
so the pull is toward leaking specifics back in. Before committing:

```sh
git grep -niE '<your client names>'
```

Client-specific configuration lives in the *consuming project's*
`.t4/config.json`, never here.

## Ground rules

- **Standard library only.** No runtime dependencies, deliberately. The tool
  installs on machines behind a VPN where package resolution is unreliable, and
  a dependency list is a support burden for a tool teammates install once. If
  something seems to need a package, it almost certainly does not.
- **Python 3.8+.** CI enforces it. Avoid walrus-in-comprehension, `match`,
  `dict |` merges, and `X | Y` type syntax.
- **Cross-platform.** CI runs Linux, macOS and Windows. Use `pathlib`, never
  assume `/` in paths, never shell out to `wget`, `curl`, `ln` or `chmod` and
  expect them to exist. `os.chmod` is a no-op on Windows and that is fine.
- **Read-only by default.** `pull`, `sync`, `diff`, `doctor` and `mirror` only
  ever GET. `push` is the single writer.

## Layout

```
src/t4sync/
  cli.py          argparse subcommands; the only entry point
  config.py       Project discovery (.t4/config.json), tokens
  api.py          HTTP client, retries, error description
  endpoints.py    WHAT to pull and WHERE source lives in each record
  extract.py      records -> files, hashing, local-edit protection
  compare.py      local vs remote
  mirror.py       published-site crawler (stdlib, no wget)
  serve.py        local server: overrides, rewrites, origin proxy
  commands/       one module per subcommand
tests/            no network, no T4 instance required
```

## Things that are deliberate — do not "fix" them

**Endpoint names in `endpoints.py` are case-sensitive and inconsistent.**
`/pageLayout` is camelCase, `/contenttype` is not. These are verified against a
live instance. A wrong case returns **500**, which is exactly what an unrouted
path returns, so a well-meaning tidy-up produces an error that looks like "this
endpoint does not exist". This has already caused one wrong diagnosis.

**`/list` uses `list/1` where the `1` is ignored.** `/list` alone returns 500;
`/list/<anything>` returns every list. It is a required placeholder, not an id.

**All file IO uses `newline=""`.** T4 stores CRLF. Python's universal-newline
translation would rewrite it, making every CRLF file look modified and causing
`push` to send back mangled line endings.

**Mirrored files are never rewritten.** They must stay byte-identical to what
T4 published so markup can be pasted back into a layout. URL rewriting happens
at serve time, from `.t4/rewrites.conf`.

**Pull records a hash per file.** That is the only way to distinguish "the user
edited this" from "the remote changed", and it is what lets pull refuse to
clobber local work. Do not drop `sha` from the manifest.

**Retries cover transport failures only, never HTTP statuses.** A 4xx/5xx is an
answer; repeating it wastes time. A dropped TLS handshake is not.

## Testing

```sh
python3 -m unittest discover -s tests
```

Tests must not require a network or a T4 instance — CI has neither. For
anything needing a live instance, extend `t4 doctor` instead, which is the
tool's own self-check and is run by a human against their own CMS.

## Verified against

T4 **8.4.3**, live. 8.4.4 and 8.4.5 release notes record no changes to the
endpoints used here, but are untested directly. `t4 doctor` is the way to
confirm against any instance — it checks that the source fields still exist on
a detail record, because a DTO change would otherwise show up as a silently
empty pull rather than an error.

Known gap: **Content Layouts are not exposed by the REST API** on 8.4.3. About
30 route spellings were tried. Page Layouts, Content Types, Navigation, Lists
and Channels all work. A Package export is the workaround; `unpack-package.py`
from the old script set has not yet been ported into the CLI.

## Releasing

`main` is the only branch. CI must be green before pushing. There are no tags
or published packages yet; users install from a clone and update with
`git pull` plus a re-run of the installer.
