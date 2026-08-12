#!/usr/bin/env python3
"""Contact-sheet export — static HTML that does not need the server running,
for sending to Suhan.

Stills are embedded as data URIs, so an image sheet is a single file you can
attach to a message. Clips cannot be embedded sanely, so a video sheet copies
the clips into exports/media/ next to the HTML and links them relatively; send
the folder, or zip it.

    python3 contact_sheet.py --root . --stage image --tier A
"""

import argparse
import base64
import html
import mimetypes
import os
import shutil
import time

import store as store_mod

EMBED_LIMIT = 3 * 1024 * 1024  # per file; above this we copy instead

STYLE = """
:root { color-scheme: dark; }
body { margin:0; background:#0b0b0d; color:#e7e7ea; font:13px/1.45 ui-sans-serif,
  system-ui, -apple-system, "Segoe UI", Roboto, sans-serif; }
header { padding:20px 24px 12px; border-bottom:1px solid #24242a; }
h1 { margin:0 0 4px; font-size:16px; letter-spacing:.04em; text-transform:uppercase; }
.meta { color:#8b8b95; font-size:12px; }
.grid { display:grid; gap:14px; padding:20px 24px 60px;
  grid-template-columns:repeat(auto-fill, minmax(300px, 1fr)); }
figure { margin:0; background:#141418; border:1px solid #24242a; border-radius:6px;
  overflow:hidden; }
figure img, figure video { display:block; width:100%; height:auto; background:#000; }
figcaption { padding:8px 10px; display:flex; gap:8px; align-items:baseline;
  flex-wrap:wrap; }
.id { font:600 12px ui-monospace, SFMono-Regular, Menlo, monospace;
  letter-spacing:.06em; }
.tier { font-size:10px; padding:1px 5px; border-radius:3px; background:#2a2a32;
  color:#c9c9d2; }
.tier.A { background:#4a2330; color:#ffb3c4; }
.tier.B { background:#25313f; color:#a9cdf0; }
.tier.C { background:#26302a; color:#a9d6b4; }
.state { font-size:10px; color:#8b8b95; text-transform:uppercase;
  letter-spacing:.06em; }
.desc { flex:1 1 100%; color:#9a9aa4; font-size:12px; }
.defects { flex:1 1 100%; color:#ff9d9d; font-size:11px;
  font-family:ui-monospace, Menlo, monospace; }
.missing { aspect-ratio:16/9; display:flex; align-items:center;
  justify-content:center; color:#5a5a63; font-size:11px; background:#101014; }
"""


def _data_uri(path):
    ctype = mimetypes.guess_type(path)[0] or "application/octet-stream"
    with open(path, "rb") as fh:
        return "data:%s;base64,%s" % (ctype, base64.b64encode(fh.read()).decode("ascii"))


def build_contact_sheet(st, stage="image", scene=None, tier=None, state=None,
                        out_path=None):
    if stage not in ("image", "video"):
        raise store_mod.StoreError("stage must be image or video")
    os.makedirs(st.export_dir, exist_ok=True)
    stamp = time.strftime("%Y%m%d-%H%M%S", time.gmtime())
    out_path = out_path or os.path.join(
        st.export_dir, "contact-sheet-%s-%s.html" % (stage, stamp))
    asset_dir = os.path.join(st.export_dir, "media")

    tiles = []
    for record in st.all(stage):
        shot_id = record["shot_id"]
        plan = st.read("plan", shot_id) or {}
        if scene not in (None, "", "all") and str(plan.get("scene")) != str(scene):
            continue
        if tier not in (None, "", "all") and plan.get("tier") != tier:
            continue
        if state not in (None, "", "all") and record.get("state") != state:
            continue
        tiles.append((shot_id, plan, record))
    tiles.sort(key=lambda t: (t[1].get("scene") if t[1].get("scene") is not None else 999,
                              t[0]))

    parts = ["<!doctype html><html lang=\"en\"><head><meta charset=\"utf-8\">",
             "<meta name=\"viewport\" content=\"width=device-width,initial-scale=1\">",
             "<title>%s contact sheet — %s</title>"
             % (html.escape(str(st.config.get("project", "AYNI"))), stage),
             "<style>%s</style></head><body>" % STYLE,
             "<header><h1>%s — %s contact sheet</h1>"
             % (html.escape(str(st.config.get("project", "AYNI"))), stage),
             "<div class=\"meta\">%d shots · %s · generated %s UTC · static, no server "
             "required</div></header><div class=\"grid\">"
             % (len(tiles),
                " · ".join(filter(None, [
                    "scene %s" % scene if scene not in (None, "", "all") else None,
                    "tier %s" % tier if tier not in (None, "", "all") else None,
                    "state %s" % state if state not in (None, "", "all") else "all states",
                ])),
                time.strftime("%Y-%m-%d %H:%M", time.gmtime()))]

    for shot_id, plan, record in tiles:
        media = record.get("media")
        source = os.path.join(st.media_dir, media) if media else None
        block = "<div class=\"missing\">no media</div>"
        if source and os.path.isfile(source):
            is_video = os.path.splitext(source)[1].lower() in (".mp4", ".webm", ".mov")
            if is_video or os.path.getsize(source) > EMBED_LIMIT:
                os.makedirs(asset_dir, exist_ok=True)
                shutil.copy2(source, os.path.join(asset_dir, os.path.basename(source)))
                src = "media/" + os.path.basename(source)
            else:
                src = _data_uri(source)
            if is_video:
                block = ("<video src=\"%s\" muted loop autoplay playsinline></video>"
                         % html.escape(src))
            else:
                block = "<img src=\"%s\" alt=\"%s\" loading=\"lazy\">" % (
                    html.escape(src), html.escape(shot_id))

        defects = []
        for defect in record.get("defects", []) or []:
            defects.extend(defect.get("codes", []))
        parts.append(
            "<figure>%s<figcaption>"
            "<span class=\"id\">%s</span>"
            "<span class=\"tier %s\">%s</span>"
            "<span class=\"state\">%s%s</span>"
            "<span class=\"desc\">%s</span>%s"
            "</figcaption></figure>" % (
                block,
                html.escape(shot_id),
                html.escape(plan.get("tier") or ""),
                html.escape(plan.get("tier") or "—"),
                html.escape(record.get("state") or ""),
                " · %d/%d" % (record.get("attempts", 0), st.attempt_cap),
                html.escape(plan.get("description") or ""),
                "<span class=\"defects\">%s</span>" % html.escape(", ".join(defects))
                if defects else "",
            ))

    parts.append("</div></body></html>")
    with open(out_path, "w", encoding="utf-8") as fh:
        fh.write("".join(parts))
    return out_path


def main():
    parser = argparse.ArgumentParser(description="AYNI contact-sheet export")
    parser.add_argument("--root", default=os.environ.get("AYNI_ROOT", "."))
    parser.add_argument("--stage", default="image", choices=("image", "video"))
    parser.add_argument("--scene")
    parser.add_argument("--tier", choices=("A", "B", "C"))
    parser.add_argument("--state")
    parser.add_argument("--out")
    args = parser.parse_args()
    st = store_mod.Store(args.root)
    path = build_contact_sheet(st, args.stage, args.scene, args.tier, args.state,
                               args.out)
    print(path)


if __name__ == "__main__":
    main()
