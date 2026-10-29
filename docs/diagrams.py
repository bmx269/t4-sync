#!/usr/bin/env python3
"""Generate the README diagrams as SVG.

A generator rather than hand-written files: the diagrams describe how the
tool works, so they go stale when it changes, and editing raw SVG by hand is
how a box ends up one pixel off and nobody notices. Re-run this after
changing what the tool does.

    python3 docs/diagrams.py

Each diagram is written twice, light and dark, because GitHub renders
Markdown in both themes and a white card glares in one of them. The README
picks between them with <picture media="(prefers-color-scheme: dark)">.

Standard library only, like the rest of the project.
"""
import pathlib
import xml.etree.ElementTree as ET

HERE = pathlib.Path(__file__).resolve().parent

# Two palettes, same shapes. Taken from the app's own tokens so a diagram of
# the panel looks like the panel.
PALETTES = {
    "light": dict(bg="#ffffff", card="#f4f6fa", edge="#c3cbdb", fg="#1a1d25",
                  dim="#5a6376", accent="#2b4bbf", accent_soft="#e6ebfa",
                  warn="#9a3412", ok="#17603a", ok_soft="#e6f7ec",
                  code_bg="#0b0d12", code_fg="#dbe1ee"),
    "dark": dict(bg="#0d1017", card="#191c24", edge="#3b4455", fg="#e8eaf0",
                 dim="#99a1b3", accent="#9fb4ff", accent_soft="#1d2440",
                 warn="#fca5a5", ok="#8ff0a6", ok_soft="#17281d",
                 code_bg="#05070b", code_fg="#dbe1ee"),
}

SANS = "system-ui,-apple-system,'Segoe UI',Roboto,sans-serif"
MONO = "ui-monospace,SFMono-Regular,Menlo,Consolas,monospace"


class Canvas:
    """A very small SVG writer: boxes, text, arrows."""

    def __init__(self, width, height, palette):
        self.w, self.h = width, height
        self.p = palette
        self.parts = []

    def box(self, x, y, w, h, fill=None, stroke=None, radius=8, dash=None,
            width=1.5):
        attrs = ['x="%g" y="%g" width="%g" height="%g" rx="%g"' % (x, y, w, h, radius),
                 'fill="%s"' % (fill or self.p["card"]),
                 'stroke="%s"' % (stroke or self.p["edge"]),
                 'stroke-width="%g"' % width]
        if dash:
            attrs.append('stroke-dasharray="%s"' % dash)
        self.parts.append("<rect %s/>" % " ".join(attrs))

    def text(self, x, y, value, size=13, fill=None, weight="400", anchor="start",
             mono=False, opacity=None):
        attrs = ['x="%g" y="%g"' % (x, y),
                 'font-family="%s"' % (MONO if mono else SANS),
                 'font-size="%g"' % size,
                 'font-weight="%s"' % weight,
                 'fill="%s"' % (fill or self.p["fg"]),
                 'text-anchor="%s"' % anchor]
        if opacity is not None:
            attrs.append('opacity="%g"' % opacity)
        self.parts.append("<text %s>%s</text>" % (" ".join(attrs), _esc(value)))

    def runs(self, x, y, pieces, size=13, mono=False):
        """One line of text whose parts are coloured differently.

        tspans rather than separate <text> elements positioned by guessing
        character widths: the browser does the advancing, so the pieces
        cannot drift apart or overlap at a different font.
        """
        spans = "".join('<tspan fill="%s">%s</tspan>' % (colour, _esc(text))
                        for text, colour in pieces)
        self.parts.append(
            '<text x="%g" y="%g" font-family="%s" font-size="%g" '
            'xml:space="preserve">%s</text>'
            % (x, y, MONO if mono else SANS, size, spans))

    def line(self, x1, y1, x2, y2, stroke=None, width=1.5, dash=None):
        attrs = ['x1="%g" y1="%g" x2="%g" y2="%g"' % (x1, y1, x2, y2),
                 'stroke="%s"' % (stroke or self.p["edge"]),
                 'stroke-width="%g"' % width, 'stroke-linecap="round"']
        if dash:
            attrs.append('stroke-dasharray="%s"' % dash)
        self.parts.append("<line %s/>" % " ".join(attrs))

    def arrow(self, x1, y1, x2, y2, stroke=None, label=None, label_side="right"):
        colour = stroke or self.p["accent"]
        self.line(x1, y1, x2, y2, stroke=colour, width=2)
        self.parts.append(
            '<path d="M%g %g l-5 -9 l10 0 z" fill="%s" transform="rotate(%g %g %g)"/>'
            % (x2, y2 + 4, colour, 180 if y2 > y1 else 0, x2, y2))
        if label:
            mid = (y1 + y2) / 2
            self.text(x1 + (8 if label_side == "right" else -8), mid,
                      label, size=11, fill=colour,
                      anchor="start" if label_side == "right" else "end")

    def render(self):
        return (
            '<svg xmlns="http://www.w3.org/2000/svg" width="%g" height="%g" '
            'viewBox="0 0 %g %g" role="img">\n'
            '<rect width="%g" height="%g" fill="%s"/>\n%s\n</svg>\n'
            % (self.w, self.h, self.w, self.h, self.w, self.h,
               self.p["bg"], "\n".join(self.parts)))


def _esc(value):
    return (str(value).replace("&", "&amp;").replace("<", "&lt;")
            .replace(">", "&gt;"))


def overview(p):
    """Where things live, and what moves between them."""
    c = Canvas(880, 440, p)

    c.box(40, 30, 330, 92, fill=p["accent_soft"], stroke=p["accent"])
    c.text(56, 56, "TERMINAL FOUR", size=11, weight="700", fill=p["accent"])
    c.text(56, 78, "the CMS - where your templates live", size=12.5)
    c.text(56, 98, "myweb-test.example.edu", size=11.5, fill=p["dim"], mono=True)

    c.box(510, 30, 330, 92, fill=p["ok_soft"], stroke=p["ok"])
    c.text(526, 56, "PUBLISHED SITE", size=11, weight="700", fill=p["ok"])
    c.text(526, 78, "the finished pages visitors see", size=12.5)
    c.text(526, 98, "webtest.example.edu", size=11.5, fill=p["dim"], mono=True)

    c.arrow(120, 130, 120, 170)
    c.text(120, 186, "t4 pull", size=11, weight="700", anchor="middle",
           fill=p["accent"], mono=True)
    c.text(120, 200, "bring templates down", size=10, anchor="middle",
           fill=p["dim"])

    c.arrow(292, 170, 292, 130)
    c.text(292, 186, "t4 push", size=11, weight="700", anchor="middle",
           fill=p["accent"], mono=True)
    c.text(292, 200, "send changes back", size=10, anchor="middle",
           fill=p["dim"])

    c.arrow(675, 130, 675, 170, stroke=p["ok"])
    c.text(675, 186, "t4 mirror", size=11, weight="700", anchor="middle",
           fill=p["ok"], mono=True)
    c.text(675, 200, "copy the pages (one way)", size=10, anchor="middle",
           fill=p["dim"])

    c.box(40, 214, 800, 196, width=2)
    c.text(62, 244, "YOUR COMPUTER", size=11, weight="700", fill=p["dim"])

    c.box(62, 260, 360, 60, fill=p["bg"])
    c.text(78, 284, "t4-source/", size=12.5, weight="600", mono=True)
    c.text(78, 304, "the templates - these are what you edit", size=11.5,
           fill=p["dim"])

    c.box(458, 260, 360, 60, fill=p["bg"])
    c.text(474, 284, "site-mirror/", size=12.5, weight="600", mono=True)
    c.text(474, 304, "a copy of the finished pages", size=11.5, fill=p["dim"])

    c.box(62, 336, 756, 56, fill=p["accent_soft"], stroke=p["accent"])
    c.text(78, 360, "t4 serve", size=12.5, weight="700", mono=True,
           fill=p["accent"])
    c.text(148, 360, "http://127.0.0.1:8321", size=12, mono=True)
    c.text(78, 380, "the two combined: real pages, with your edits applied "
                    "as you save", size=11.5, fill=p["dim"])
    return c


def workflow(p):
    """A day's work, left to right."""
    c = Canvas(880, 190, p)
    steps = [
        ("t4 sync", "get what\nchanged", p["accent"]),
        ("edit", "in your\neditor", p["fg"]),
        ("t4 serve", "see it\nimmediately", p["accent"]),
        ("t4 save", "record it\nin git", p["fg"]),
        ("t4 push", "send it\nto T4", p["ok"]),
    ]
    x, w, gap = 34, 142, 29
    for index, (title, detail, colour) in enumerate(steps):
        c.box(x, 48, w, 92, fill=p["card"],
              stroke=colour if colour != p["fg"] else p["edge"])
        c.text(x + w / 2, 80, title, size=13, weight="700", anchor="middle",
               mono=True, fill=colour)
        for line_no, line in enumerate(detail.split("\n")):
            c.text(x + w / 2, 104 + line_no * 16, line, size=11.5,
                   anchor="middle", fill=p["dim"])
        if index < len(steps) - 1:
            nx = x + w
            c.line(nx + 7, 94, nx + gap - 7, 94, stroke=p["edge"], width=2)
            c.parts.append('<path d="M%g %g l-7 -5 l0 10 z" fill="%s"/>'
                           % (nx + gap - 5, 94, p["edge"]))
        x += w + gap
    c.text(34, 32, "A DAY'S WORK", size=11, weight="700", fill=p["dim"])
    c.text(34, 168, "Only the first and last need the CMS reachable. "
                    "Everything between is on disk.", size=11.5, fill=p["dim"])
    return c


def setup(p):
    """The two things that get set up, and how they differ."""
    c = Canvas(880, 230, p)

    c.box(40, 46, 380, 150, fill=p["accent_soft"], stroke=p["accent"])
    c.text(62, 76, "THE TOOL", size=11, weight="700", fill=p["accent"])
    c.text(62, 100, "installed once per computer", size=12.5)
    c.text(62, 126, "~/Sites/t4-sync/", size=12, mono=True, fill=p["dim"])
    c.line(62, 142, 398, 142, dash="3 4")
    c.text(62, 164, "gives you the `t4` command,", size=11.5, fill=p["dim"])
    c.text(62, 182, "which serves every project", size=11.5, fill=p["dim"])

    c.box(460, 46, 380, 150)
    c.text(482, 76, "A PROJECT", size=11, weight="700", fill=p["dim"])
    c.text(482, 100, "one per website", size=12.5)
    c.text(482, 126, "~/Sites/MySite/", size=12, mono=True, fill=p["dim"])
    c.line(482, 142, 818, 142, dash="3 4")
    c.text(482, 164, "that site's templates, settings,", size=11.5,
           fill=p["dim"])
    c.text(482, 182, "history and credentials", size=11.5, fill=p["dim"])

    c.text(440, 126, "+", size=20, weight="700", anchor="middle",
           fill=p["dim"])
    c.text(40, 220, "Installing the tool does not create a project. "
                    "`t4 init` does, in whatever folder you run it from.",
           size=11.5, fill=p["dim"])
    return c


def app_panel(p):
    """What the control panel shows, drawn from the running thing."""
    c = Canvas(880, 690, p)
    c.box(16, 16, 848, 658, fill=p["bg"], width=2)

    # Header
    c.box(16, 16, 848, 40, fill=p["card"])
    c.text(34, 42, "t4-sync", size=13, weight="700")
    c.text(96, 42, "Environment", size=11, fill=p["dim"])
    c.box(170, 25, 190, 22, fill=p["bg"], radius=5)
    c.text(180, 41, "TEST (cms.example.edu)", size=10.5, mono=True)
    c.text(352, 41, "\u25be", size=9, fill=p["dim"])
    c.box(372, 26, 62, 20, fill=p["bg"], radius=999)
    c.text(403, 40, "writable", size=9.5, anchor="middle", fill=p["warn"])
    c.box(782, 25, 66, 22, fill=p["btn"] if "btn" in p else p["card"], radius=5)
    c.text(815, 40, "Refresh", size=10.5, anchor="middle")

    # Project and the three stages
    c.box(28, 68, 824, 148)
    c.text(44, 92, "\u25b8", size=9, fill=p["dim"])
    c.text(58, 92, "Project", size=12, weight="700")
    c.text(110, 92, "753 files  \u00b7  768 pages mirrored  \u00b7  main at "
                    "a1b2c3d, clean", size=10.5, fill=p["dim"], mono=True)

    columns = [
        ("1 \u00b7 GET THE FILES",
         ["Pull source", "Mirror site", "Map sections", "Sync"]),
        ("2 \u00b7 WORK ON IT",
         ["Start local site", "View site", "Status", "What differs"]),
        ("3 \u00b7 SHIP IT", ["Commit to git", "Push to T4"]),
    ]
    cx = 44
    for title, buttons in columns:
        c.text(cx, 116, title, size=9, weight="700", fill=p["dim"])
        by = 126
        for label in buttons:
            primary = label == "Push to T4"
            disabled = label == "View site"
            c.box(cx, by, 196, 20,
                  fill=p["accent"] if primary else p["card"],
                  stroke=p["accent"] if primary else p["edge"], radius=5,
                  dash="3 3" if disabled else None)
            c.text(cx + 10, by + 14, label, size=10.5,
                   fill=p["bg"] if primary else
                   (p["dim"] if disabled else p["fg"]))
            c.box(cx + 202, by, 20, 20, fill=p["card"], radius=5)
            c.text(cx + 212, by + 14, "?", size=10, anchor="middle",
                   fill=p["dim"], weight="700")
            by += 24
        cx += 270

    # Changes, and what happened, side by side
    c.box(28, 228, 548, 178)
    c.text(44, 250, "CHANGES", size=10, weight="700", fill=p["dim"])
    c.box(104, 240, 76, 16, fill=p["accent_soft"], radius=999)
    c.text(142, 252, "3 CHANGED", size=8.5, anchor="middle", fill=p["accent"],
           weight="700")
    c.box(498, 240, 62, 18, fill=p["card"], radius=5)
    c.text(529, 253, "Rescan", size=9.5, anchor="middle")
    c.text(44, 272, "Files you have edited since the last pull. diff opens "
                    "what would change;", size=9.5, fill=p["dim"])
    c.text(44, 286, "push deploys that one file.", size=9.5, fill=p["dim"])

    rows = [("media/site.css", "media #4120"),
            ("pageLayout/standard.footer.html", "pageLayout #1180"),
            ("contentLayout/image-card--texthtml.html", "layout #4210")]
    ry = 300
    for path, record in rows:
        c.box(44, ry + 3, 12, 12, fill=p["accent"], stroke=p["accent"], radius=3)
        c.text(64, ry + 13, path, size=10, mono=True)
        c.text(408, ry + 13, record, size=9, fill=p["dim"], anchor="end")
        c.box(418, ry + 2, 48, 16, fill=p["card"], radius=4)
        c.text(442, ry + 14, "\u203a diff", size=9, anchor="middle")
        c.box(472, ry + 2, 44, 16, fill=p["card"], radius=4)
        c.text(494, ry + 14, "push", size=9, anchor="middle", fill=p["accent"])
        ry += 26

    c.box(588, 228, 264, 178)
    c.text(604, 250, "HISTORY", size=10, weight="700", fill=p["dim"])
    hy = 274
    for label, when, detail in [
            ("Pulled", "2 hours ago", None),
            ("Pushed", "yesterday", "4 files"),
            ("Committed", "7 hours ago", "a1b2c3d - 4 files"),
            ("Edited", "20 minutes ago", "media/site.css"),
            ("Mapped", "34 minutes ago", "2801 sections")]:
        c.text(604, hy, label, size=10, fill=p["dim"])
        c.text(836, hy, when, size=10, anchor="end")
        if detail:
            c.text(836, hy + 12, detail, size=8.5, anchor="end", fill=p["dim"],
                   mono=True)
            hy += 26
        else:
            hy += 20

    # Tag builder
    c.box(28, 418, 824, 190)
    c.text(44, 440, "TAG BUILDER", size=10, weight="700", fill=p["dim"])
    c.box(126, 430, 186, 16, fill=p["accent_soft"], radius=999)
    c.text(219, 442, "48 CONTENT TYPES, 1920 TAGS READ", size=8,
           anchor="middle", fill=p["accent"], weight="700")
    c.text(44, 462, "Builds a T4 tag from this project's own content types. "
                    "Composes only \u2014 T4 evaluates", size=9.5, fill=p["dim"])
    c.text(44, 475, "tags at publish time, nothing local can.", size=9.5,
           fill=p["dim"])

    fields = [("Tag", "content"), ("Content type", "Staff profile"),
              ("Element", "Job title \u00b7 plain text"), ("Output", "normal (1426)")]
    fx = 44
    for label, value in fields:
        c.text(fx, 494, label, size=8.5, fill=p["dim"])
        c.box(fx, 500, 186, 20, fill=p["bg"], radius=5)
        c.text(fx + 8, 514, value, size=9.5)
        c.text(fx + 174, 514, "\u25be", size=8, fill=p["dim"])
        fx += 196

    mx = 44
    for label in ("htmlentities (489)", "striptags (413)", "nl2br (63)"):
        c.box(mx, 532, 11, 11, fill=p["bg"], radius=3)
        c.text(mx + 17, 542, label, size=9, fill=p["dim"])
        mx += 150

    c.text(44, 566, "T4 tag", size=8.5, fill=p["dim"])
    c.box(786, 556, 52, 16, fill=p["card"], radius=4)
    c.text(812, 568, "copy", size=9, anchor="middle")
    c.box(44, 572, 794, 24, fill=p["code_bg"], stroke=p["code_bg"], radius=5)
    # Coloured the way the panel colours it, as one line of tspans.
    c.runs(56, 588, [("<t4 ", "#f7768e"), ("type", "#e0af68"),
                     ("=", p["code_fg"]), ('"content"', "#9ece6a"),
                     (" name", "#e0af68"), ("=", p["code_fg"]),
                     ('"Job title"', "#9ece6a"), (" output", "#e0af68"),
                     ("=", p["code_fg"]), ('"normal"', "#9ece6a"),
                     (" />", "#f7768e")], size=9.5, mono=True)

    c.box(28, 620, 824, 40)
    c.text(44, 642, "TASKS", size=10, weight="700", fill=p["dim"])
    c.text(96, 642, "Nothing running.", size=10, fill=p["dim"])
    return c


def inspector(p):
    """Click something on the page; the panel says what made it."""
    c = Canvas(880, 350, p)

    c.box(30, 56, 390, 262, fill=p["bg"], width=2)
    c.text(46, 44, "THE PAGE YOU ARE LOOKING AT", size=10.5, weight="700",
           fill=p["dim"])
    c.box(52, 80, 346, 30, fill=p["card"], radius=5)
    c.text(68, 100, "Home   About   Programs   Contact", size=11.5,
           fill=p["dim"])
    c.box(52, 124, 346, 120, fill=p["card"], stroke=p["accent"], width=2.5,
          dash="5 4")
    c.text(68, 146, "Staff profile . A. Example", size=10.5, weight="700",
           fill=p["accent"])
    c.box(68, 156, 60, 60, fill=p["bg"], radius=4)
    c.text(98, 191, "photo", size=10, anchor="middle", fill=p["dim"])
    c.text(144, 178, "A. Example", size=12.5, weight="600")
    c.text(144, 196, "Job title", size=11.5, fill=p["dim"])
    c.text(52, 272, "click any element", size=11.5, fill=p["accent"],
           weight="600")

    c.line(400, 190, 456, 190, stroke=p["accent"], width=2)
    c.parts.append('<path d="M%g %g l-8 -6 l0 12 z" fill="%s"/>'
                   % (458, 190, p["accent"]))

    c.box(466, 56, 384, 262, fill=p["card"], width=2)
    c.text(482, 44, "THE T4 PANEL", size=10.5, weight="700", fill=p["dim"])
    c.box(486, 76, 220, 22, fill=p["bg"], radius=5)
    c.text(496, 91, "Highlight: content items", size=11)
    c.box(486, 104, 220, 22, fill=p["bg"], radius=5)
    c.text(496, 119, "Select on click  (links off)", size=11)

    c.text(486, 150, "Selected", size=11, weight="700", fill=p["dim"])
    c.parts.append('<circle cx="492" cy="172" r="5" fill="%s"/>' % p["accent"])
    c.text(506, 176, "Staff profile . A. Example", size=11.5, weight="600")
    c.text(506, 194, "contentLayout/staff-profile--texthtml.html", size=10.5,
           mono=True, fill=p["dim"])
    for index, label in enumerate(("edit", "folder", "copy")):
        bx = 506 + index * 62
        c.box(bx, 202, 54, 20, fill=p["bg"], radius=5)
        c.text(bx + 27, 216, label, size=10, anchor="middle")

    c.parts.append('<circle cx="492" cy="246" r="5" fill="%s"/>' % p["dim"])
    c.text(506, 250, "page layout standard", size=11.5, weight="600")
    c.text(506, 268, "pageLayout/standard.header.html", size=10.5,
           mono=True, fill=p["dim"])
    for index, label in enumerate(("edit", "folder", "copy")):
        bx = 506 + index * 62
        c.box(bx, 276, 54, 20, fill=p["bg"], radius=5)
        c.text(bx + 27, 290, label, size=10, anchor="middle")
    return c


DIAGRAMS = {"overview": overview, "setup": setup, "workflow": workflow,
            "app": app_panel, "inspector": inspector}


def main():
    out = HERE / "img"
    out.mkdir(parents=True, exist_ok=True)
    for name, build in DIAGRAMS.items():
        for theme, palette in PALETTES.items():
            markup = build(palette).render()
            ET.fromstring(markup)          # refuse to write malformed SVG
            path = out / ("%s-%s.svg" % (name, theme))
            path.write_text(markup, encoding="utf-8")
            print("%-28s %5d bytes" % (path.relative_to(HERE.parent), len(markup)))


if __name__ == "__main__":
    main()
