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
    c = Canvas(880, 430, p)

    c.box(40, 30, 330, 92, fill=p["accent_soft"], stroke=p["accent"])
    c.text(56, 56, "TERMINAL FOUR", size=11, weight="700", fill=p["accent"])
    c.text(56, 78, "the CMS - where your templates live", size=12.5)
    c.text(56, 98, "myweb-test.example.edu", size=11.5, fill=p["dim"], mono=True)

    c.box(510, 30, 330, 92, fill=p["ok_soft"], stroke=p["ok"])
    c.text(526, 56, "PUBLISHED SITE", size=11, weight="700", fill=p["ok"])
    c.text(526, 78, "the finished pages visitors see", size=12.5)
    c.text(526, 98, "webtest.example.edu", size=11.5, fill=p["dim"], mono=True)

    c.arrow(150, 128, 150, 196, label="t4 pull")
    c.text(158, 178, "bring templates down", size=10.5, fill=p["dim"])
    c.arrow(262, 196, 262, 128, label="t4 push", label_side="left")
    c.text(254, 178, "send changes back", size=10.5, fill=p["dim"], anchor="end")
    c.arrow(675, 128, 675, 196, stroke=p["ok"], label="t4 mirror")
    c.text(683, 178, "copy the pages (one way)", size=10.5, fill=p["dim"])

    c.box(40, 204, 800, 196, width=2)
    c.text(62, 234, "YOUR COMPUTER", size=11, weight="700", fill=p["dim"])

    c.box(62, 250, 360, 60, fill=p["bg"])
    c.text(78, 274, "t4-source/", size=12.5, weight="600", mono=True)
    c.text(78, 294, "the templates - these are what you edit", size=11.5,
           fill=p["dim"])

    c.box(458, 250, 360, 60, fill=p["bg"])
    c.text(474, 274, "site-mirror/", size=12.5, weight="600", mono=True)
    c.text(474, 294, "a copy of the finished pages", size=11.5, fill=p["dim"])

    c.box(62, 326, 756, 56, fill=p["accent_soft"], stroke=p["accent"])
    c.text(78, 350, "t4 serve", size=12.5, weight="700", mono=True,
           fill=p["accent"])
    c.text(148, 350, "http://127.0.0.1:8321", size=12, mono=True)
    c.text(78, 370, "the two combined: real pages, with your edits applied "
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
    """What the control panel shows."""
    c = Canvas(880, 470, p)
    c.box(20, 20, 840, 430, fill=p["bg"], width=2)

    c.box(20, 20, 840, 44, fill=p["card"], radius=8)
    c.text(40, 48, "t4-sync", size=13, weight="700")
    c.box(110, 30, 110, 24, fill=p["bg"], radius=6)
    c.text(122, 47, "TEST", size=11.5, mono=True)
    c.text(236, 48, "writable", size=11, fill=p["warn"])
    c.box(770, 30, 72, 24, fill=p["bg"], radius=6)
    c.text(806, 47, "Refresh", size=11, anchor="middle")

    c.box(36, 78, 808, 136)
    c.text(54, 102, "Project", size=12, weight="700")
    c.text(112, 102, "753 files  .  394 pages mirrored  .  main, clean",
           size=11.5, fill=p["dim"], mono=True)
    columns = [("GET THE FILES", ["Pull source", "Mirror site", "Map sections"]),
               ("WORK ON IT", ["Start local site", "View site", "Status"]),
               ("SHIP IT", ["Commit to git", "Push to T4"])]
    cx = 54
    for title, buttons in columns:
        c.text(cx, 128, title, size=9.5, weight="700", fill=p["dim"])
        by = 138
        for label in buttons:
            primary = label == "Push to T4"
            c.box(cx, by, 196, 22,
                  fill=p["accent"] if primary else p["card"],
                  stroke=p["accent"] if primary else p["edge"], radius=5)
            c.text(cx + 10, by + 15, label, size=11,
                   fill=p["bg"] if primary else p["fg"])
            c.box(cx + 202, by, 22, 22, fill=p["card"], radius=5)
            c.text(cx + 213, by + 15, "?", size=11, anchor="middle",
                   fill=p["dim"])
            by += 26
        cx += 262

    c.box(36, 226, 540, 150)
    c.text(54, 250, "Changes", size=12, weight="700")
    c.text(124, 250, "5 changed", size=11, fill=p["dim"])
    rows = [("media/ufv-v.css", "media #971148"),
            ("pageLayout/m-open.footer.html", "pageLayout #1027039")]
    ry = 272
    for path, record in rows:
        c.box(54, ry, 13, 13, fill=p["accent"], stroke=p["accent"], radius=3)
        c.text(76, ry + 11, path, size=11, mono=True)
        c.text(560, ry + 11, record, size=10, fill=p["dim"], anchor="end")
        ry += 24
    c.box(76, 320, 480, 44, fill=p["code_bg"], stroke=p["code_bg"], radius=5)
    c.text(90, 338, "@@ -14,7 +14,7 @@", size=10.5, mono=True, fill=p["dim"])
    c.text(90, 354, "- padding: 40px 0 20px;   + padding: 24px 0 16px;",
           size=10.5, mono=True, fill=p["code_fg"])

    c.box(588, 226, 256, 150)
    c.text(606, 250, "History", size=12, weight="700")
    hy = 274
    for label, when in [("Pulled", "2 hours ago"), ("Pushed", "yesterday"),
                        ("Committed", "yesterday"), ("Edited", "10 min ago")]:
        c.text(606, hy, label, size=11, fill=p["dim"])
        c.text(826, hy, when, size=11, anchor="end")
        hy += 22

    c.box(36, 388, 808, 48)
    c.text(54, 410, "Tasks", size=12, weight="700")
    c.parts.append('<circle cx="112" cy="406" r="4" fill="%s"/>' % p["accent"])
    c.text(124, 410, "t4 pull test", size=11, mono=True)
    c.text(214, 410, "running", size=10.5, fill=p["dim"])
    c.parts.append('<circle cx="300" cy="406" r="4" fill="%s"/>' % p["ok"])
    c.text(312, 410, "t4 save test", size=11, mono=True)
    c.text(404, 410, "done", size=10.5, fill=p["dim"])
    c.text(54, 428, "tasks run side by side - a pull does not stop you "
                    "committing", size=10.5, fill=p["dim"])
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
    c.text(68, 146, "Contact . Elaine Newman", size=10.5, weight="700",
           fill=p["accent"])
    c.box(68, 156, 60, 60, fill=p["bg"], radius=4)
    c.text(98, 191, "photo", size=10, anchor="middle", fill=p["dim"])
    c.text(144, 178, "Elaine Newman", size=12.5, weight="600")
    c.text(144, 196, "Academic Advisor", size=11.5, fill=p["dim"])
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
    c.text(506, 176, "Contact . Elaine Newman", size=11.5, weight="600")
    c.text(506, 194, "contentLayout/contact--texthtml.html", size=10.5,
           mono=True, fill=p["dim"])
    for index, label in enumerate(("edit", "folder", "copy")):
        bx = 506 + index * 62
        c.box(bx, 202, 54, 20, fill=p["bg"], radius=5)
        c.text(bx + 27, 216, label, size=10, anchor="middle")

    c.parts.append('<circle cx="492" cy="246" r="5" fill="%s"/>' % p["dim"])
    c.text(506, 250, "page layout u_webpage", size=11.5, weight="600")
    c.text(506, 268, "pageLayout/u-webpage.production.header.html", size=10.5,
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
