"""Placeholder media generation.

Real media comes from Higgsfield via the runner's agent driver. These stand-ins
exist so the panel can be exercised — and its acceptance criteria measured —
without spending a credit. Stills are SVG; "clips" are SVG with SMIL animation,
which the panel renders in an <img> and which loop forever without a decoder.
"""

import hashlib
import os

PALETTES = [
    ("#1b2a3a", "#3d6b8f", "#e8d9c0"),
    ("#2a1b24", "#8f3d5c", "#f0dcc8"),
    ("#1d2a20", "#4f8f5a", "#e4e9cf"),
    ("#2b2418", "#9a7b3a", "#f3e6c8"),
    ("#231c2e", "#6b4f9a", "#ded2f0"),
    ("#2e1f1b", "#a8573a", "#f2ded0"),
]


def _seeded(shot_id, salt=""):
    digest = hashlib.sha256(("%s|%s" % (shot_id, salt)).encode("utf-8")).digest()
    return digest


def _palette(digest):
    return PALETTES[digest[0] % len(PALETTES)]


def _frame(digest, width, height):
    """Deterministic pseudo-composition so tiles are distinguishable at a glance
    and drift between attempts is visible."""
    shapes = []
    for i in range(5):
        x = (digest[i * 3] / 255) * width * 0.8
        y = (digest[i * 3 + 1] / 255) * height * 0.8
        r = 40 + (digest[i * 3 + 2] / 255) * (min(width, height) * 0.28)
        shapes.append((x, y, r))
    return shapes


def make_still(path, shot_id, label="", tier="", attempt=1, width=1024, height=576):
    digest = _seeded(shot_id, "still-%s" % attempt)
    back, mid, fore = _palette(digest)
    shapes = _frame(digest, width, height)
    circles = "".join(
        '<circle cx="%.1f" cy="%.1f" r="%.1f" fill="%s" opacity="%.2f"/>'
        % (x, y, r, mid, 0.25 + (i * 0.1)) for i, (x, y, r) in enumerate(shapes))
    svg = (
        '<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 %d %d" '
        'width="%d" height="%d">'
        '<rect width="%d" height="%d" fill="%s"/>%s'
        '<rect x="0" y="%d" width="%d" height="72" fill="#000" opacity="0.45"/>'
        '<text x="24" y="%d" font-family="monospace" font-size="34" fill="%s">%s</text>'
        '<text x="24" y="%d" font-family="sans-serif" font-size="20" fill="%s" '
        'opacity="0.75">%s</text></svg>'
        % (width, height, width, height, width, height, back, circles,
           height - 72, width,
           height - 40, fore, shot_id,
           height - 14, fore, _escape(("tier %s · %s" % (tier, label))[:64])))
    _write(path, svg)
    return path


def make_clip(path, shot_id, label="", tier="", attempt=1, seconds=5,
              width=1024, height=576):
    digest = _seeded(shot_id, "clip-%s" % attempt)
    back, mid, fore = _palette(digest)
    shapes = _frame(digest, width, height)
    parts = []
    for i, (x, y, r) in enumerate(shapes):
        drift = 30 + (digest[i] % 90)
        parts.append(
            '<circle cx="%.1f" cy="%.1f" r="%.1f" fill="%s" opacity="%.2f">'
            '<animate attributeName="cx" values="%.1f;%.1f;%.1f" dur="%ss" '
            'repeatCount="indefinite"/></circle>'
            % (x, y, r, mid, 0.25 + (i * 0.1), x, x + drift, x, seconds))
    svg = (
        '<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 %d %d" '
        'width="%d" height="%d">'
        '<rect width="%d" height="%d" fill="%s"/>%s'
        '<rect x="0" y="%d" width="%d" height="72" fill="#000" opacity="0.45"/>'
        '<text x="24" y="%d" font-family="monospace" font-size="34" fill="%s">%s</text>'
        '<text x="24" y="%d" font-family="sans-serif" font-size="20" fill="%s" '
        'opacity="0.75">%s</text></svg>'
        % (width, height, width, height, width, height, back, "".join(parts),
           height - 72, width,
           height - 40, fore, shot_id,
           height - 14, fore,
           _escape(("tier %s · %ss · %s" % (tier, seconds, label))[:64])))
    _write(path, svg)
    return path


def _escape(text):
    return (text.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;"))


def _write(path, body):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8") as fh:
        fh.write(body)
