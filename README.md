# AYNI shot panel

One HTML file. Open it in a browser — double-click it, no server, no install, no
build step. It reviews the shot list for the AYNI sprint: what is approved, what
still needs work, and in plain words what is missing.

```
index.html      the whole thing
```

## Using it

1. Open `index.html`.
2. On the **Shots** tab, paste your shot list (CSV or JSON) and press *Import*,
   or drop a file anywhere on the page. There is also a *Load an example shot
   list* button if you just want to see it work.
3. Put a media URL or file path in the **still media** / **clip media** columns.
   Anything that a browser can load works: an `https://` URL from Higgsfield, or
   a relative path like `media/S07_04.jpg` if you keep the media in a folder
   next to `index.html`.
4. Review on the **Review** tab. Approve, or write a note saying what is wrong.
5. On **Overview**, *Copy list for the agent* gives you every outstanding note as
   plain text, ready to paste back to whoever is generating the shots.

## Review

Stills and clips are two separate walls — the `Stills` / `Clips` toggle, or `S`.
Each shot has its own status per wall, so a shot whose still is approved can
still have a clip that needs work.

| Key | |
| --- | --- |
| `A` | Approve |
| `R` | Needs work — opens the note box |
| `Enter` | Save the note (`Shift`+`Enter` for a new line) |
| `⇧S` | While writing a note on a clip: save it to the still instead |
| `← → ↑ ↓` | Move between shots |
| `O` | Open the shot large |
| `S` | Switch between stills and clips |
| `?` | Help |

There is no defect vocabulary and nothing to look up. A rejection is a sentence
about what is missing, and that sentence is what you send back.

Notes are kept per shot per stage, with a timestamp, and stay on the shot after
it is fixed — the history of what went wrong is usually the useful part. Delete
one with the `×` on the note.

## Where the data lives

In your browser's local storage, saved on every change. Closing the tab or the
laptop loses nothing.

That storage belongs to one browser on one machine. To keep a copy, move to
another machine, or hand the list to someone else, use **Shots → Download JSON**
(or Copy JSON). Importing it somewhere else restores everything, notes included.

Import has two modes. *Merge* matches on shot id: it updates the description and
media, adds shots it has not seen, and keeps every status and note you already
have. New media on an existing shot sets it back to *to review*, since it is a
new thing to look at. *Replace* throws away what is there and starts from the
file.

## Shot list format

CSV needs a header row. Only `shot` is required.

```csv
shot,scene,tier,description,still,clip
S07_04,7,A,Nayra at the channel,media/S07_04.jpg,media/S07_04.mp4
S07_05,7,C,Insert of the water,,
```

JSON is what the export writes, and is the format to prefer since it carries
statuses and notes:

```json
{
  "project": "AYNI",
  "shots": [
    {
      "id": "S07_04",
      "scene": 7,
      "tier": "A",
      "description": "Nayra at the channel",
      "still": { "status": "approved", "media": "media/S07_04.jpg", "notes": [] },
      "clip":  { "status": "rework", "media": "media/S07_04.mp4",
                 "notes": [{ "at": "2026-08-13T09:12:00Z",
                             "text": "camera drifts left, should be locked" }] }
    }
  ]
}
```

`status` is one of `todo`, `review`, `approved`, `rework`. `scene` is filled in
from the shot id when it looks like `S07_04`. `tier` is free text — A/B/C if you
use it, blank if you do not.

## Notes

Clips only load while they are on screen, and no more than eight play at once,
so a wall of a hundred clips does not stall the browser.

Everything runs locally. Nothing is uploaded, and the page makes no network
requests of its own — only the browser fetching whatever media URLs you point it
at.
