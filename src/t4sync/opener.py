"""Open a source file in the editor, or show it in the file manager.

The inspector tells you which file produced what you are looking at, and then
leaves you to go and find it. That last step is a path copied out of a browser
and pasted into something else, every time -- so the server the page is
already talking to does it instead.

It has to be the server: a page cannot launch a local application, and should
not be able to. Which makes the path argument the whole security question, and
the answer is that it is resolved against the project and refused if it lands
outside. Not because a loopback server is likely to be attacked, but because
"open any file on this machine" is not a capability worth leaving lying around
for the sake of saving a copy and paste.
"""
import os
import subprocess
import sys


class Refused(Exception):
    """The path was outside the project, or the action is unknown."""


def safe_path(root, candidate):
    """Resolve `candidate` under `root`, or refuse.

    Symlinks are resolved before the check, so a link inside the project
    pointing out of it is caught rather than followed.
    """
    root = os.path.realpath(str(root))
    full = os.path.realpath(os.path.join(root, str(candidate)))
    if full != root and not full.startswith(root + os.sep):
        raise Refused("%s is outside the project" % candidate)
    if not os.path.exists(full):
        raise Refused("%s does not exist" % candidate)
    return full


def commands(action, path):
    """The command to run, per platform. Returns a list of argv candidates.

    More than one, because the first choice is not always installed: a Linux
    box without xdg-open should still manage something.
    """
    if sys.platform == "darwin":
        if action == "reveal":
            return [["open", "-R", path]]
        # -t opens in the editor registered for plain text, which is what
        # someone who has set one expects; without that, the default app.
        return [[os.environ["EDITOR"], path]] if os.environ.get("EDITOR") \
            else [["open", "-t", path], ["open", path]]

    if os.name == "nt":
        if action == "reveal":
            return [["explorer", "/select,", path]]
        return [["cmd", "/c", "start", "", path]]

    folder = os.path.dirname(path)
    if action == "reveal":
        return [["xdg-open", folder], ["nautilus", folder], ["thunar", folder]]
    editor = os.environ.get("VISUAL") or os.environ.get("EDITOR")
    return ([[editor, path]] if editor else []) + [["xdg-open", path]]


def open_path(root, candidate, action="edit"):
    """Open or reveal a file. Returns the command that worked."""
    if action not in ("edit", "reveal"):
        raise Refused("unknown action %r" % action)
    full = safe_path(root, candidate)

    errors = []
    for argv in commands(action, full):
        try:
            # Detached and output-discarded: a GUI editor that keeps running
            # must not hold the request open, and a console editor launched
            # by mistake must not inherit the server's terminal.
            subprocess.Popen(argv, stdout=subprocess.DEVNULL,
                             stderr=subprocess.DEVNULL,
                             stdin=subprocess.DEVNULL)
            return argv
        except (OSError, ValueError) as exc:
            errors.append("%s: %s" % (argv[0], exc))
    raise Refused("nothing could open it (%s)" % "; ".join(errors)
                  if errors else "no opener for this platform")
