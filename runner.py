#!/usr/bin/env python3
"""The runner — the only component in this system that spends credits.

It reads queued work out of shots/*.json, executes it, books the credits, and
writes the result back. The panel never talks to it; they meet on disk. That is
what lets the runner keep working while the director reviews, and what makes
closing the browser mid-review cost nothing.

Two drivers:

  sim     fabricates placeholder media locally. Books credits in the ledger so
          the accounting can be exercised, but spends nothing real. Default.

  agent   writes a work order to runner/orders/pending/ and stops. A Claude Code
          agent session picks the order up, calls Higgsfield (Cinema Studio
          Image 2.5 / Seedance 2.5 / Topaz), and reports back with:

              python3 runner.py complete --shot S07_04 --stage image \\
                  --media images/S07_04_a2.jpg --credits 8

          This is the seam where real generation attaches. Nothing above this
          line needs to change to swap the model behind it.

    python3 runner.py --watch
    python3 runner.py --once --driver agent
"""

import argparse
import json
import os
import time

import placeholder
import store as store_mod

TIER_PRIORITY = {"A": 0, "B": 1, "C": 2, None: 3}
LOG_LIMIT = 200


class Runner:
    def __init__(self, st, driver="sim", verbose=True):
        self.store = st
        self.driver = driver
        self.verbose = verbose
        self.state_path = os.path.join(st.runner_dir, "state.json")
        self.orders_dir = os.path.join(st.runner_dir, "orders")

    # ------------------------------------------------------------- state

    def _read_state(self):
        if os.path.exists(self.state_path):
            try:
                with open(self.state_path, encoding="utf-8") as fh:
                    return json.load(fh)
            except (OSError, ValueError):
                pass
        return {"running": None, "driver": self.driver, "heartbeat": None,
                "log": [], "failures": []}

    def _write_state(self, **changes):
        state = self._read_state()
        state.update(changes)
        state["driver"] = self.driver
        state["heartbeat"] = store_mod.now()
        state["log"] = state.get("log", [])[-LOG_LIMIT:]
        state["failures"] = state.get("failures", [])[-LOG_LIMIT:]
        os.makedirs(os.path.dirname(self.state_path), exist_ok=True)
        tmp = self.state_path + ".tmp"
        with open(tmp, "w", encoding="utf-8") as fh:
            json.dump(state, fh, indent=2)
        os.replace(tmp, self.state_path)
        return state

    def log(self, message, **detail):
        state = self._read_state()
        entry = {"at": store_mod.now(), "message": message, **detail}
        state.setdefault("log", []).append(entry)
        self._write_state(log=state["log"])
        if self.verbose:
            print("[runner] %s %s" % (message,
                                      " ".join("%s=%s" % kv for kv in detail.items())))

    def fail_log(self, shot_id, stage, error):
        state = self._read_state()
        state.setdefault("failures", []).append(
            {"at": store_mod.now(), "shot_id": shot_id, "stage": stage, "error": error})
        self._write_state(failures=state["failures"])

    # ------------------------------------------------------------- queue

    def next_items(self):
        """Hero shots first — if the sprint runs out of credits, it should run
        out on inserts."""
        items = self.store.queue_items()
        def key(item):
            plan = self.store.read("plan", item["shot_id"]) or {}
            return (TIER_PRIORITY.get(plan.get("tier"), 3), item.get("queued_at") or "",
                    item["shot_id"])
        return sorted(items, key=key)

    def pass_once(self):
        done = 0
        for item in self.next_items():
            if self.driver == "agent" and item.get("job_id"):
                continue  # order already out with the agent
            self.process(item)
            done += 1
        self._write_state(running=None)
        return done

    def watch(self, interval=3.0):
        self.log("watch started", driver=self.driver)
        try:
            while True:
                self.pass_once()
                self._write_state(running=None)
                time.sleep(interval)
        except KeyboardInterrupt:
            self.log("watch stopped")
            self._write_state(running=None)

    # ----------------------------------------------------------- execute

    def process(self, item):
        shot_id, stage, kind = item["shot_id"], item["stage"], item["kind"]
        record = self.store.read(stage, shot_id)
        if not record or record.get("state") != "queued":
            return
        plan = self.store.read("plan", shot_id) or {}
        job_id = "job_%s_%s_%d" % (shot_id, stage, int(time.time() * 1000) % 10 ** 8)
        self._write_state(running={"shot_id": shot_id, "stage": stage, "kind": kind,
                                   "job_id": job_id, "started_at": store_mod.now(),
                                   "estimate": item["estimate"]})
        record["job_id"] = job_id
        self.store.write(stage, record)

        if self.driver == "agent":
            self.emit_order(shot_id, stage, kind, record, plan, job_id,
                            item["estimate"])
            self.log("order emitted", shot_id=shot_id, stage=stage, job=job_id)
            return

        try:
            media = self.generate_placeholder(shot_id, stage, kind, record, plan)
        except Exception as exc:  # a driver failure must not wedge the queue
            self.fail(shot_id, stage, "%s: %s" % (type(exc).__name__, exc))
            return
        self.complete(shot_id, stage, media, credits=item["estimate"], job_id=job_id)

    def emit_order(self, shot_id, stage, kind, record, plan, job_id, estimate):
        """A work order is a complete brief: everything the agent needs to place
        the call, and nothing it would have to go read the panel to find."""
        pending = os.path.join(self.orders_dir, "pending")
        os.makedirs(pending, exist_ok=True)
        order = {
            "job_id": job_id,
            "shot_id": shot_id,
            "stage": stage,
            "kind": kind,
            "model": ("cinema-studio-image-2.5" if stage == "image"
                      else "seedance-2.5"),
            "prompt": record.get("prompt", ""),
            "attempt": record.get("attempts", 0),
            "attempt_cap": self.store.attempt_cap,
            "estimate_credits": estimate,
            "tier": plan.get("tier"),
            "scene": plan.get("scene"),
            "description": plan.get("description", ""),
            "elements": plan.get("elements", []),
            "queue_note": record.get("queue_note", ""),
            "defects": record.get("defects", [])[-3:],
            "issued_at": store_mod.now(),
            "report_back": ("python3 runner.py complete --shot %s --stage %s "
                            "--media <path-under-media/> --credits <n>"
                            % (shot_id, stage)),
        }
        if stage == "image":
            order["resolution"] = record.get("resolution", "2k")
        else:
            order["start_frame"] = record.get("start_frame")
            order["references"] = record.get("references", [])
            order["duration"] = record.get("duration", 5)
        if kind == "local_edit":
            order["edit_target"] = record.get("media")
        with open(os.path.join(pending, "%s.json" % job_id), "w",
                  encoding="utf-8") as fh:
            json.dump(order, fh, indent=2, ensure_ascii=False)
        return order

    def generate_placeholder(self, shot_id, stage, kind, record, plan):
        attempt = record.get("attempts", 0) or 1
        salt = attempt if kind == "regenerate" else "%se" % attempt
        relative = os.path.join("%ss" % stage, "%s_a%s.svg" % (shot_id, salt))
        target = os.path.join(self.store.media_dir, relative)
        label = plan.get("description", "")[:48]
        if stage == "image":
            placeholder.make_still(target, shot_id, label, plan.get("tier") or "",
                                   attempt=salt)
        else:
            placeholder.make_clip(target, shot_id, label, plan.get("tier") or "",
                                  attempt=salt, seconds=record.get("duration", 5))
        return relative.replace(os.sep, "/")

    # ------------------------------------------------------ report back

    def complete(self, shot_id, stage, media, credits=None, job_id=None):
        record = self.store.read(stage, shot_id)
        if record is None:
            raise store_mod.StoreError("no %s record for %s" % (stage, shot_id), 404)
        kind = record.get("queued_kind", "regenerate")
        if credits is None:
            credits = self.store.estimate(stage, record, kind)
        record["media"] = media
        record["state"] = "done"
        record["job_id"] = job_id or record.get("job_id")
        record["credits_spent"] = int(record.get("credits_spent") or 0) + int(credits)
        record.setdefault("history", []).append({
            "at": store_mod.now(), "event": "generated", "kind": kind,
            "media": media, "credits": int(credits),
            "attempt": record.get("attempts", 0), "driver": self.driver,
        })
        self.store.write(stage, record)
        self._retire_order(record.get("job_id"), "done")
        self.log("done", shot_id=shot_id, stage=stage, credits=credits, media=media)
        self._write_state(running=None)
        return record

    def fail(self, shot_id, stage, error, credits=0):
        record = self.store.read(stage, shot_id)
        if record is None:
            raise store_mod.StoreError("no %s record for %s" % (stage, shot_id), 404)
        credits = int(credits or 0)
        record["credits_spent"] = int(record.get("credits_spent") or 0) + credits
        if credits == 0 and record.get("queued_kind", "regenerate") == "regenerate":
            # Nothing was produced and nothing was charged, so the attempt does
            # not count against the cap. The cap exists to bound credit burn.
            record["attempts"] = max(0, record.get("attempts", 0) - 1)
        record["state"] = "prompted"
        record["job_id"] = None
        record.setdefault("history", []).append({
            "at": store_mod.now(), "event": "failed", "error": error,
            "credits": credits, "driver": self.driver,
        })
        self.store.write(stage, record)
        self._retire_order(record.get("job_id"), "failed")
        self.fail_log(shot_id, stage, error)
        self.log("failed", shot_id=shot_id, stage=stage, error=error)
        self._write_state(running=None)
        return record

    def _retire_order(self, job_id, outcome):
        if not job_id:
            return
        source = os.path.join(self.orders_dir, "pending", "%s.json" % job_id)
        if not os.path.exists(source):
            return
        target_dir = os.path.join(self.orders_dir, outcome)
        os.makedirs(target_dir, exist_ok=True)
        os.replace(source, os.path.join(target_dir, "%s.json" % job_id))


def main():
    parser = argparse.ArgumentParser(description="AYNI runner — executes queued work")
    parser.add_argument("command", nargs="?", default="run",
                        choices=("run", "complete", "fail", "orders"))
    parser.add_argument("--root", default=os.environ.get("AYNI_ROOT", "."))
    parser.add_argument("--driver", default=os.environ.get("AYNI_DRIVER", "sim"),
                        choices=("sim", "agent"))
    parser.add_argument("--watch", action="store_true")
    parser.add_argument("--once", action="store_true")
    parser.add_argument("--interval", type=float, default=3.0)
    parser.add_argument("--shot")
    parser.add_argument("--stage", choices=("image", "video"))
    parser.add_argument("--media")
    parser.add_argument("--credits", type=int)
    parser.add_argument("--job-id")
    parser.add_argument("--error", default="unspecified failure")
    parser.add_argument("--quiet", action="store_true")
    args = parser.parse_args()

    st = store_mod.Store(args.root)
    st.ensure_dirs()
    runner = Runner(st, driver=args.driver, verbose=not args.quiet)

    if args.command == "complete":
        if not (args.shot and args.stage and args.media):
            parser.error("complete needs --shot, --stage and --media")
        runner.complete(args.shot, args.stage, args.media, args.credits, args.job_id)
        return
    if args.command == "fail":
        if not (args.shot and args.stage):
            parser.error("fail needs --shot and --stage")
        runner.fail(args.shot, args.stage, args.error, args.credits or 0)
        return
    if args.command == "orders":
        pending = os.path.join(runner.orders_dir, "pending")
        for name in sorted(os.listdir(pending)) if os.path.isdir(pending) else []:
            print(os.path.join(pending, name))
        return

    if args.watch or not args.once:
        runner.watch(args.interval)
    else:
        count = runner.pass_once()
        print("processed %d queued item(s)" % count)


if __name__ == "__main__":
    main()
