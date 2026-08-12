#!/usr/bin/env python3
"""Migration: existing shot plan -> shots/plan/*.json, one file per shot.

Accepts the shot plan as CSV or JSON. Plan records are immutable through
production, so this refuses to overwrite an existing record unless you ask for
it — a re-run after the plan document changes should be a deliberate act.

    python3 migrate.py --from shotplan.example.csv --root .
    python3 migrate.py --from shotplan.json --root . --force

Recognised columns (CSV header or JSON keys), case-insensitive:

    shot_id | id            S07_04. Derived from scene+slot if absent.
    scene                   integer scene number
    slot | shot | index     position within the scene
    tier                    A / B / C
    description | desc      what the shot is
    elements                semicolon- or comma-separated element tokens
    duration_target | dur   seconds, default 5
"""

import argparse
import csv
import json
import os
import sys

import codes as codeset
import store as store_mod

ALIASES = {
    "shot_id": ("shot_id", "shotid", "id"),
    "scene": ("scene", "sc", "scene_no", "scene_number"),
    "slot": ("slot", "shot", "index", "shot_no", "shot_number", "position"),
    "tier": ("tier", "priority"),
    "description": ("description", "desc", "action", "summary"),
    "elements": ("elements", "element", "refs", "references"),
    "duration_target": ("duration_target", "duration", "dur", "seconds", "length"),
}


def _pick(row, field):
    for alias in ALIASES[field]:
        for key in row:
            if key is None:
                continue
            if key.strip().lower() == alias:
                value = row[key]
                if value is None:
                    return None
                value = str(value).strip()
                return value or None
    return None


def _elements(raw):
    if raw is None:
        return []
    if isinstance(raw, list):
        return [str(e).strip() for e in raw if str(e).strip()]
    separator = ";" if ";" in raw else ","
    return [part.strip() for part in raw.split(separator) if part.strip()]


def read_rows(path):
    if path.lower().endswith(".json"):
        with open(path, encoding="utf-8") as fh:
            data = json.load(fh)
        if isinstance(data, dict):
            data = data.get("shots") or data.get("plan") or []
        return list(data)
    with open(path, newline="", encoding="utf-8-sig") as fh:
        return list(csv.DictReader(fh))


def build_records(rows):
    records, problems = [], []
    for number, row in enumerate(rows, start=2):
        row = {k: v for k, v in row.items()}
        shot_id = _pick(row, "shot_id")
        scene = _pick(row, "scene")
        slot = _pick(row, "slot")
        if not shot_id or not store_mod.SHOT_ID_RE.match(shot_id):
            if scene and slot:
                shot_id = store_mod.make_shot_id(int(scene), int(slot))
            elif not shot_id:
                problems.append("row %d: no shot_id and no scene+slot to derive one"
                                % number)
                continue
        try:
            store_mod.validate_shot_id(shot_id)
        except store_mod.StoreError as exc:
            problems.append("row %d: %s" % (number, exc))
            continue
        derived_scene, derived_slot = store_mod.parse_shot_id(shot_id)
        tier = (_pick(row, "tier") or "").upper() or None
        if tier and tier not in codeset.TIERS:
            problems.append("row %d (%s): tier %r is not A/B/C" % (number, shot_id, tier))
            tier = None
        duration = _pick(row, "duration_target")
        records.append({
            "shot_id": shot_id,
            "scene": int(scene) if scene else derived_scene,
            "tier": tier,
            "description": _pick(row, "description") or "",
            "elements": _elements(_pick(row, "elements")),
            "duration_target": int(float(duration)) if duration else 5,
            "_slot": int(slot) if slot else derived_slot,
        })
    seen = {}
    for record in records:
        seen.setdefault(record["shot_id"], []).append(record)
    for shot_id, group in seen.items():
        if len(group) > 1:
            problems.append("%s appears %d times in the plan" % (shot_id, len(group)))
    return records, problems


def migrate(source, root, force=False, dry_run=False):
    st = store_mod.Store(root)
    st.ensure_dirs()
    rows = read_rows(source)
    records, problems = build_records(rows)

    written, skipped = [], []
    for record in records:
        record.pop("_slot", None)
        path = st.path_for("plan", record["shot_id"])
        if os.path.exists(path) and not force:
            skipped.append(record["shot_id"])
            continue
        if not dry_run:
            st.write("plan", record)
        written.append(record["shot_id"])
    return {"written": written, "skipped": skipped, "problems": problems,
            "total": len(records), "root": st.root}


def main():
    parser = argparse.ArgumentParser(description="shot plan -> shots/plan/*.json")
    parser.add_argument("--from", dest="source", required=True,
                        help="shot plan CSV or JSON")
    parser.add_argument("--root", default=os.environ.get("AYNI_ROOT", "."))
    parser.add_argument("--force", action="store_true",
                        help="overwrite plan records that already exist")
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()

    result = migrate(args.source, args.root, args.force, args.dry_run)
    for problem in result["problems"]:
        print("problem: %s" % problem, file=sys.stderr)
    print("%d shot(s) in %s" % (result["total"], args.source))
    print("%d plan record(s) %s" % (len(result["written"]),
                                    "would be written" if args.dry_run else "written"))
    if result["skipped"]:
        print("%d already existed and were left alone (use --force to overwrite)"
              % len(result["skipped"]))
    return 1 if result["problems"] else 0


if __name__ == "__main__":
    raise SystemExit(main())
