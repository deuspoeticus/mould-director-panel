#!/usr/bin/env python3
"""Thumbnails for the review wall.

The grid puts forty tiles on screen at once. Serving forty full-resolution 2k
stills to do that is the difference between a wall that scrolls and a wall that
stutters, and stutter is what makes a director inspect one shot at a time again.

Thumbnails are written next to the media, under `media/.thumbs/`, mirroring the
media path. They are generated when the runner books a result, on demand when
the panel asks for one that does not exist yet, and in bulk by this script.

There is no hard dependency. Two backends are detected independently, because
the machine that can resize a still is not necessarily the one that can pull a
frame out of a clip:

    stills      pillow (pip install pillow) > ffmpeg > sips (macOS) > none
    posters     ffmpeg > none

With neither, the panel still works: /thumb falls back to the original file,
exactly as it behaved before thumbnails existed. A missing poster frame costs a
grey tile until playback starts, nothing more.

    python3 thumbs.py --root .            # warm the cache for existing media
    python3 thumbs.py --root . --force    # rebuild every thumbnail
"""

import argparse
import os
import shutil
import subprocess

import store as store_mod

THUMB_DIR = ".thumbs"
DEFAULT_WIDTH = 720
QUALITY = 72

# Sources we never thumbnail: already small, or not raster at all.
SKIP_EXT = {".svg", ".gif"}
VIDEO_EXT = {".mp4", ".webm", ".mov", ".m4v"}

_still_backend = None
_video_backend = None


def still_backend():
    """What resizes a still on this machine, decided once."""
    global _still_backend
    if _still_backend is not None:
        return _still_backend
    try:
        import PIL.Image  # noqa: F401
        _still_backend = "pillow"
    except ImportError:
        if shutil.which("ffmpeg"):
            _still_backend = "ffmpeg"
        elif shutil.which("sips"):
            _still_backend = "sips"
        else:
            _still_backend = "none"
    return _still_backend


def video_backend():
    """What pulls a poster frame out of a clip. Only ffmpeg can, and whether
    Pillow happens to be installed has nothing to do with it."""
    global _video_backend
    if _video_backend is not None:
        return _video_backend
    _video_backend = "ffmpeg" if shutil.which("ffmpeg") else "none"
    return _video_backend


def set_backends(still=None, video=None):
    """Override detection — used by the tests to exercise each path."""
    global _still_backend, _video_backend
    _still_backend = still
    _video_backend = video


def is_video(media):
    return os.path.splitext(media or "")[1].lower() in VIDEO_EXT


def thumb_relative(media, extension=".jpg"):
    """Where the thumbnail for this media path lives, relative to media root."""
    stem = os.path.splitext(media)[0]
    return "%s/%s%s" % (THUMB_DIR, stem, extension)


def wants_thumb(media):
    if not media:
        return False
    extension = os.path.splitext(media)[1].lower()
    if extension in SKIP_EXT:
        return False
    if extension in VIDEO_EXT:
        return video_backend() != "none"
    return still_backend() != "none"


def ensure(st, media, force=False, width=None):
    """Return the thumbnail's path relative to the media root, or None if this
    machine cannot make one. Never raises — a missing thumbnail is a fallback,
    not a failure."""
    if not wants_thumb(media):
        return None
    source = os.path.join(st.media_dir, media)
    if not os.path.isfile(source):
        return None
    if not force:
        fresh = _existing(st, media, source)
        if fresh:
            return fresh
    width = width or int(st.config.get("thumbnail_width", DEFAULT_WIDTH))
    relative = thumb_relative(media)
    target = os.path.join(st.media_dir, relative)
    os.makedirs(os.path.dirname(target), exist_ok=True)
    try:
        if is_video(media):
            return _poster_frame(st, media, source, target, width)
        _still(source, target, width)
    except Exception:
        # A machine without a working backend serves originals and carries on.
        if os.path.exists(target):
            os.unlink(target)
        return None
    return relative if os.path.exists(target) else None


def _existing(st, media, source):
    """A thumbnail already on disk and newer than its source, in either format
    a poster frame might have landed in."""
    for extension in (".jpg", ".png"):
        relative = thumb_relative(media, extension)
        path = os.path.join(st.media_dir, relative)
        if os.path.exists(path) and os.path.getmtime(path) >= os.path.getmtime(source):
            return relative
    return None


def _still(source, target, width):
    name = still_backend()
    if name == "pillow":
        from PIL import Image
        with Image.open(source) as image:
            image = image.convert("RGB")
            if image.width > width:
                height = round(image.height * (width / image.width))
                image = image.resize((width, height), Image.LANCZOS)
            image.save(target, "JPEG", quality=QUALITY, optimize=True)
        return
    if name == "ffmpeg":
        _run(["ffmpeg", "-y", "-loglevel", "error", "-i", source,
              "-vf", "scale='min(%d,iw)':-2" % width,
              "-q:v", "5", target])
        return
    if name == "sips":
        _run(["sips", "-s", "format", "jpeg", "-s", "formatOptions", str(QUALITY),
              "-Z", str(width), source, "--out", target])
        return
    raise RuntimeError("no thumbnail backend")


def _poster_frame(st, media, source, target, width):
    """First frame, scaled. Most ffmpeg builds write JPEG; a build without an
    encoder for it still has PNG, and a PNG poster beats no poster."""
    scale = "scale='min(%d,iw)':-2" % width
    try:
        _run(["ffmpeg", "-y", "-loglevel", "error", "-i", source,
              "-frames:v", "1", "-vf", scale, "-q:v", "5", target])
        return thumb_relative(media)
    except (subprocess.SubprocessError, OSError):
        if os.path.exists(target):
            os.unlink(target)
    relative = thumb_relative(media, ".png")
    fallback = os.path.join(st.media_dir, relative)
    _run(["ffmpeg", "-y", "-loglevel", "error", "-i", source,
          "-frames:v", "1", "-vf", scale, "-c:v", "png", fallback])
    return relative if os.path.exists(fallback) else None


def _run(command):
    subprocess.run(command, check=True, stdout=subprocess.DEVNULL,
                   stderr=subprocess.DEVNULL, timeout=60)


def backfill(st, force=False):
    """Warm the cache for everything already on disk."""
    made, skipped, failed = [], [], []
    for stage in ("image", "video"):
        for record in st.all(stage):
            media = record.get("media")
            if not media:
                continue
            if not wants_thumb(media):
                skipped.append(media)
                continue
            if ensure(st, media, force=force):
                made.append(media)
            else:
                failed.append(media)
    return {"backend": still_backend(), "video_backend": video_backend(),
            "made": made, "skipped": skipped, "failed": failed}


def main():
    parser = argparse.ArgumentParser(description="build review-wall thumbnails")
    parser.add_argument("--root", default=os.environ.get("AYNI_ROOT", "."))
    parser.add_argument("--force", action="store_true")
    args = parser.parse_args()

    st = store_mod.Store(args.root)
    st.ensure_dirs()
    result = backfill(st, args.force)
    print("stills     %s" % result["backend"])
    print("posters    %s" % result["video_backend"])
    if result["backend"] == "none":
        print("           no thumbnailer found — the panel will serve full-resolution")
        print("           media to the grid. Install one with: pip install pillow")
    if result["video_backend"] == "none":
        print("           no ffmpeg — clips will show a grey tile until they play")
    print("built      %d" % len(result["made"]))
    print("skipped    %d (already small, or unsupported by the backend)"
          % len(result["skipped"]))
    if result["failed"]:
        print("failed     %d" % len(result["failed"]))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
