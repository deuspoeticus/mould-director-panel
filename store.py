"""shots/*.json is the single source of truth. This module is the only code
that knows how those files are shaped.

Three files per shot, each with exactly one writer:

    shots/plan/S07_04.json      written once by the shot-plan pass
    shots/images/S07_04.json    written by the image stage
    shots/videos/S07_04.json    written by the video stage

`shot_id` is the join key. One writer per file means an image-stage session and
a video-stage session can run at the same time on the same shot range without
either being able to corrupt the other's state.

Nothing here spends a credit. This module records intent; runner.py executes it.
"""

import json
import os
import re
import shutil
import tempfile
import time
from datetime import datetime, timezone

import codes as codeset

SHOT_ID_RE = re.compile(r"^S(\d{2})_(\d{2})$")

IMAGE_STATES = ("planned", "prompted", "queued", "done",
                "approved", "rejected", "escape_hatch")
VIDEO_STATES = ("prompted", "queued", "done",
                "approved", "rejected", "escape_hatch", "fix_in_post")

STATES = {"image": IMAGE_STATES, "video": VIDEO_STATES}

# Which states a new generation attempt may be launched from.
QUEUEABLE_FROM = {"image": ("prompted", "rejected"), "video": ("prompted", "rejected")}
# Which states a cheap local edit may be launched from.
LOCAL_EDITABLE_FROM = {"image": ("done", "rejected"), "video": ("done", "rejected")}
# Which states a review decision may be made from.
REVIEWABLE_FROM = ("done", "approved", "rejected", "escape_hatch", "fix_in_post")


class StoreError(Exception):
    """A rule violation. Carries an HTTP status so the server can pass it through."""

    def __init__(self, message, status=400):
        super().__init__(message)
        self.status = status


def now():
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


class Store:
    def __init__(self, root=None, config=None):
        self.root = os.path.abspath(root or os.environ.get("AYNI_ROOT") or os.getcwd())
        self.plan_dir = os.path.join(self.root, "shots", "plan")
        self.image_dir = os.path.join(self.root, "shots", "images")
        self.video_dir = os.path.join(self.root, "shots", "videos")
        self.media_dir = os.path.join(self.root, "media")
        self.runner_dir = os.path.join(self.root, "runner")
        self.export_dir = os.path.join(self.root, "exports")
        self.config = config if config is not None else self._load_config()
        self.pricing = dict(codeset.DEFAULT_PRICING)
        self.pricing.update(self.config.get("pricing", {}))
        self.attempt_cap = int(self.config.get("attempt_cap", codeset.ATTEMPT_CAP))

    # ---------------------------------------------------------------- paths

    def ensure_dirs(self):
        for d in (self.plan_dir, self.image_dir, self.video_dir, self.media_dir,
                  self.runner_dir, self.export_dir,
                  os.path.join(self.runner_dir, "orders")):
            os.makedirs(d, exist_ok=True)

    def _load_config(self):
        path = os.path.join(self.root, "config.json")
        if os.path.exists(path):
            with open(path, encoding="utf-8") as fh:
                return json.load(fh)
        return {}

    def dir_for(self, stage):
        if stage == "image":
            return self.image_dir
        if stage == "video":
            return self.video_dir
        if stage == "plan":
            return self.plan_dir
        raise StoreError("unknown stage %r" % (stage,))

    def path_for(self, stage, shot_id):
        return os.path.join(self.dir_for(stage), "%s.json" % validate_shot_id(shot_id))

    # ------------------------------------------------------------------- io

    def read(self, stage, shot_id):
        path = self.path_for(stage, shot_id)
        if not os.path.exists(path):
            return None
        with open(path, encoding="utf-8") as fh:
            return json.load(fh)

    def write(self, stage, record):
        """Atomic write. A killed process leaves either the old file or the new
        one, never a half-written record."""
        shot_id = validate_shot_id(record["shot_id"])
        directory = self.dir_for(stage)
        os.makedirs(directory, exist_ok=True)
        record["updated_at"] = now()
        path = os.path.join(directory, "%s.json" % shot_id)
        fd, tmp = tempfile.mkstemp(dir=directory, prefix=".%s." % shot_id, suffix=".tmp")
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as fh:
                json.dump(record, fh, indent=2, ensure_ascii=False)
                fh.write("\n")
                fh.flush()
                os.fsync(fh.fileno())
            os.replace(tmp, path)
        except BaseException:
            if os.path.exists(tmp):
                os.unlink(tmp)
            raise
        return record

    def delete(self, stage, shot_id, reason="deleted"):
        """Records are never destroyed outright — they move to shots/attic/ so a
        routing decision can always be audited after the fact."""
        path = self.path_for(stage, shot_id)
        if not os.path.exists(path):
            return False
        attic = os.path.join(self.root, "shots", "attic", stage)
        os.makedirs(attic, exist_ok=True)
        stamp = time.strftime("%Y%m%dT%H%M%S", time.gmtime())
        shutil.move(path, os.path.join(attic, "%s.%s.%s.json" % (shot_id, stamp, reason)))
        return True

    def ids(self, stage):
        directory = self.dir_for(stage)
        if not os.path.isdir(directory):
            return []
        out = []
        for name in os.listdir(directory):
            if name.endswith(".json") and not name.startswith("."):
                out.append(name[:-5])
        return sorted(out)

    def all(self, stage):
        out = []
        for shot_id in self.ids(stage):
            rec = self.read(stage, shot_id)
            if rec:
                out.append(rec)
        return out

    # -------------------------------------------------------------- records

    def new_plan(self, shot_id, scene, tier, description="", elements=None,
                 duration_target=5):
        return {
            "shot_id": validate_shot_id(shot_id),
            "scene": int(scene),
            "tier": tier,
            "description": description,
            "elements": list(elements or []),
            "duration_target": duration_target,
        }

    def new_image(self, shot_id, prompt="", resolution="2k", state="prompted"):
        return {
            "shot_id": validate_shot_id(shot_id),
            "prompt": prompt,
            "resolution": resolution,
            "job_id": None,
            "media": None,
            "state": state,
            "attempts": 0,
            "defects": [],
            "credits_spent": 0,
            "flagged": False,
            "local_editable": False,
            "escape": None,
            "history": [],
        }

    def new_video(self, shot_id, start_frame, prompt="", duration=5,
                  references=None, state="prompted"):
        return {
            "shot_id": validate_shot_id(shot_id),
            "start_frame": start_frame,
            "prompt": prompt,
            "references": list(references or []),
            "duration": duration,
            "job_id": None,
            "media": None,
            "state": state,
            "attempts": 0,
            "defects": [],
            "credits_spent": 0,
            "flagged": False,
            "local_editable": False,
            "escape": None,
            "history": [],
        }

    # ---------------------------------------------------------------- joins

    def shots(self):
        """Plan records joined with current image and video state."""
        out = []
        seen = set()
        for plan in self.all("plan"):
            shot_id = plan["shot_id"]
            seen.add(shot_id)
            out.append(self._join(plan, shot_id))
        # Orphans still belong in the list; Coverage is where they get named.
        for stage in ("image", "video"):
            for shot_id in self.ids(stage):
                if shot_id in seen:
                    continue
                seen.add(shot_id)
                out.append(self._join(None, shot_id))
        out.sort(key=lambda s: s["shot_id"])
        return out

    def _join(self, plan, shot_id):
        image = self.read("image", shot_id)
        video = self.read("video", shot_id)
        scene, slot = parse_shot_id(shot_id)
        return {
            "shot_id": shot_id,
            "scene": (plan or {}).get("scene", scene),
            "slot": slot,
            "tier": (plan or {}).get("tier"),
            "description": (plan or {}).get("description", ""),
            "elements": (plan or {}).get("elements", []),
            "duration_target": (plan or {}).get("duration_target"),
            "plan": plan,
            "image": image,
            "video": video,
            "image_state": (image or {}).get("state"),
            "video_state": (video or {}).get("state"),
            "orphan": plan is None,
        }

    def queue_items(self, stage=None):
        items = []
        for st in (("image", "video") if stage is None else (stage,)):
            for rec in self.all(st):
                if rec.get("state") == "queued":
                    items.append({
                        "shot_id": rec["shot_id"],
                        "stage": st,
                        "kind": rec.get("queued_kind", "regenerate"),
                        "attempts": rec.get("attempts", 0),
                        "queued_at": rec.get("queued_at"),
                        "job_id": rec.get("job_id"),
                        "estimate": self.estimate(st, rec, rec.get("queued_kind",
                                                                  "regenerate")),
                    })
        items.sort(key=lambda i: (i.get("queued_at") or "", i["shot_id"]))
        return items

    # -------------------------------------------------------------- pricing

    def estimate(self, stage, record, kind="regenerate"):
        """What one unit of work costs. Panel shows it; runner books it."""
        if kind == "local_edit":
            return int(self.pricing["local_edit_%s" % stage])
        if stage == "image":
            table = self.pricing["image"]
            return int(table.get(record.get("resolution") or "", table["default"]))
        seconds = record.get("duration") or 5
        return int(max(self.pricing["video_minimum"],
                       seconds * self.pricing["video_per_second"]))

    # ------------------------------------------------------------ mutations

    def _log(self, record, event, **detail):
        record.setdefault("history", []).append(
            {"at": now(), "event": event, **detail})

    def _load_for_write(self, stage, shot_id):
        record = self.read(stage, shot_id)
        if record is None:
            raise StoreError("no %s record for %s" % (stage, shot_id), 404)
        return record

    def review(self, shot_id, stage, action, codes=None, note="", route=None,
               escape=None, merge=False, actor="director"):
        """Record a decision. Writes through immediately — there is no unsaved
        state anywhere in this system."""
        if stage not in ("image", "video"):
            raise StoreError("stage must be image or video")
        if action == "reroute":
            return self.reroute(shot_id, route)
        record = self._load_for_write(stage, shot_id)
        codes = [c.strip().upper() for c in (codes or []) if c and c.strip()]

        if action == "approve":
            return self._approve(stage, record)
        if action == "reject":
            return self._reject(stage, record, codes, note, route, merge)
        if action == "flag":
            record["flagged"] = not record.get("flagged")
            self._log(record, "flag", flagged=record["flagged"], by=actor)
            self.write(stage, record)
            return {"shot_id": shot_id, "stage": stage, "flagged": record["flagged"],
                    "state": record["state"]}
        if action == "local":
            record["local_editable"] = not record.get("local_editable")
            self._log(record, "mark_local_editable",
                      local_editable=record["local_editable"])
            self.write(stage, record)
            return {"shot_id": shot_id, "stage": stage,
                    "local_editable": record["local_editable"],
                    "state": record["state"],
                    "estimate": self.estimate(stage, record, "local_edit")}
        if action in ("escape_hatch", "fix_in_post"):
            return self._escape(stage, record, action, escape, codes, note)
        raise StoreError("unknown action %r" % (action,))

    def _approve(self, stage, record):
        if record["state"] not in REVIEWABLE_FROM:
            raise StoreError("cannot approve %s from state %r"
                             % (record["shot_id"], record["state"]))
        if not record.get("media"):
            raise StoreError("cannot approve %s: no media" % record["shot_id"])
        record["state"] = "approved"
        self._log(record, "approve", media=record["media"])
        self.write(stage, record)
        result = {"shot_id": record["shot_id"], "stage": stage, "state": "approved"}
        if stage == "image":
            result["video"] = self._open_video_stage(record)
        return result

    def _open_video_stage(self, image):
        """A video record exists only when its still is approved. That rule is
        what makes stage progress a directory listing instead of a state
        comparison, so it lives here and nowhere else."""
        shot_id = image["shot_id"]
        plan = self.read("plan", shot_id) or {}
        existing = self.read("video", shot_id)
        if existing:
            # Re-approval of a different still. Do not silently rewrite the
            # video record — Coverage surfaces the stale start frame and the
            # director decides whether the clip survives.
            if existing.get("start_frame") != image.get("media"):
                return {"created": False, "stale_start_frame": True,
                        "start_frame": existing.get("start_frame")}
            return {"created": False, "stale_start_frame": False}
        video = self.new_video(
            shot_id,
            start_frame=image["media"],
            prompt="",
            duration=plan.get("duration_target") or 5,
        )
        self._log(video, "created", start_frame=image["media"],
                  from_image_attempt=image.get("attempts", 0))
        self.write("video", video)
        return {"created": True, "stale_start_frame": False,
                "start_frame": image["media"]}

    def _reject(self, stage, record, codes, note, route, merge=False):
        if not codes:
            raise StoreError("a rejection needs at least one defect code")
        valid = codeset.valid_codes(stage)
        unknown = [c for c in codes if c not in valid]
        if unknown:
            raise StoreError("unknown %s defect code(s): %s"
                             % (stage, ", ".join(unknown)))
        if record["state"] not in REVIEWABLE_FROM:
            raise StoreError("cannot reject %s from state %r"
                             % (record["shot_id"], record["state"]))

        # Routing: default from the first code, override allowed.
        if route not in (None, "image", "video"):
            raise StoreError("route must be image or video")
        default = codeset.default_route(stage, codes[0])
        target = route or default
        if stage == "image" and target != "image":
            raise StoreError("an image-stage defect cannot route downstream")

        if stage == "video" and target == "image":
            return self._reject_upstream(record, codes, note, merge)

        # A second code pressed on the same tile is more detail about one
        # rejection, not a second rejection. Merging keeps the defect histogram
        # honest.
        merged = merge and self._merge_into_last(record, codes, note)
        if not merged:
            record["defects"].append({"at": now(), "codes": codes, "note": note or "",
                                      "attempt": record.get("attempts", 0)})
        record["state"] = "rejected"
        all_codes = record["defects"][-1]["codes"]
        record["local_editable"] = all(codeset.is_local_editable(c) for c in all_codes)
        self._log(record, "reject", codes=codes, route=target, merged=bool(merged))
        self.write(stage, record)
        return {
            "shot_id": record["shot_id"], "stage": stage, "state": "rejected",
            "route": target, "routed_upstream": False,
            "codes": all_codes,
            "attempts": record.get("attempts", 0),
            "at_cap": record.get("attempts", 0) >= self.attempt_cap,
            "local_editable": record["local_editable"],
        }

    def _merge_into_last(self, record, codes, note):
        """Fold codes into the most recent defect entry, if that entry belongs
        to the current attempt and the record is still sitting in `rejected`."""
        if record.get("state") != "rejected" or not record.get("defects"):
            return False
        last = record["defects"][-1]
        if last.get("attempt") != record.get("attempts", 0):
            return False
        last["codes"] = sorted(set(last.get("codes", [])) | set(codes))
        last["at"] = now()
        if note:
            last["note"] = note
        return True

    def _reject_upstream(self, video, codes, note, merge=False):
        """The one place the two records must talk. The defect belongs to the
        still, so it is written to the image record, the image returns to
        `rejected`, and the video record goes away — the shot re-enters the
        pipeline at the stage that can actually fix it."""
        shot_id = video["shot_id"]
        image = self.read("image", shot_id)
        if image is None:
            raise StoreError("cannot route %s upstream: no image record" % shot_id, 409)
        merged = merge and self._merge_into_last(image, codes, note)
        if not merged:
            image["defects"].append({
                "at": now(), "codes": codes, "note": note or "",
                "attempt": image.get("attempts", 0), "seen_in": "video",
            })
        image["state"] = "rejected"
        all_codes = image["defects"][-1]["codes"]
        image["local_editable"] = all(codeset.is_local_editable(c) for c in all_codes)
        self._log(image, "reject_from_video", codes=codes,
                  video_attempts=video.get("attempts", 0),
                  video_media=video.get("media"))
        self.write("image", image)
        self.delete("video", shot_id, reason="routed_upstream")
        return {
            "shot_id": shot_id, "stage": "image", "state": "rejected",
            "route": "image", "routed_upstream": True,
            "video_deleted": True, "codes": all_codes,
            "attempts": image.get("attempts", 0),
            "at_cap": image.get("attempts", 0) >= self.attempt_cap,
            "local_editable": image["local_editable"],
        }

    def reroute(self, shot_id, route):
        """Override the routing of the most recent rejection, in either
        direction. The default comes from the defect code; the director has the
        last word, and changing their mind must not cost a record."""
        shot_id = validate_shot_id(shot_id)
        if route not in ("image", "video"):
            raise StoreError("route must be image or video")

        if route == "image":
            video = self.read("video", shot_id)
            if video is None or video.get("state") != "rejected" or \
                    not video.get("defects"):
                raise StoreError("no rejected video decision on %s to route upstream"
                                 % shot_id, 409)
            defect = video["defects"].pop()
            self.write("video", video)
            return self._reject_upstream(video, defect.get("codes", []),
                                         defect.get("note", ""))

        # image -> video: restore the clip that the upstream route retired.
        image = self.read("image", shot_id)
        if image is None or not image.get("defects") or \
                image["defects"][-1].get("seen_in") != "video":
            raise StoreError("no upstream-routed decision on %s to send back "
                             "downstream" % shot_id, 409)
        video = self._restore_video(shot_id)
        if video is None:
            raise StoreError("the video record for %s is not in the attic" % shot_id,
                             409)
        defect = image["defects"].pop()
        image["state"] = "approved"
        self._log(image, "reroute_downstream", codes=defect.get("codes", []))
        self.write("image", image)
        video["defects"].append({"at": now(), "codes": defect.get("codes", []),
                                 "note": defect.get("note", ""),
                                 "attempt": video.get("attempts", 0)})
        video["state"] = "rejected"
        self._log(video, "restored", reason="reroute_downstream")
        self.write("video", video)
        return {"shot_id": shot_id, "stage": "video", "state": "rejected",
                "route": "video", "routed_upstream": False, "restored": True,
                "codes": defect.get("codes", []),
                "attempts": video.get("attempts", 0),
                "at_cap": video.get("attempts", 0) >= self.attempt_cap,
                "local_editable": video.get("local_editable", False)}

    def _restore_video(self, shot_id):
        attic = os.path.join(self.root, "shots", "attic", "video")
        if not os.path.isdir(attic):
            return None
        candidates = sorted(name for name in os.listdir(attic)
                            if name.startswith("%s." % shot_id)
                            and name.endswith("routed_upstream.json"))
        if not candidates:
            return None
        source = os.path.join(attic, candidates[-1])
        with open(source, encoding="utf-8") as fh:
            video = json.load(fh)
        os.unlink(source)
        return video

    def _escape(self, stage, record, action, escape, codes, note):
        if action == "fix_in_post" and stage != "video":
            raise StoreError("fix_in_post is a video-stage outcome")
        kind = (escape or {}).get("kind") if isinstance(escape, dict) else escape
        if action == "escape_hatch" and kind not in ("reframe", "cutaway"):
            raise StoreError("escape hatch kind must be reframe or cutaway")
        if codes:
            record["defects"].append({"at": now(), "codes": codes,
                                      "note": note or "",
                                      "attempt": record.get("attempts", 0)})
        record["state"] = action
        record["escape"] = {"kind": kind or "fix_in_post", "at": now(),
                            "note": note or ""}
        self._log(record, action, kind=kind or "fix_in_post")
        self.write(stage, record)
        return {"shot_id": record["shot_id"], "stage": stage, "state": action,
                "escape": record["escape"]}

    def enqueue(self, shot_id, stage, kind="regenerate", note=""):
        """Write intent. No credit is spent here — the runner picks this up on
        its next pass."""
        if stage not in ("image", "video"):
            raise StoreError("stage must be image or video")
        if kind not in ("regenerate", "local_edit"):
            raise StoreError("kind must be regenerate or local_edit")
        record = self._load_for_write(stage, shot_id)

        if record["state"] == "queued":
            raise StoreError("%s %s is already queued" % (shot_id, stage), 409)

        if kind == "regenerate":
            if record["state"] not in QUEUEABLE_FROM[stage]:
                raise StoreError(
                    "cannot regenerate %s %s from state %r"
                    % (shot_id, stage, record["state"]), 409)
            attempts = record.get("attempts", 0)
            if attempts >= self.attempt_cap:
                # Structural, not disciplinary. At 3/3 there is no path through
                # this call at 2am or any other time.
                raise StoreError(
                    "%s %s is at the attempt cap (%d/%d) — use an escape hatch"
                    % (shot_id, stage, attempts, self.attempt_cap), 409)
            record["attempts"] = attempts + 1
        else:
            if record["state"] not in LOCAL_EDITABLE_FROM[stage]:
                raise StoreError(
                    "cannot local-edit %s %s from state %r"
                    % (shot_id, stage, record["state"]), 409)
            if not record.get("media"):
                raise StoreError("cannot local-edit %s: no media" % shot_id, 409)

        record["state"] = "queued"
        record["queued_kind"] = kind
        record["queued_at"] = now()
        record["queue_note"] = note or ""
        record["job_id"] = None
        estimate = self.estimate(stage, record, kind)
        self._log(record, "queued", kind=kind, estimate=estimate,
                  attempt=record.get("attempts", 0))
        self.write(stage, record)
        return {"shot_id": shot_id, "stage": stage, "state": "queued", "kind": kind,
                "attempts": record.get("attempts", 0), "estimate": estimate}

    def bulk_approve(self, stage, tier="C", shot_ids=None):
        """C-tier only. Hero and normal coverage get looked at."""
        if tier != "C":
            raise StoreError("bulk approve is C-tier only")
        approved, skipped = [], []
        wanted = set(shot_ids) if shot_ids else None
        for record in self.all(stage):
            shot_id = record["shot_id"]
            if wanted is not None and shot_id not in wanted:
                continue
            plan = self.read("plan", shot_id)
            if not plan or plan.get("tier") != "C":
                skipped.append({"shot_id": shot_id, "why": "not C tier"})
                continue
            if record.get("state") != "done":
                skipped.append({"shot_id": shot_id, "why": "state %s" % record.get("state")})
                continue
            try:
                self._approve(stage, record)
                approved.append(shot_id)
            except StoreError as exc:
                skipped.append({"shot_id": shot_id, "why": str(exc)})
        return {"stage": stage, "approved": approved, "skipped": skipped}

    # ------------------------------------------------------------- coverage

    def coverage(self):
        """Gaps, not progress. Each gap is a set difference between the three
        directories, which is cheap and hard to get wrong."""
        plan_ids = set(self.ids("plan"))
        image_ids = set(self.ids("image"))
        video_ids = set(self.ids("video"))
        images = {r["shot_id"]: r for r in self.all("image")}
        videos = {r["shot_id"]: r for r in self.all("video")}
        plans = {r["shot_id"]: r for r in self.all("plan")}

        approved_images = {i for i, r in images.items() if r.get("state") == "approved"}

        gaps = {
            "planned_no_image": sorted(plan_ids - image_ids),
            "image_approved_no_video": sorted(approved_images - video_ids),
            "stale_start_frame": [],
            "video_without_approved_still": [],
            "orphan_image": sorted(image_ids - plan_ids),
            "orphan_video": sorted(video_ids - plan_ids),
            "untiered": sorted(i for i, r in plans.items()
                               if r.get("tier") not in codeset.TIERS),
            "at_cap": [],
        }

        for shot_id, video in videos.items():
            image = images.get(shot_id)
            if image is None or image.get("state") != "approved":
                gaps["video_without_approved_still"].append(shot_id)
                continue
            if video.get("start_frame") != image.get("media"):
                # The still was re-approved after the clip was made. The clip is
                # built on a frame that is no longer the shot.
                gaps["stale_start_frame"].append({
                    "shot_id": shot_id,
                    "video_start_frame": video.get("start_frame"),
                    "current_still": image.get("media"),
                })
        gaps["video_without_approved_still"].sort()
        gaps["stale_start_frame"].sort(key=lambda g: g["shot_id"])

        for stage, table in (("image", images), ("video", videos)):
            for shot_id, rec in sorted(table.items()):
                if rec.get("attempts", 0) >= self.attempt_cap and \
                        rec.get("state") in ("rejected", "done"):
                    gaps["at_cap"].append({"shot_id": shot_id, "stage": stage,
                                           "attempts": rec.get("attempts")})

        scenes = {}
        for shot in self.shots():
            scenes.setdefault(shot["scene"], []).append({
                "shot_id": shot["shot_id"],
                "slot": shot["slot"],
                "tier": shot["tier"],
                "description": shot["description"],
                "image_state": shot["image_state"],
                "video_state": shot["video_state"],
                "orphan": shot["orphan"],
                "flagged": bool((shot["image"] or {}).get("flagged")
                                or (shot["video"] or {}).get("flagged")),
            })
        rows = [{"scene": scene, "shots": sorted(v, key=lambda s: s["slot"])}
                for scene, v in sorted(scenes.items(), key=lambda kv: (kv[0] is None,
                                                                      kv[0]))]

        totals = {
            "planned": len(plan_ids),
            "images": len(image_ids),
            "images_approved": len(approved_images),
            "videos": len(video_ids),
            "videos_approved": sum(1 for r in videos.values()
                                   if r.get("state") == "approved"),
            "gap_count": sum(len(v) for v in gaps.values()),
        }
        return {"rows": rows, "gaps": gaps, "totals": totals}

    # --------------------------------------------------------------- ledger

    def ledger(self):
        spent = {"image": 0, "video": 0}
        histogram = {}
        stage_histogram = {"image": {}, "video": {}}
        for stage in ("image", "video"):
            for rec in self.all(stage):
                spent[stage] += int(rec.get("credits_spent") or 0)
                for defect in rec.get("defects", []):
                    for code in defect.get("codes", []):
                        histogram[code] = histogram.get(code, 0) + 1
                        stage_histogram[stage][code] = \
                            stage_histogram[stage].get(code, 0) + 1

        queued = self.queue_items()
        projected = sum(i["estimate"] for i in queued)
        budget = int(self.config.get("credit_budget", 0))
        total_spent = spent["image"] + spent["video"]

        bars = []
        for code, count in sorted(histogram.items(), key=lambda kv: (-kv[1], kv[0])):
            info = codeset.code_info(code) or {}
            bars.append({"code": code, "count": count,
                         "label": info.get("label", code),
                         "desc": info.get("desc", ""),
                         "stage": "video" if code.startswith("V") else "image"})

        return {
            "budget": budget,
            "spent": total_spent,
            "spent_by_stage": spent,
            "projected": projected,
            "queued_items": len(queued),
            "remaining": budget - total_spent if budget else None,
            "remaining_after_queue": budget - total_spent - projected if budget else None,
            "histogram": bars,
            "histogram_by_stage": stage_histogram,
        }

    # --------------------------------------------------------------- status

    def runner_state(self):
        path = os.path.join(self.runner_dir, "state.json")
        if not os.path.exists(path):
            return {"running": None, "driver": None, "heartbeat": None,
                    "log": [], "failures": []}
        try:
            with open(path, encoding="utf-8") as fh:
                return json.load(fh)
        except (OSError, ValueError):
            return {"running": None, "driver": None, "heartbeat": None,
                    "log": [], "failures": []}

    def status(self):
        state = self.runner_state()
        queue = self.queue_items()
        counts = {}
        for stage in ("image", "video"):
            counts[stage] = {}
            for rec in self.all(stage):
                counts[stage][rec["state"]] = counts[stage].get(rec["state"], 0) + 1
        heartbeat = state.get("heartbeat")
        alive = False
        if heartbeat:
            try:
                delta = (datetime.now(timezone.utc)
                         - datetime.fromisoformat(heartbeat)).total_seconds()
                alive = delta < 30
            except ValueError:
                alive = False
        return {
            "runner": {
                "alive": alive,
                "driver": state.get("driver"),
                "heartbeat": heartbeat,
                "running": state.get("running"),
                "log": state.get("log", [])[-40:],
                "failures": state.get("failures", [])[-40:],
            },
            "queue": queue,
            "queue_depth": len(queue),
            "counts": counts,
            "at": now(),
        }

    # ------------------------------------------------------------ inspector

    def inspect(self, shot_id):
        shot_id = validate_shot_id(shot_id)
        plan = self.read("plan", shot_id)
        image = self.read("image", shot_id)
        video = self.read("video", shot_id)
        if plan is None and image is None and video is None:
            raise StoreError("no such shot %s" % shot_id, 404)
        timeline = []
        for stage, rec in (("image", image), ("video", video)):
            for entry in (rec or {}).get("history", []):
                timeline.append({"stage": stage, **entry})
        timeline.sort(key=lambda e: e.get("at") or "")
        defects = []
        for stage, rec in (("image", image), ("video", video)):
            for defect in (rec or {}).get("defects", []):
                defects.append({"stage": stage, **defect})
        defects.sort(key=lambda d: d.get("at") or "")
        return {"shot_id": shot_id, "plan": plan, "image": image, "video": video,
                "timeline": timeline, "defects": defects,
                "attempt_cap": self.attempt_cap}


def validate_shot_id(shot_id):
    if not isinstance(shot_id, str) or not SHOT_ID_RE.match(shot_id):
        raise StoreError("bad shot id %r (expected S07_04)" % (shot_id,))
    return shot_id


def parse_shot_id(shot_id):
    match = SHOT_ID_RE.match(shot_id or "")
    if not match:
        return None, None
    return int(match.group(1)), int(match.group(2))


def make_shot_id(scene, slot):
    return "S%02d_%02d" % (int(scene), int(slot))
