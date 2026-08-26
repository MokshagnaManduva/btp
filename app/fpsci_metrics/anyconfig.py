"""Just enough .Any handling to inspect a config safely.

G3D .Any is JSON-like with // comments. Two rules matter when checking one:

  * Key checks must run on comment-stripped text, or a sentence in a comment
    mentioning a key reads as the key being set.
  * The apostrophe check must run on the RAW text, comments included, because
    FPSci embeds the whole file verbatim into SQL and a comment apostrophe
    breaks the insert exactly as a real one would.
"""

from __future__ import annotations

BACKSLASH = chr(92)
NEWLINE = chr(10)


def strip_comments(text):
    """Remove // comments, leaving anything inside a string literal alone."""
    out, i, n = [], 0, len(text)
    in_str = False
    while i < n:
        c = text[i]
        if in_str:
            out.append(c)
            if c == BACKSLASH and i + 1 < n:
                out.append(text[i + 1])
                i += 2
                continue
            if c == '"':
                in_str = False
            i += 1
            continue
        if c == '"':
            in_str = True
            out.append(c)
            i += 1
            continue
        if c == '/' and i + 1 < n and text[i + 1] == '/':
            while i < n and text[i] != NEWLINE:
                i += 1
            continue
        out.append(c)
        i += 1
    return ''.join(out)


def scalar(text, key):
    """The value of a top-level-ish ``"key": value`` line, as a string.

    Returns None when the key is absent. Comments are stripped first, so a key
    named only in prose does not count as set.
    """
    needle = '"%s"' % key
    for line in strip_comments(text).splitlines():
        stripped = line.strip()
        if not stripped.startswith(needle):
            continue
        _, _, rest = stripped.partition(":")
        return rest.strip().rstrip(",").strip()
    return None


def count_key(text, key):
    """How many times ``"key"`` appears as a set key, ignoring comments."""
    return strip_comments(text).count('"%s"' % key)
