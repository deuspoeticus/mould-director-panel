#!/usr/bin/env python3
"""Manifest validator: schema check, missing element references, untagged
tiers, prompts referencing elements that do not exist.

Run standalone before a session, or hit /api/validate from the panel.

    python3 validate.py --root .
"""

import argparse
import json
import os
import re

import codes as codeset
import store as store_mod

PLAN_REQUIRED = ("shot_id", "scene", "tier", "description")
IMAGE_REQUIRED = ("shot_id", "prompt", "state", "attempts", "defects", "credits_spent")
VIDEO_REQUIRED = ("shot_id", "start_frame", "prompt", "duration", "state",
                  "attempts", "defects", "credits_spent")

ELEMENT_TOKEN = re.compile(r"\{([A-Za-z0-9_.:-]+)\}")


def _issue(out, level, kind, shot_id, detail):
    out.append({"level": level, "kind": kind, "shot_id": shot_id, "detail": detail})


def validate_manifest(st):
    issues = []
    elements = set()
    plans = {}

    for shot_id in st.ids("plan"):
        try:
            plan = st.read("plan", shot_id)
        except ValueError as exc:
            _issue(issues, "error", "unparseable", shot_id, str(exc))
            continue
        plans[shot_id] = plan
        for field in PLAN_REQUIRED:
            if field not in plan:
                _issue(issues, "error", "schema", shot_id, "plan missing %r" % field)
        if plan.get("shot_id") != shot_id:
            _issue(issues, "error", "schema", shot_id,
                   "shot_id %r does not match filename" % plan.get("shot_id"))
        if plan.get("tier") not in codeset.TIERS:
            _issue(issues, "error", "untagged_tier", shot_id,
                   "tier %r is not one of A/B/C" % plan.get("tier"))
        if not plan.get("description"):
            _issue(issues, "warn", "schema", shot_id, "plan has no description")
        for element in plan.get("elements", []) or []:
            elements.add(element)
        scene, _ = store_mod.parse_shot_id(shot_id)
        if scene is not None and plan.get("scene") != scene:
            _issue(issues, "warn", "schema", shot_id,
                   "scene %r disagrees with shot id" % plan.get("scene"))

    for stage, required in (("image", IMAGE_REQUIRED), ("video", VIDEO_REQUIRED)):
        for shot_id in st.ids(stage):
            try:
                record = st.read(stage, shot_id)
            except ValueError as exc:
                _issue(issues, "error", "unparseable", shot_id,
                       "%s record: %s" % (stage, exc))
                continue
            for field in required:
                if field not in record:
                    _issue(issues, "error", "schema", shot_id,
                           "%s record missing %r" % (stage, field))
            if record.get("state") not in store_mod.STATES[stage]:
                _issue(issues, "error", "schema", shot_id,
                       "%s state %r is not a legal state" % (stage, record.get("state")))
            if shot_id not in plans:
                _issue(issues, "error", "orphan", shot_id,
                       "%s record has no plan entry" % stage)
            if record.get("attempts", 0) > st.attempt_cap:
                _issue(issues, "error", "cap_exceeded", shot_id,
                       "%s attempts %d over cap %d"
                       % (stage, record.get("attempts", 0), st.attempt_cap))
            if record.get("state") in ("done", "approved") and not record.get("media"):
                _issue(issues, "error", "no_media", shot_id,
                       "%s is %s with no media" % (stage, record.get("state")))
            media = record.get("media")
            if media and not os.path.exists(os.path.join(st.media_dir, media)):
                _issue(issues, "error", "missing_media", shot_id,
                       "%s media %r is not on disk" % (stage, media))
            for defect in record.get("defects", []) or []:
                for code in defect.get("codes", []):
                    if code not in codeset.BY_CODE:
                        _issue(issues, "warn", "unknown_code", shot_id,
                               "%s defect code %r is not in the registry"
                               % (stage, code))
            # Prompts address elements as {token}; a token with no element
            # behind it is a prompt that will silently generate the wrong thing.
            for token in ELEMENT_TOKEN.findall(record.get("prompt") or ""):
                if token not in elements:
                    _issue(issues, "error", "unknown_element", shot_id,
                           "%s prompt references element {%s} that no plan record "
                           "declares" % (stage, token))
            if stage == "video":
                image = st.read("image", shot_id)
                if image is None:
                    _issue(issues, "error", "handoff", shot_id,
                           "video record exists with no image record")
                elif image.get("state") != "approved":
                    _issue(issues, "error", "handoff", shot_id,
                           "video record exists but still is %r, not approved"
                           % image.get("state"))
                elif record.get("start_frame") != image.get("media"):
                    _issue(issues, "error", "stale_start_frame", shot_id,
                           "start_frame %r is no longer the approved still %r"
                           % (record.get("start_frame"), image.get("media")))
                for ref in record.get("references", []) or []:
                    if not ref.get("media"):
                        _issue(issues, "warn", "schema", shot_id,
                               "video reference %r has no media" % ref.get("role"))

    counts = {"error": 0, "warn": 0}
    for issue in issues:
        counts[issue["level"]] = counts.get(issue["level"], 0) + 1
    issues.sort(key=lambda i: (i["level"] != "error", i["shot_id"] or "", i["kind"]))
    return {"issues": issues, "counts": counts, "elements": sorted(elements),
            "valid": counts["error"] == 0}


def main():
    parser = argparse.ArgumentParser(description="AYNI manifest validator")
    parser.add_argument("--root", default=os.environ.get("AYNI_ROOT", "."))
    parser.add_argument("--json", action="store_true", help="machine-readable output")
    args = parser.parse_args()

    st = store_mod.Store(args.root)
    report = validate_manifest(st)
    if args.json:
        print(json.dumps(report, indent=2))
        return 0 if report["valid"] else 1
    for issue in report["issues"]:
        print("%-5s %-20s %-16s %s" % (issue["level"].upper(), issue["shot_id"] or "-",
                                       issue["kind"], issue["detail"]))
    print("\n%d error(s), %d warning(s), %d element(s) declared"
          % (report["counts"]["error"], report["counts"]["warn"],
             len(report["elements"])))
    return 0 if report["valid"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
