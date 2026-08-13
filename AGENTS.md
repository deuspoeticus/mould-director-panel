# Working with the panel, for an agent

You generate shots with Higgsfield. A human director reviews them in
`index.html` and writes notes saying what is wrong. This file is the contract
between you.

You never talk to the panel directly. You meet it at two files sitting next to
`index.html`:

```
shots.js          you write it     — what you have generated
work-order.json   you read it      — what the director wants next
```

## The loop

1. Read `work-order.json` (the director exports it from **Overview → Work for
   the agent**, or copies it to you).
2. Do the work with Higgsfield, one queue at a time.
3. Write `shots.js`.
4. Say so. The director presses **⟳ Check for new work** and your results appear
   in their review wall, without losing their place.

## What you write: shots.js

A plain JavaScript file assigning one global. Not JSON, not a module — the
panel loads it with a `<script>` tag, because a page opened from disk is not
allowed to `fetch()` a sibling file.

```js
window.SHOTS_FEED = {
  "project": "AYNI",
  "updated": "2026-08-13T14:02:11Z",
  "shots": [
    {
      "id": "S07_04",
      "scene": 7,
      "tier": "A",
      "description": "Nayra at the channel",
      "still": {
        "media": "https://media.higgsfield.ai/....jpg",
        "prompt": "the exact prompt you sent",
        "model": "cinema-studio-image-2.5",
        "generation_id": "...",
        "credits": 8,
        "created_at": "2026-08-13T14:01:40Z"
      },
      "clip": {
        "media": "https://media.higgsfield.ai/....mp4",
        "start_frame": "https://media.higgsfield.ai/....jpg",
        "prompt": "...",
        "model": "seedance-2.5",
        "generation_id": "...",
        "credits": 40,
        "created_at": "2026-08-13T14:05:02Z"
      }
    }
  ]
};
```

Only `id` is required. Send only the shots you changed — the panel merges on
`id` and leaves everything else alone.

`media` takes a URL. Higgsfield gives you one; use it as-is. Do not download
anything, and do not build a media folder — the panel loads remote URLs
directly. A relative path like `media/S07_04.jpg` also works if the file really
does sit beside `index.html`.

Always set `updated` to the current time when you rewrite the file. The panel
uses it to notice there is something new.

## Rules

**Never set `status`.** You generate; the director judges. Any `status` you put
in the feed is ignored. New `media` on a shot automatically puts it back in
front of the director as *to review* — that is the only status change you can
cause, and you cause it by delivering work.

**Never invent or renumber a shot id.** `S07_04` is the join key and the only
thing tying your work to their decisions. If a shot needs to exist that is not
in the plan, say so in prose; do not conjure an id.

**Never remove a shot or a note.** The feed only adds and updates. Notes are the
director's record of what went wrong, and they are kept after the fix.

**Always send the prompt you actually used.** When the director rejects a take,
the panel shows them that prompt while they write the note, and hands it back to
you in the work order. A note without its prompt is guesswork on your side.

**Re-sending an unchanged feed is safe.** Merging is repeatable: identical media
changes nothing, and notes are de-duplicated.

## What you read: work-order.json

```json
{
  "project": "AYNI",
  "generated": "2026-08-13T14:20:00Z",
  "queues": {
    "needs_prompting":   [ { "shot": "S07_05", "scene": 7, "tier": "C",
                             "description": "Insert of the water",
                             "stage": "still" } ],
    "needs_work":        [ { "shot": "S07_04", "stage": "still",
                             "note": "hands are wrong, shawl colour drifted",
                             "earlier_notes": ["too dark"],
                             "prompt": "the prompt that produced the bad take",
                             "model": "cinema-studio-image-2.5",
                             "media": "https://...",
                             "generation_id": "..." } ],
    "ready_to_animate":  [ { "shot": "S07_04", "stage": "clip",
                             "start_frame": "https://....jpg",
                             "still_prompt": "..." } ],
    "ready_to_upscale":  [ { "shot": "S07_04", "stage": "clip",
                             "media": "https://....mp4" } ]
  }
}
```

Every queue is derived from the director's decisions. There is no list anyone
maintains by hand, and nothing appears in a queue until they have made the
decision that puts it there.

## Which queue maps to which tool

| Queue | What to do |
| --- | --- |
| `needs_prompting` | `generate_image` from `description`. Write the still back. |
| `needs_work` | Revise the `prompt` using `note`, then regenerate that stage — `generate_image` for `still`, `generate_video` for `clip`. |
| `ready_to_animate` | `generate_video` with `start_frame` as the input image. This is the handoff: the approved still is the first frame, so do not restate wardrobe or identity the frame already carries. |
| `ready_to_upscale` | `upscale_video` (or `upscale_image`). Write the result back with `"upscaled": true` so it leaves the queue. |

Use the batch tools for a whole queue at once, wait with `jobs_wait`, then
collect with one `show_generation_by_ids` before writing `shots.js`.

Record `credits` per generation. The panel totals them so the director can see
the burn without asking.

## Worked example

Director exports a work order with eleven shots in `ready_to_animate`.

1. `generate_video_batch` — one job per shot, `start_frame` as the input image,
   the shot's `description` shaping the motion.
2. `jobs_wait` on the batch.
3. `show_generation_by_ids` to collect URLs, generation ids and credit costs.
4. Write `shots.js` with eleven shots, each carrying only a `clip` object.
5. Tell the director: *"Eleven clips ready, press Check for new work."*

They review the wall, approve seven, and write notes on four. The next work
order has those four in `needs_work`, each with the prompt that made it and a
sentence saying what was wrong.
