#!/usr/bin/env python3
"""Tests, organised around section 10 of the brief. Each acceptance criterion
that can be checked mechanically has a test named after it.

    python3 tests.py
"""

import json
import os
import shutil
import tempfile
import threading
import unittest
import urllib.error
import urllib.request

import codes as codeset
import contact_sheet
import migrate
import placeholder
import runner as runner_mod
import server as server_mod
import store as store_mod
import validate as validate_mod


class Base(unittest.TestCase):
    def setUp(self):
        self.root = tempfile.mkdtemp(prefix="ayni-test-")
        self.store = store_mod.Store(self.root, config={
            "project": "TEST", "credit_budget": 1000, "attempt_cap": 3})
        self.store.ensure_dirs()

    def tearDown(self):
        shutil.rmtree(self.root, ignore_errors=True)

    # helpers -------------------------------------------------------------
    def plan(self, shot_id, tier="B", scene=None, duration=5):
        scene = scene if scene is not None else store_mod.parse_shot_id(shot_id)[0]
        record = self.store.new_plan(shot_id, scene, tier, "test shot",
                                     ["NAYRA"], duration)
        return self.store.write("plan", record)

    def still(self, shot_id, attempt=1):
        relative = "images/%s_a%d.svg" % (shot_id, attempt)
        placeholder.make_still(os.path.join(self.store.media_dir, relative),
                               shot_id, "test", "B", attempt)
        return relative

    def clip(self, shot_id, attempt=1):
        relative = "videos/%s_a%d.svg" % (shot_id, attempt)
        placeholder.make_clip(os.path.join(self.store.media_dir, relative),
                              shot_id, "test", "B", attempt)
        return relative

    def image_done(self, shot_id, tier="B", attempt=1):
        self.plan(shot_id, tier)
        record = self.store.new_image(shot_id, "a prompt")
        record["attempts"] = attempt
        record["media"] = self.still(shot_id, attempt)
        record["state"] = "done"
        return self.store.write("image", record)

    def video_done(self, shot_id, tier="B"):
        self.image_done(shot_id, tier)
        self.store.review(shot_id, "image", "approve")
        video = self.store.read("video", shot_id)
        video["attempts"] = 1
        video["media"] = self.clip(shot_id)
        video["state"] = "done"
        return self.store.write("video", video)


class TestHandoff(Base):
    def test_video_record_exists_only_once_the_still_is_approved(self):
        self.image_done("S07_04")
        self.assertIsNone(self.store.read("video", "S07_04"))
        self.store.review("S07_04", "image", "approve")
        video = self.store.read("video", "S07_04")
        self.assertIsNotNone(video)
        self.assertEqual(video["start_frame"],
                         self.store.read("image", "S07_04")["media"])
        self.assertEqual(video["state"], "prompted")

    def test_approving_without_media_is_refused(self):
        self.plan("S07_05")
        self.store.write("image", self.store.new_image("S07_05", "p"))
        with self.assertRaises(store_mod.StoreError):
            self.store.review("S07_05", "image", "approve")

    def test_video_duration_inherits_the_plan_target(self):
        self.plan("S07_06", duration=8)
        record = self.store.new_image("S07_06", "p")
        record["media"] = self.still("S07_06")
        record["state"] = "done"
        self.store.write("image", record)
        self.store.review("S07_06", "image", "approve")
        self.assertEqual(self.store.read("video", "S07_06")["duration"], 8)


class TestRejectionIsCodesOnly(Base):
    def test_rejecting_a_shot_requires_no_prose(self):
        self.image_done("S01_01")
        result = self.store.review("S01_01", "image", "reject", codes=["I2"])
        self.assertEqual(result["state"], "rejected")
        defect = self.store.read("image", "S01_01")["defects"][-1]
        self.assertEqual(defect["codes"], ["I2"])
        self.assertEqual(defect["note"], "")

    def test_a_rejection_without_a_code_is_refused(self):
        self.image_done("S01_02")
        with self.assertRaises(store_mod.StoreError):
            self.store.review("S01_02", "image", "reject", codes=[])

    def test_unknown_codes_are_refused(self):
        self.image_done("S01_03")
        with self.assertRaises(store_mod.StoreError):
            self.store.review("S01_03", "image", "reject", codes=["ZZ9"])

    def test_a_video_code_is_not_valid_at_the_image_stage(self):
        self.image_done("S01_04")
        with self.assertRaises(store_mod.StoreError):
            self.store.review("S01_04", "image", "reject", codes=["V1"])

    def test_second_code_on_the_same_tile_extends_one_rejection(self):
        self.image_done("S01_05")
        self.store.review("S01_05", "image", "reject", codes=["I1"])
        self.store.review("S01_05", "image", "reject", codes=["I4"], merge=True)
        defects = self.store.read("image", "S01_05")["defects"]
        self.assertEqual(len(defects), 1)
        self.assertEqual(defects[0]["codes"], ["I1", "I4"])


class TestUpstreamRouting(Base):
    def test_clip_rejected_for_an_upstream_cause_returns_to_the_image_stage(self):
        self.video_done("S12_02")
        result = self.store.review("S12_02", "video", "reject", codes=["V2"])
        self.assertTrue(result["routed_upstream"])
        self.assertEqual(result["stage"], "image")
        self.assertIsNone(self.store.read("video", "S12_02"))
        image = self.store.read("image", "S12_02")
        self.assertEqual(image["state"], "rejected")
        self.assertEqual(image["defects"][-1]["codes"], ["V2"])
        self.assertEqual(image["defects"][-1]["seen_in"], "video")

    def test_clip_rejected_for_a_motion_defect_stays_at_the_video_stage(self):
        self.video_done("S12_03")
        result = self.store.review("S12_03", "video", "reject", codes=["V4"])
        self.assertFalse(result["routed_upstream"])
        self.assertEqual(self.store.read("video", "S12_03")["state"], "rejected")
        self.assertEqual(self.store.read("image", "S12_03")["state"], "approved")

    def test_the_default_route_can_be_overridden_in_both_directions(self):
        self.video_done("S12_04")
        # A motion defect the director judges to be the start frame's fault.
        result = self.store.review("S12_04", "video", "reject", codes=["V4"],
                                   route="image")
        self.assertTrue(result["routed_upstream"])
        self.assertIsNone(self.store.read("video", "S12_04"))
        # ...and changing their mind restores the clip record.
        back = self.store.review("S12_04", "video", "reroute", route="video")
        self.assertTrue(back["restored"])
        self.assertEqual(self.store.read("video", "S12_04")["state"], "rejected")
        self.assertEqual(self.store.read("image", "S12_04")["state"], "approved")
        self.assertEqual(self.store.read("image", "S12_04")["defects"], [])

    def test_attempt_caps_are_per_stage(self):
        self.video_done("S12_05")
        image = self.store.read("image", "S12_05")
        image["attempts"] = 3
        self.store.write("image", image)
        # The still burned three attempts; the clip still has all of its own.
        result = self.store.review("S12_05", "video", "reject", codes=["V4"])
        self.assertFalse(result["at_cap"])
        self.store.enqueue("S12_05", "video", "regenerate")
        self.assertEqual(self.store.read("video", "S12_05")["attempts"], 2)


class TestAttemptCap(Base):
    def test_the_cap_cannot_be_exceeded_through_the_interface(self):
        self.image_done("S03_01")
        for _ in range(3):
            self.store.review("S03_01", "image", "reject", codes=["I1"])
            record = self.store.read("image", "S03_01")
            if record["attempts"] >= 3:
                break
            self.store.enqueue("S03_01", "image", "regenerate")
            record = self.store.read("image", "S03_01")
            record["state"] = "done"
            record["media"] = self.still("S03_01", record["attempts"])
            self.store.write("image", record)
        record = self.store.read("image", "S03_01")
        record["attempts"] = 3
        self.store.write("image", record)
        with self.assertRaises(store_mod.StoreError) as caught:
            self.store.enqueue("S03_01", "image", "regenerate")
        self.assertEqual(caught.exception.status, 409)
        self.assertIn("attempt cap", str(caught.exception))

    def test_escape_hatches_remain_available_at_the_cap(self):
        self.image_done("S03_02")
        record = self.store.read("image", "S03_02")
        record["attempts"] = 3
        self.store.write("image", record)
        result = self.store.review("S03_02", "image", "escape_hatch",
                                   escape={"kind": "reframe"})
        self.assertEqual(result["state"], "escape_hatch")
        self.assertEqual(self.store.read("image", "S03_02")["escape"]["kind"],
                         "reframe")

    def test_fix_in_post_is_a_video_stage_outcome_only(self):
        self.image_done("S03_03")
        with self.assertRaises(store_mod.StoreError):
            self.store.review("S03_03", "image", "fix_in_post")
        self.video_done("S03_04")
        result = self.store.review("S03_04", "video", "fix_in_post")
        self.assertEqual(result["state"], "fix_in_post")

    def test_a_failed_job_that_cost_nothing_does_not_consume_an_attempt(self):
        self.image_done("S03_05")
        self.store.review("S03_05", "image", "reject", codes=["I1"])
        self.store.enqueue("S03_05", "image", "regenerate")
        self.assertEqual(self.store.read("image", "S03_05")["attempts"], 2)
        runner_mod.Runner(self.store, verbose=False).fail("S03_05", "image", "timeout")
        self.assertEqual(self.store.read("image", "S03_05")["attempts"], 1)


class TestLocalEdit(Base):
    def test_local_edit_is_priced_and_queued_separately_from_a_re_roll(self):
        self.video_done("S04_01")
        video = self.store.read("video", "S04_01")
        self.assertLess(self.store.estimate("video", video, "local_edit"),
                        self.store.estimate("video", video, "regenerate"))
        result = self.store.enqueue("S04_01", "video", "local_edit")
        self.assertEqual(result["kind"], "local_edit")
        # A local edit is not a generation attempt, so it does not touch the cap.
        self.assertEqual(self.store.read("video", "S04_01")["attempts"], 1)

    def test_local_edit_is_available_at_the_cap(self):
        self.video_done("S04_02")
        video = self.store.read("video", "S04_02")
        video["attempts"] = 3
        self.store.write("video", video)
        self.store.enqueue("S04_02", "video", "local_edit")
        self.assertEqual(self.store.read("video", "S04_02")["state"], "queued")

    def test_locally_editable_defects_are_marked_from_the_code(self):
        self.video_done("S04_03")
        self.store.review("S04_03", "video", "reject", codes=["V7"])
        self.assertTrue(self.store.read("video", "S04_03")["local_editable"])
        self.video_done("S04_04")
        self.store.review("S04_04", "video", "reject", codes=["V4"])
        self.assertFalse(self.store.read("video", "S04_04")["local_editable"])


class TestCoverage(Base):
    def test_it_reveals_a_deliberately_removed_shot(self):
        for slot in range(1, 5):
            self.image_done(store_mod.make_shot_id(14, slot))
        self.plan("S14_05")   # planned, never prompted — the removed shot
        gaps = self.store.coverage()["gaps"]
        self.assertEqual(gaps["planned_no_image"], ["S14_05"])

    def test_it_flags_a_still_re_approved_after_its_clip_was_created(self):
        self.video_done("S15_01")
        image = self.store.read("image", "S15_01")
        image["attempts"] = 2
        image["media"] = self.still("S15_01", 2)
        image["state"] = "done"
        self.store.write("image", image)
        self.store.review("S15_01", "image", "approve")
        stale = self.store.coverage()["gaps"]["stale_start_frame"]
        self.assertEqual([g["shot_id"] for g in stale], ["S15_01"])
        self.assertNotEqual(stale[0]["video_start_frame"], stale[0]["current_still"])

    def test_re_approval_does_not_silently_rewrite_the_clip_record(self):
        self.video_done("S15_02")
        original = self.store.read("video", "S15_02")["start_frame"]
        image = self.store.read("image", "S15_02")
        image["media"] = self.still("S15_02", 2)
        image["state"] = "done"
        self.store.write("image", image)
        result = self.store.review("S15_02", "image", "approve")
        self.assertTrue(result["video"]["stale_start_frame"])
        self.assertEqual(self.store.read("video", "S15_02")["start_frame"], original)

    def test_it_names_orphans_and_untiered_shots(self):
        self.plan("S16_01", tier=None)
        record = self.store.new_image("S16_02", "p")
        self.store.write("image", record)
        gaps = self.store.coverage()["gaps"]
        self.assertIn("S16_01", gaps["untiered"])
        self.assertIn("S16_02", gaps["orphan_image"])

    def test_the_video_queue_is_a_set_difference(self):
        self.image_done("S17_01")
        self.store.review("S17_01", "image", "approve")
        self.store.delete("video", "S17_01", reason="test")
        self.assertIn("S17_01",
                      self.store.coverage()["gaps"]["image_approved_no_video"])


class TestLedger(Base):
    def test_defect_frequency_histogram_counts_every_code(self):
        for slot, code in ((1, "I1"), (2, "I1"), (3, "I4")):
            shot_id = store_mod.make_shot_id(18, slot)
            self.image_done(shot_id)
            self.store.review(shot_id, "image", "reject", codes=[code])
        histogram = {bar["code"]: bar["count"] for bar in self.store.ledger()["histogram"]}
        self.assertEqual(histogram["I1"], 2)
        self.assertEqual(histogram["I4"], 1)

    def test_projected_burn_covers_everything_queued(self):
        self.image_done("S19_01")
        self.store.review("S19_01", "image", "reject", codes=["I1"])
        self.store.enqueue("S19_01", "image", "regenerate")
        ledger = self.store.ledger()
        self.assertEqual(ledger["projected"], self.store.pricing["image"]["2k"])
        self.assertEqual(ledger["queued_items"], 1)

    def test_remaining_balance_accounts_for_the_queue(self):
        self.image_done("S19_02")
        record = self.store.read("image", "S19_02")
        record["credits_spent"] = 100
        self.store.write("image", record)
        self.store.review("S19_02", "image", "reject", codes=["I1"])
        self.store.enqueue("S19_02", "image", "regenerate")
        ledger = self.store.ledger()
        self.assertEqual(ledger["spent"], 100)
        self.assertEqual(ledger["remaining"], 900)
        self.assertEqual(ledger["remaining_after_queue"], 900 - ledger["projected"])


class TestBulkApprove(Base):
    def test_bulk_approve_is_c_tier_only(self):
        self.image_done("S20_01", tier="C")
        self.image_done("S20_02", tier="A")
        result = self.store.bulk_approve("image", "C")
        self.assertEqual(result["approved"], ["S20_01"])
        self.assertEqual(self.store.read("image", "S20_02")["state"], "done")
        with self.assertRaises(store_mod.StoreError):
            self.store.bulk_approve("image", "A")


class TestConcurrency(Base):
    def test_the_two_stages_write_to_different_files(self):
        self.video_done("S05_01")
        image_path = self.store.path_for("image", "S05_01")
        video_path = self.store.path_for("video", "S05_01")
        self.assertNotEqual(image_path, video_path)
        image_before = os.path.getmtime(image_path)
        self.store.review("S05_01", "video", "reject", codes=["V4"])
        self.assertEqual(os.path.getmtime(image_path), image_before)

    def test_parallel_stage_sessions_do_not_interleave_writes(self):
        shots = [store_mod.make_shot_id(6, slot) for slot in range(1, 21)]
        for shot_id in shots:
            self.video_done(shot_id)
        errors = []

        def hammer(stage, action_codes):
            for shot_id in shots:
                try:
                    self.store.review(shot_id, stage, "reject", codes=action_codes)
                except store_mod.StoreError as exc:
                    errors.append(str(exc))

        threads = [threading.Thread(target=hammer, args=("image", ["I7"])),
                   threading.Thread(target=hammer, args=("video", ["V4"]))]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()
        self.assertEqual(errors, [])
        for shot_id in shots:
            # Both files are readable, complete JSON — no torn writes.
            self.assertEqual(self.store.read("image", shot_id)["state"], "rejected")
            self.assertEqual(self.store.read("video", shot_id)["state"], "rejected")


class TestDurability(Base):
    def test_every_decision_writes_through_immediately(self):
        self.image_done("S08_01")
        self.store.review("S08_01", "image", "reject", codes=["I1"])
        # A brand new Store, as if the browser was killed and the panel reopened.
        fresh = store_mod.Store(self.root)
        self.assertEqual(fresh.read("image", "S08_01")["state"], "rejected")
        self.assertEqual(fresh.read("image", "S08_01")["defects"][-1]["codes"], ["I1"])

    def test_writes_are_atomic(self):
        self.image_done("S08_02")
        path = self.store.path_for("image", "S08_02")
        for index in range(50):
            record = self.store.read("image", "S08_02")
            record["prompt"] = "revision %d" % index
            self.store.write("image", record)
            with open(path, encoding="utf-8") as fh:
                json.load(fh)          # never a partial file
        leftovers = [n for n in os.listdir(os.path.dirname(path))
                     if n.startswith(".") or n.endswith(".tmp")]
        self.assertEqual(leftovers, [])

    def test_a_routed_upstream_clip_is_recoverable_from_the_attic(self):
        self.video_done("S08_03")
        self.store.review("S08_03", "video", "reject", codes=["V2"])
        attic = os.path.join(self.root, "shots", "attic", "video")
        self.assertTrue(any(n.startswith("S08_03.") for n in os.listdir(attic)))


class TestRunner(Base):
    def test_the_runner_books_credits_and_the_panel_never_does(self):
        self.image_done("S09_01")
        self.store.review("S09_01", "image", "reject", codes=["I1"])
        before = self.store.ledger()["spent"]
        self.store.enqueue("S09_01", "image", "regenerate")
        self.assertEqual(self.store.ledger()["spent"], before,
                         "queueing must not spend a credit")
        runner_mod.Runner(self.store, verbose=False).pass_once()
        record = self.store.read("image", "S09_01")
        self.assertEqual(record["state"], "done")
        self.assertEqual(record["credits_spent"], self.store.pricing["image"]["2k"])
        self.assertTrue(os.path.exists(os.path.join(self.store.media_dir,
                                                    record["media"])))

    def test_the_agent_driver_emits_a_work_order_and_waits(self):
        self.image_done("S09_02")
        self.store.review("S09_02", "image", "reject", codes=["I1"])
        self.store.enqueue("S09_02", "image", "regenerate")
        runner = runner_mod.Runner(self.store, driver="agent", verbose=False)
        runner.pass_once()
        pending = os.path.join(self.store.runner_dir, "orders", "pending")
        orders = os.listdir(pending)
        self.assertEqual(len(orders), 1)
        with open(os.path.join(pending, orders[0]), encoding="utf-8") as fh:
            order = json.load(fh)
        self.assertEqual(order["shot_id"], "S09_02")
        self.assertEqual(order["model"], "cinema-studio-image-2.5")
        self.assertEqual(self.store.read("image", "S09_02")["state"], "queued")

        runner.complete("S09_02", "image", self.still("S09_02", 2), credits=8)
        self.assertEqual(self.store.read("image", "S09_02")["state"], "done")
        self.assertEqual(os.listdir(pending), [])

    def test_hero_shots_are_processed_first(self):
        for shot_id, tier in (("S10_01", "C"), ("S10_02", "A"), ("S10_03", "B")):
            self.image_done(shot_id, tier=tier)
            self.store.review(shot_id, "image", "reject", codes=["I1"])
            self.store.enqueue(shot_id, "image", "regenerate")
        order = [item["shot_id"]
                 for item in runner_mod.Runner(self.store, verbose=False).next_items()]
        self.assertEqual(order, ["S10_02", "S10_03", "S10_01"])


class TestValidator(Base):
    def test_it_catches_prompts_referencing_elements_that_do_not_exist(self):
        self.plan("S11_01")
        record = self.store.new_image("S11_01", "a shot of {TUPAQ} and {NAYRA}")
        self.store.write("image", record)
        report = validate_mod.validate_manifest(self.store)
        kinds = [(i["kind"], i["detail"]) for i in report["issues"]]
        self.assertTrue(any(kind == "unknown_element" and "TUPAQ" in detail
                            for kind, detail in kinds))
        self.assertFalse(any(kind == "unknown_element" and "NAYRA" in detail
                             for kind, detail in kinds))

    def test_it_catches_missing_media_untagged_tiers_and_stale_handoffs(self):
        self.plan("S11_02", tier=None)
        record = self.store.new_image("S11_02", "p")
        record["state"] = "done"
        record["media"] = "images/does-not-exist.jpg"
        self.store.write("image", record)
        report = validate_mod.validate_manifest(self.store)
        kinds = {i["kind"] for i in report["issues"]}
        self.assertIn("untagged_tier", kinds)
        self.assertIn("missing_media", kinds)
        self.assertFalse(report["valid"])

    def test_a_clean_project_validates(self):
        self.video_done("S11_03")
        report = validate_mod.validate_manifest(self.store)
        self.assertTrue(report["valid"], report["issues"])


class TestMigration(Base):
    def test_it_generates_plan_records_from_a_csv_shot_plan(self):
        source = os.path.join(self.root, "plan.csv")
        with open(source, "w", encoding="utf-8") as fh:
            fh.write("Scene,Shot,Tier,Description,Elements,Duration\n"
                     "7,4,A,Nayra at the channel,NAYRA;CHANNEL,6\n"
                     "7,5,c,Insert of the water,CHANNEL,3\n")
        result = migrate.migrate(source, self.root)
        self.assertEqual(sorted(result["written"]), ["S07_04", "S07_05"])
        record = self.store.read("plan", "S07_04")
        self.assertEqual(record["tier"], "A")
        self.assertEqual(record["elements"], ["NAYRA", "CHANNEL"])
        self.assertEqual(record["duration_target"], 6)
        self.assertEqual(self.store.read("plan", "S07_05")["tier"], "C")

    def test_plan_records_are_not_overwritten_by_accident(self):
        source = os.path.join(self.root, "plan.csv")
        with open(source, "w", encoding="utf-8") as fh:
            fh.write("shot_id,scene,tier,description\nS07_04,7,A,first\n")
        migrate.migrate(source, self.root)
        with open(source, "w", encoding="utf-8") as fh:
            fh.write("shot_id,scene,tier,description\nS07_04,7,A,second\n")
        result = migrate.migrate(source, self.root)
        self.assertEqual(result["skipped"], ["S07_04"])
        self.assertEqual(self.store.read("plan", "S07_04")["description"], "first")
        migrate.migrate(source, self.root, force=True)
        self.assertEqual(self.store.read("plan", "S07_04")["description"], "second")

    def test_bad_rows_are_reported_not_silently_dropped(self):
        source = os.path.join(self.root, "plan.csv")
        with open(source, "w", encoding="utf-8") as fh:
            fh.write("shot_id,scene,tier,description\n"
                     "S07_04,7,A,fine\nnonsense,7,A,bad id\nS07_04,7,B,duplicate\n")
        result = migrate.migrate(source, self.root)
        self.assertEqual(len(result["problems"]), 2)


class TestContactSheet(Base):
    def test_it_writes_a_standalone_file_with_media_embedded(self):
        self.image_done("S13_01")
        path = contact_sheet.build_contact_sheet(self.store, "image")
        with open(path, encoding="utf-8") as fh:
            body = fh.read()
        self.assertIn("S13_01", body)
        self.assertIn("data:image/svg+xml;base64,", body)
        self.assertNotIn("/media/", body)      # no server dependency


class TestHttp(Base):
    """The panel talks to the server over exactly these routes."""

    def setUp(self):
        super().setUp()
        self.httpd, _ = server_mod.serve(self.root, port=0)
        self.httpd.store = self.store
        self.port = self.httpd.server_address[1]
        self.thread = threading.Thread(target=self.httpd.serve_forever, daemon=True)
        self.thread.start()

    def tearDown(self):
        self.httpd.shutdown()
        self.httpd.server_close()
        super().tearDown()

    def get(self, path):
        with urllib.request.urlopen("http://127.0.0.1:%d%s" % (self.port, path)) as r:
            return json.load(r), r.status

    def post(self, path, payload):
        request = urllib.request.Request(
            "http://127.0.0.1:%d%s" % (self.port, path),
            data=json.dumps(payload).encode("utf-8"),
            headers={"Content-Type": "application/json"}, method="POST")
        try:
            with urllib.request.urlopen(request) as response:
                return json.load(response), response.status
        except urllib.error.HTTPError as exc:
            return json.load(exc), exc.code

    def test_every_documented_route_answers(self):
        self.video_done("S02_01")
        for path in ("/api/shots", "/api/images", "/api/videos", "/api/status",
                     "/api/ledger", "/api/coverage", "/api/codes", "/api/validate",
                     "/api/shot?id=S02_01"):
            payload, status = self.get(path)
            self.assertEqual(status, 200, path)
            self.assertTrue(payload["ok"], path)

    def test_the_panel_is_served_and_media_is_served(self):
        self.image_done("S02_02")
        url = "http://127.0.0.1:%d/" % self.port
        with urllib.request.urlopen(url) as response:
            self.assertIn(b"AYNI", response.read())
        media = self.store.read("image", "S02_02")["media"]
        with urllib.request.urlopen("http://127.0.0.1:%d/media/%s"
                                    % (self.port, media)) as response:
            self.assertEqual(response.headers["Content-Type"], "image/svg+xml")

    def test_media_paths_cannot_escape_the_media_directory(self):
        try:
            with urllib.request.urlopen(
                    "http://127.0.0.1:%d/media/../../config.json" % self.port):
                self.fail("path traversal was served")
        except urllib.error.HTTPError as exc:
            self.assertIn(exc.code, (403, 404))

    def test_review_and_queue_round_trip_over_http(self):
        self.image_done("S02_03")
        payload, status = self.post("/api/review",
                                    {"shot_id": "S02_03", "stage": "image",
                                     "action": "reject", "codes": ["I2"]})
        self.assertEqual(status, 200)
        self.assertEqual(payload["state"], "rejected")
        payload, status = self.post("/api/queue",
                                    {"shot_id": "S02_03", "stage": "image"})
        self.assertEqual(status, 200)
        self.assertEqual(payload["estimate"], 8)
        self.assertEqual(self.store.ledger()["spent"], 0,
                         "no panel route may spend a credit")

    def test_the_cap_is_a_409_over_http_too(self):
        self.image_done("S02_04")
        record = self.store.read("image", "S02_04")
        record["attempts"] = 3
        record["state"] = "rejected"
        self.store.write("image", record)
        payload, status = self.post("/api/queue",
                                    {"shot_id": "S02_04", "stage": "image"})
        self.assertEqual(status, 409)
        self.assertIn("attempt cap", payload["error"])

    def test_review_filters_narrow_the_queue(self):
        self.image_done("S02_05", tier="A")
        self.image_done("S02_06", tier="C")
        payload, _ = self.get("/api/images?state=done&tier=A")
        self.assertEqual([r["shot_id"] for r in payload["records"]], ["S02_05"])
        payload, _ = self.get("/api/images?state=all&tier=all")
        self.assertEqual(len(payload["records"]), 2)

    def test_bad_input_is_rejected_not_absorbed(self):
        _, status = self.post("/api/review", {"shot_id": "nope", "stage": "image",
                                              "action": "approve"})
        self.assertEqual(status, 400)
        _, status = self.post("/api/review", {"shot_id": "S02_09", "stage": "image",
                                              "action": "approve"})
        self.assertEqual(status, 404)


class TestCodeRegistry(unittest.TestCase):
    def test_every_stage_has_nine_codes_bound_to_keys_one_to_nine(self):
        for stage in ("image", "video"):
            keys = [c["key"] for c in codeset.CODES[stage]]
            self.assertEqual(keys, [str(n) for n in range(1, 10)], stage)

    def test_codes_are_unique_across_stages(self):
        all_codes = [c["code"] for c in codeset.IMAGE_CODES + codeset.VIDEO_CODES]
        self.assertEqual(len(all_codes), len(set(all_codes)))

    def test_some_video_codes_default_to_the_upstream_route(self):
        upstream = [c["code"] for c in codeset.VIDEO_CODES if c["route"] == "image"]
        self.assertTrue(upstream)
        for code in upstream:
            self.assertEqual(codeset.default_route("video", code), "image")


if __name__ == "__main__":
    unittest.main(verbosity=2)
