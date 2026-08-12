#!/usr/bin/env python3
"""Build a demo project: a ~100-shot plan across 20 scenes, records in every
state the panel has to render, placeholder media on disk, and the awkward cases
the Coverage tab exists to catch.

Deliberately planted, so the acceptance criteria can be checked rather than
asserted:

  * a shot that is in the plan and nowhere else (the "deliberately removed shot")
  * a clip whose start frame is no longer the approved still
  * an orphan image record with no plan entry
  * a plan record with no tier
  * a shot sitting at 3/3 attempts, where regeneration must be unavailable

    python3 seed_demo.py --root . --reset
"""

import argparse
import csv
import os
import random
import shutil

import placeholder
import store as store_mod

SCENES = [
    (1, "Cold open — the terraces before dawn", ["ALTIPLANO", "TERRACE", "MIST"]),
    (2, "Nayra wakes in the stone house", ["NAYRA", "STONE_HOUSE", "OIL_LAMP"]),
    (3, "The water channel is dry", ["CHANNEL", "NAYRA", "DUST"]),
    (4, "Village assembly at the plaza", ["PLAZA", "ELDERS", "CROWD"]),
    (5, "Tupaq returns from the city", ["TUPAQ", "BUS", "ROAD"]),
    (6, "Brother and sister argue in the kitchen", ["NAYRA", "TUPAQ", "KITCHEN"]),
    (7, "The mine survey team arrives", ["SURVEYORS", "TRUCK", "THEODOLITE"]),
    (8, "Night — the offering at the apacheta", ["APACHETA", "NAYRA", "COCA"]),
    (9, "Digging the old canal by hand", ["CANAL", "VILLAGERS", "TOOLS"]),
    (10, "Rain that does not come", ["SKY", "TERRACE", "NAYRA"]),
    (11, "Tupaq signs the survey papers", ["TUPAQ", "SURVEYORS", "PAPERS"]),
    (12, "The herd moves to higher ground", ["ALPACAS", "HERDER", "RIDGE"]),
    (13, "Nayra confronts the foreman", ["NAYRA", "FOREMAN", "CAMP"]),
    (14, "The channel breaks open", ["CHANNEL", "WATER", "VILLAGERS"]),
    (15, "Funeral procession on the ridge", ["PROCESSION", "RIDGE", "CLOTH"]),
    (16, "The assembly votes", ["PLAZA", "ELDERS", "HANDS"]),
    (17, "Tupaq works the terrace alone", ["TUPAQ", "TERRACE", "HOE"]),
    (18, "First water down the new channel", ["CHANNEL", "WATER", "CHILDREN"]),
    (19, "Ayni — the exchange of labour", ["VILLAGERS", "TERRACE", "HARVEST"]),
    (20, "Last light on the altiplano", ["ALTIPLANO", "NAYRA", "TUPAQ"]),
]

ACTIONS = [
    "wide establishing, low sun raking the terrace walls",
    "medium on {who}, wind moving the shawl",
    "close on hands working the earth",
    "over-shoulder toward the far ridge",
    "insert — water finding the channel lip",
    "low angle, figures against a bleached sky",
    "tracking behind {who} along the wall",
    "tight on {who}'s face, holding the beat",
    "texture — dust lifting off dry stone",
    "two-shot, neither of them looking at the other",
    "high wide, the village small in the frame",
    "insert — coca leaves on wet cloth",
]


def build_plan(store, rows_out=None):
    random.seed(1701)
    rows = []
    for scene, title, elements in SCENES:
        for slot in range(1, random.randint(3, 7) + 1):
            shot_id = store_mod.make_shot_id(scene, slot)
            tier = random.choices(["A", "B", "C"], weights=[2, 5, 3])[0]
            action = random.choice(ACTIONS).replace(
                "{who}", elements[0].title().replace("_", " "))
            rows.append({
                "shot_id": shot_id,
                "scene": scene,
                "slot": slot,
                "tier": tier,
                "description": "%s — %s" % (title, action),
                "elements": ";".join(elements),
                "duration_target": random.choice([3, 4, 5, 5, 6, 8]),
            })
    if rows_out:
        with open(rows_out, "w", newline="", encoding="utf-8") as fh:
            writer = csv.DictWriter(fh, fieldnames=list(rows[0].keys()))
            writer.writeheader()
            writer.writerows(rows)
    return rows


def seed(root, reset=False):
    st = store_mod.Store(root)
    if reset:
        for directory in ("shots", "media", "runner", "exports"):
            path = os.path.join(st.root, directory)
            if os.path.isdir(path):
                shutil.rmtree(path)
    st.ensure_dirs()

    rows = build_plan(st, os.path.join(st.root, "shotplan.example.csv"))
    random.seed(88)

    for row in rows:
        st.write("plan", st.new_plan(
            row["shot_id"], row["scene"], row["tier"], row["description"],
            row["elements"].split(";"), row["duration_target"]))

    shot_ids = [row["shot_id"] for row in rows]
    plans = {row["shot_id"]: row for row in rows}

    # An untiered plan record — Coverage should name it.
    untiered = shot_ids[len(shot_ids) // 3]
    plan = st.read("plan", untiered)
    plan["tier"] = None
    st.write("plan", plan)

    # A shot that exists in the plan and nowhere else. Coverage has to find this
    # without being told which one it is.
    removed = shot_ids[7]

    for shot_id in shot_ids:
        if shot_id == removed:
            continue
        row = plans[shot_id]
        roll = random.random()
        image = st.new_image(shot_id, prompt=_image_prompt(row), resolution="2k")

        if roll < 0.10:                       # prompted, not yet run
            st.write("image", image)
            continue
        if roll < 0.16:                       # sitting in the runner queue
            image["state"] = "queued"
            image["queued_kind"] = "regenerate"
            image["queued_at"] = store_mod.now()
            image["attempts"] = 1
            st.write("image", image)
            continue

        image["attempts"] = 1
        image["credits_spent"] = st.estimate("image", image)
        image["media"] = _still(st, shot_id, row, 1)
        image["state"] = "done"
        image["history"] = [{"at": store_mod.now(), "event": "generated",
                             "kind": "regenerate", "media": image["media"],
                             "credits": image["credits_spent"], "attempt": 1,
                             "driver": "sim"}]

        if roll < 0.42:                       # the still review queue
            st.write("image", image)
            continue

        if roll < 0.52:                       # rejected once, re-rolled, done again
            code = random.choice(["I1", "I2", "I4", "I7", "I9"])
            image["defects"].append({"at": store_mod.now(), "codes": [code],
                                     "note": "", "attempt": 1})
            image["attempts"] = 2
            image["credits_spent"] += st.estimate("image", image)
            image["media"] = _still(st, shot_id, row, 2)
            st.write("image", image)
            continue

        if roll < 0.56:                       # burnt through the cap
            for attempt in (1, 2, 3):
                image["defects"].append({
                    "at": store_mod.now(), "codes": [random.choice(["I1", "I2"])],
                    "note": "", "attempt": attempt})
            image["attempts"] = 3
            image["credits_spent"] = st.estimate("image", image) * 3
            image["media"] = _still(st, shot_id, row, 3)
            image["state"] = "rejected"
            st.write("image", image)
            continue

        # approved — opens the video stage
        image["state"] = "approved"
        st.write("image", image)
        video = st.new_video(shot_id, start_frame=image["media"],
                             prompt=_video_prompt(row),
                             duration=row["duration_target"])
        vroll = random.random()
        if vroll < 0.18:
            st.write("video", video)
            continue
        if vroll < 0.26:
            video["state"] = "queued"
            video["queued_kind"] = "regenerate"
            video["queued_at"] = store_mod.now()
            video["attempts"] = 1
            st.write("video", video)
            continue

        video["attempts"] = 1
        video["credits_spent"] = st.estimate("video", video)
        video["media"] = _clip(st, shot_id, row, 1)
        video["state"] = "done"
        video["history"] = [{"at": store_mod.now(), "event": "generated",
                             "kind": "regenerate", "media": video["media"],
                             "credits": video["credits_spent"], "attempt": 1,
                             "driver": "sim"}]
        if vroll < 0.72:                      # the clip review queue
            st.write("video", video)
            continue
        if vroll < 0.82:
            code = random.choice(["V1", "V4", "V5", "V7"])
            video["defects"].append({"at": store_mod.now(), "codes": [code],
                                     "note": "", "attempt": 1})
            video["state"] = "rejected"
            st.write("video", video)
            continue
        video["state"] = "approved"
        st.write("video", video)

    _plant_stale_start_frame(st, shot_ids)
    _plant_orphan(st)

    return {
        "root": st.root,
        "planned": len(st.ids("plan")),
        "images": len(st.ids("image")),
        "videos": len(st.ids("video")),
        "removed_shot": removed,
        "untiered_shot": untiered,
    }


def _plant_stale_start_frame(st, shot_ids):
    """A clip whose still was re-approved underneath it. The clip is built on a
    frame that is no longer the shot, and only Coverage can see that."""
    for shot_id in shot_ids:
        video = st.read("video", shot_id)
        image = st.read("image", shot_id)
        if not video or not image or image.get("state") != "approved":
            continue
        if not video.get("media"):
            continue
        image["attempts"] = 2
        image["media"] = _still(st, shot_id, {"description": "re-approved still",
                                              "tier": "A"}, 9)
        image["credits_spent"] += st.estimate("image", image)
        image["history"].append({"at": store_mod.now(), "event": "generated",
                                 "kind": "regenerate", "media": image["media"],
                                 "credits": st.estimate("image", image),
                                 "attempt": 2, "driver": "sim"})
        image["history"].append({"at": store_mod.now(), "event": "approve",
                                 "media": image["media"]})
        st.write("image", image)
        return shot_id
    return None


def _plant_orphan(st):
    orphan = "S99_01"
    record = st.new_image(orphan, prompt="orphan record with no plan entry")
    record["state"] = "done"
    record["attempts"] = 1
    record["media"] = _still(st, orphan, {"description": "orphan", "tier": "C"}, 1)
    st.write("image", record)
    return orphan


def _image_prompt(row):
    return ("%s. Cinema Studio 2.5, anamorphic 2.39:1, altiplano daylight, "
            "elements: %s" % (row["description"], row["elements"].replace(";", ", ")))


def _video_prompt(row):
    return ("From the approved still: %s. Seedance 2.5, %ss, single sustained move, "
            "no cut." % (row["description"].split("—")[-1].strip(),
                         row["duration_target"]))


def _still(st, shot_id, row, attempt):
    relative = "images/%s_a%s.svg" % (shot_id, attempt)
    placeholder.make_still(os.path.join(st.media_dir, relative), shot_id,
                           row.get("description", "")[:48], row.get("tier") or "",
                           attempt=attempt)
    return relative


def _clip(st, shot_id, row, attempt):
    relative = "videos/%s_a%s.svg" % (shot_id, attempt)
    placeholder.make_clip(os.path.join(st.media_dir, relative), shot_id,
                          row.get("description", "")[:48], row.get("tier") or "",
                          attempt=attempt, seconds=row.get("duration_target", 5))
    return relative


def main():
    parser = argparse.ArgumentParser(description="seed a demo AYNI project")
    parser.add_argument("--root", default=os.environ.get("AYNI_ROOT", "."))
    parser.add_argument("--reset", action="store_true",
                        help="delete shots/, media/, runner/ and exports/ first")
    args = parser.parse_args()
    result = seed(args.root, args.reset)
    print("root            %s" % result["root"])
    print("plan records    %d" % result["planned"])
    print("image records   %d" % result["images"])
    print("video records   %d" % result["videos"])
    print("planted gaps    %s has no image record · %s has no tier · S99_01 is an orphan"
          % (result["removed_shot"], result["untiered_shot"]))
    print("\nnext: python3 server.py --root %s" % result["root"])


if __name__ == "__main__":
    main()
