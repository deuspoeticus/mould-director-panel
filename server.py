#!/usr/bin/env python3
"""The panel's server. A browser cannot write to disk, so something has to.

It reads and writes shots/*.json through store.py and serves media off disk.
It does not call Higgsfield, and there is no code path here that could: no
route in this file spends a credit. Approvals and rejections write intent;
runner.py picks queued work up on its next pass.

    python3 server.py --port 8787
"""

import argparse
import json
import mimetypes
import os
import posixpath
import re
import urllib.parse
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import codes as codeset
import store as store_mod
from contact_sheet import build_contact_sheet
from validate import validate_manifest

mimetypes.add_type("video/mp4", ".mp4")
mimetypes.add_type("video/webm", ".webm")
mimetypes.add_type("image/svg+xml", ".svg")

MAX_BODY = 1 << 20


class Handler(BaseHTTPRequestHandler):
    server_version = "AyniPanel/1.0"
    protocol_version = "HTTP/1.1"
    store = None

    # ----------------------------------------------------------- plumbing

    def log_message(self, fmt, *args):
        if self.server.verbose:
            super().log_message(fmt, *args)

    def _send(self, status, body, content_type="application/json; charset=utf-8",
              extra=None):
        if isinstance(body, str):
            body = body.encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        for key, value in (extra or {}).items():
            self.send_header(key, value)
        self.end_headers()
        if self.command != "HEAD":
            self.wfile.write(body)

    def _json(self, payload, status=200):
        self._send(status, json.dumps(payload, ensure_ascii=False))

    def _error(self, message, status=400):
        self._json({"ok": False, "error": message}, status)

    def _body(self):
        length = int(self.headers.get("Content-Length") or 0)
        if length <= 0:
            return {}
        if length > MAX_BODY:
            raise store_mod.StoreError("request body too large", 413)
        raw = self.rfile.read(length)
        try:
            payload = json.loads(raw.decode("utf-8"))
        except ValueError:
            raise store_mod.StoreError("body must be JSON")
        if not isinstance(payload, dict):
            raise store_mod.StoreError("body must be a JSON object")
        return payload

    # ------------------------------------------------------------- routes

    def do_HEAD(self):
        self.do_GET()

    def do_GET(self):
        parsed = urllib.parse.urlparse(self.path)
        path = parsed.path
        query = urllib.parse.parse_qs(parsed.query)
        st = self.server.store
        try:
            if path in ("/", "/index.html", "/panel.html"):
                return self._file(os.path.join(os.path.dirname(os.path.abspath(__file__)),
                                               "panel.html"))
            if path == "/favicon.ico":
                return self._send(204, b"", "image/x-icon")
            if path == "/api/shots":
                return self._json({"ok": True, "shots": self._filter(st.shots(), query)})
            if path == "/api/images":
                return self._json({"ok": True, "stage": "image",
                                   "records": self._queue_view(st, "image", query)})
            if path == "/api/videos":
                return self._json({"ok": True, "stage": "video",
                                   "records": self._queue_view(st, "video", query)})
            if path == "/api/status":
                return self._json({"ok": True, **st.status()})
            if path == "/api/ledger":
                return self._json({"ok": True, **st.ledger()})
            if path == "/api/coverage":
                return self._json({"ok": True, **st.coverage()})
            if path == "/api/codes":
                return self._json({"ok": True, "codes": codeset.CODES,
                                   "tiers": list(codeset.TIERS),
                                   "attempt_cap": st.attempt_cap,
                                   "pricing": st.pricing,
                                   "states": store_mod.STATES,
                                   "project": st.config.get("project", "AYNI")})
            if path == "/api/shot":
                shot_id = (query.get("id") or [""])[0]
                return self._json({"ok": True, **st.inspect(shot_id)})
            if path == "/api/validate":
                return self._json({"ok": True, **validate_manifest(st)})
            if path.startswith("/media/"):
                return self._serve_tree(st.media_dir, path[len("/media/"):])
            if path.startswith("/exports/"):
                return self._serve_tree(st.export_dir, path[len("/exports/"):])
            return self._error("no route for %s" % path, 404)
        except store_mod.StoreError as exc:
            return self._error(str(exc), exc.status)
        except Exception as exc:  # keep the panel alive on a bad request
            return self._error("%s: %s" % (type(exc).__name__, exc), 500)

    def do_POST(self):
        parsed = urllib.parse.urlparse(self.path)
        st = self.server.store
        try:
            body = self._body()
            if parsed.path == "/api/review":
                result = st.review(
                    shot_id=body.get("shot_id"),
                    stage=body.get("stage"),
                    action=body.get("action"),
                    codes=body.get("codes"),
                    note=body.get("note", ""),
                    route=body.get("route"),
                    escape=body.get("escape"),
                    merge=bool(body.get("merge")),
                )
                return self._json({"ok": True, **result})
            if parsed.path == "/api/queue":
                result = st.enqueue(
                    shot_id=body.get("shot_id"),
                    stage=body.get("stage"),
                    kind=body.get("kind", "regenerate"),
                    note=body.get("note", ""),
                )
                return self._json({"ok": True, **result})
            if parsed.path == "/api/bulk_approve":
                result = st.bulk_approve(stage=body.get("stage", "image"),
                                         tier=body.get("tier", "C"),
                                         shot_ids=body.get("shot_ids"))
                return self._json({"ok": True, **result})
            if parsed.path == "/api/export/contact_sheet":
                path = build_contact_sheet(st, stage=body.get("stage", "image"),
                                           scene=body.get("scene"),
                                           tier=body.get("tier"),
                                           state=body.get("state"))
                return self._json({"ok": True, "path": path,
                                   "url": "/exports/" + os.path.basename(path)})
            return self._error("no route for %s" % parsed.path, 404)
        except store_mod.StoreError as exc:
            return self._error(str(exc), exc.status)
        except Exception as exc:
            return self._error("%s: %s" % (type(exc).__name__, exc), 500)

    # ------------------------------------------------------------ helpers

    def _filter(self, shots, query):
        scene = (query.get("scene") or [None])[0]
        tier = (query.get("tier") or [None])[0]
        if scene not in (None, "", "all"):
            shots = [s for s in shots if str(s.get("scene")) == str(scene)]
        if tier not in (None, "", "all"):
            shots = [s for s in shots if s.get("tier") == tier]
        return shots

    def _queue_view(self, st, stage, query):
        """A review queue: stage records joined with the plan context the
        director needs on the tile, filtered by tier / scene / state / code."""
        want_state = (query.get("state") or ["done"])[0]
        want_tier = (query.get("tier") or ["all"])[0]
        want_scene = (query.get("scene") or ["all"])[0]
        want_code = (query.get("code") or ["all"])[0]
        want_flagged = (query.get("flagged") or ["all"])[0]

        out = []
        for record in st.all(stage):
            shot_id = record["shot_id"]
            plan = st.read("plan", shot_id) or {}
            state = record.get("state")
            if want_state not in ("all", "") and state != want_state:
                continue
            if want_tier not in ("all", "") and plan.get("tier") != want_tier:
                continue
            if want_scene not in ("all", "") and str(plan.get("scene")) != str(want_scene):
                continue
            if want_code not in ("all", ""):
                seen = {c for d in record.get("defects", []) for c in d.get("codes", [])}
                if want_code not in seen:
                    continue
            if want_flagged == "1" and not record.get("flagged"):
                continue
            scene, slot = store_mod.parse_shot_id(shot_id)
            image = st.read("image", shot_id) if stage == "video" else record
            out.append({
                "shot_id": shot_id,
                "stage": stage,
                "scene": plan.get("scene", scene),
                "slot": slot,
                "tier": plan.get("tier"),
                "description": plan.get("description", ""),
                "elements": plan.get("elements", []),
                "duration_target": plan.get("duration_target"),
                "record": record,
                "still": (image or {}).get("media"),
                "attempt_cap": st.attempt_cap,
                "at_cap": record.get("attempts", 0) >= st.attempt_cap,
                "estimate_regenerate": st.estimate(stage, record, "regenerate"),
                "estimate_local_edit": st.estimate(stage, record, "local_edit"),
            })
        out.sort(key=lambda r: (r["scene"] if r["scene"] is not None else 999,
                                r["slot"] or 0, r["shot_id"]))
        return out

    def _file(self, path):
        if not os.path.isfile(path):
            return self._error("missing %s" % os.path.basename(path), 404)
        ctype = mimetypes.guess_type(path)[0] or "application/octet-stream"
        with open(path, "rb") as fh:
            body = fh.read()
        return self._send(200, body, ctype)

    def _serve_tree(self, root, relative):
        """Serve a file from under `root`, and only from under `root`."""
        relative = urllib.parse.unquote(relative)
        clean = posixpath.normpath("/" + relative.replace("\\", "/")).lstrip("/")
        if not clean or clean.startswith(".."):
            return self._error("bad path", 400)
        target = os.path.abspath(os.path.join(root, *clean.split("/")))
        if os.path.commonpath([target, os.path.abspath(root)]) != os.path.abspath(root):
            return self._error("path escapes root", 403)
        if not os.path.isfile(target):
            return self._error("not found", 404)
        ctype = mimetypes.guess_type(target)[0] or "application/octet-stream"
        size = os.path.getsize(target)
        range_header = self.headers.get("Range")
        # Clips are served to a <video> element, which asks for ranges.
        match = re.match(r"bytes=(\d*)-(\d*)$", range_header or "")
        if match and (match.group(1) or match.group(2)):
            start = int(match.group(1)) if match.group(1) else 0
            end = int(match.group(2)) if match.group(2) else size - 1
            end = min(end, size - 1)
            if start > end:
                return self._send(416, b"", ctype,
                                  {"Content-Range": "bytes */%d" % size})
            with open(target, "rb") as fh:
                fh.seek(start)
                chunk = fh.read(end - start + 1)
            return self._send(206, chunk, ctype, {
                "Content-Range": "bytes %d-%d/%d" % (start, end, size),
                "Accept-Ranges": "bytes",
            })
        with open(target, "rb") as fh:
            body = fh.read()
        return self._send(200, body, ctype, {"Accept-Ranges": "bytes"})


def serve(root, port=8787, host="127.0.0.1", verbose=False):
    st = store_mod.Store(root)
    st.ensure_dirs()
    httpd = ThreadingHTTPServer((host, port), Handler)
    httpd.store = st
    httpd.verbose = verbose
    httpd.daemon_threads = True
    return httpd, st


def main():
    parser = argparse.ArgumentParser(description="AYNI production panel server")
    parser.add_argument("--root", default=os.environ.get("AYNI_ROOT", "."),
                        help="project root holding shots/ and media/")
    parser.add_argument("--port", type=int, default=8787)
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--verbose", action="store_true")
    args = parser.parse_args()

    httpd, st = serve(args.root, args.port, args.host, args.verbose)
    print("panel   http://%s:%d" % (args.host, args.port))
    print("root    %s" % st.root)
    print("shots   %d planned, %d image records, %d video records"
          % (len(st.ids("plan")), len(st.ids("image")), len(st.ids("video"))))
    print("no route in this server spends a credit; start runner.py for that")
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        print("\nstopped")


if __name__ == "__main__":
    main()
