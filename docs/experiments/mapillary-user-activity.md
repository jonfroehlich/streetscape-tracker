# Where one Mapillary contributor mapped, and whether our runs have it

Measured 2026-09-22 by hand and 2026-09-23 with the committed script, for the Mapillary account `uwrapid`: a 360° rig whose recent imagery lands in cities we track.
The question is operational rather than statistical — *where has this user mapped recently, is it inside a tracked city, and did our last Mapillary run there see it?* — and the answer turned into `scripts/mapillary_user_activity.py`.

Numbers come from [`mapillary-user-activity_metrics.json`](mapillary-user-activity_metrics.json), one record per capture window, each with its own `generated_by`:

```bash
python scripts/mapillary_user_activity.py uwrapid --since 2026-09-15 --until 2026-09-15 --max-requests 20 --metrics-json docs/experiments/mapillary-user-activity_metrics.json
python scripts/mapillary_user_activity.py uwrapid --since 2026-08-26 --until 2026-08-26 --max-requests 15 --metrics-json docs/experiments/mapillary-user-activity_metrics.json
python scripts/mapillary_user_activity.py uwrapid --since 2026-08-27 --until 2026-08-27 --max-requests 8 --metrics-json docs/experiments/mapillary-user-activity_metrics.json
python scripts/mapillary_user_activity.py uwrapid --since 2026-06-24 --until 2026-06-25 --max-requests 10 --metrics-json docs/experiments/mapillary-user-activity_metrics.json
```

Each was also given `--db` pointing at a laptop's **dev** catalog (recorded as `catalog_path`), which matched the centroids to cities but holds 3 Mapillary runs in total — so every group reads `never_collected` there, and none of that is a statement about production.
**49 Graph API requests in all**, from a laptop, single-threaded at ≥1 s spacing: 4 hand probes of pagination, then 11 + 1 + 15 + 10 + 8 script requests (the 15 were an uncommitted exploratory `--since 2026-08-24 --until 2026-08-28` run that located the August batch).
`graph.mapillary.com` only; the tile CDN was never touched.

## The API: a 2,000 cap, and a cursor that makes counts exact

`GET https://graph.mapillary.com/images?creator_username=U&fields=id,captured_at,geometry,sequence,is_pano&limit=2000&start_captured_at=…&end_captured_at=…`, token in an `Authorization: OAuth` header.

- **A page caps at 2,000 images.** On 2026-09-22 every by-hand query over a busy window came back at exactly 2,000, which reads like a complete answer and is not one.
- **`paging.next` works with `creator_username`**, as Mapillary's docs say it does only for that filter.
  Measured: following it from the 2026-09-15 window returned two further pages of 2,000 with **zero overlapping ids**, strictly newest-first (00:00:00 → 23:54:57.3, then 23:54:57.2 → 23:49:30.8, then 23:49:30.6 → 23:41:34), and the cursor URL carries no token.
  So the script's counts are exact when it follows the cursor to the end, and a run stopped by `--max-requests` holds **the newest** images — the useful half for "where did they map lately?" — and says its counts are lower bounds.
- **The time filters work, and `end_captured_at` is inclusive**: the 09-15 window's newest image is stamped exactly `2026-09-16T00:00:00Z`, the bound itself.
  Two adjacent windows therefore share their boundary instant; the script de-duplicates by id within a run, not across runs.
- **`captured_at` is epoch milliseconds of CAPTURE, not upload**, and nothing in these fields says when an image was uploaded.

## The volume: far more than 2,000 per five minutes

| Window (UTC) | Complete? | Images | Requests | Span of what was read (UTC) |
|---|---|---|---|---|
| 2026-09-15 | yes | **21,933** | 11 | 22:40:06 → 2026-09-16 00:00:00 |
| 2026-08-26 | yes | **0** | 1 | — |
| 2026-08-27 | no, stopped at 8 | ≥ 16,000 | 8 | 06:28:13 → 06:59:01 |
| 2026-06-24 .. 06-25 | no, stopped at 10 | ≥ 20,000 | 10 | 02:52:16 → 05:09:50 (06-25) |

The whole of 2026-09-15 is 21,933 images in 80 minutes (~270/min), and the newest 16,000 of 2026-08-27 span 31 minutes (~520/min) — so the hand observation of **more than 2,000 images per five minutes** holds with room: ~1,370 and ~2,600 per five minutes respectively.
That rate is the reason `--max-requests` exists (default 200 ≈ 400,000 images): a single busy day of this account costs ~11 requests, and an unbounded month could cost hundreds.
Sequences are many and parallel: 09-15's largest group alone holds 298 sequences, and every group is 100% pano.

## Where: Spokane and East Houston

Groups are (mean solar day × 10 km cell), centroids from the metrics file:

- **2026-09-15, Spokane WA** — 18,559 images around (47.717, −117.483), plus 1,929 at (47.695, −117.477) and 1,445 at (47.697, −117.494), all 22:40–00:00 UTC.
  Both by-hand points (~47.718, −117.464 and ~47.719, −117.442) fall in the first group's cell.
- **2026-08-26 (solar), Spokane WA** — ≥ 8,808 at (47.661, −117.460), ≥ 6,782 at (47.689, −117.508), 326 at (47.610, −117.421), 84 at (47.701, −117.508); the by-hand ~47.717, −117.485 falls in that last group's cell and 47.610, −117.421 in the 326-image one.
  The committed window is truncated at its newest 16,000, so the 84 is a floor: the uncommitted exploratory run, reaching further back, saw thousands more in that area.
- **2026-06-24 (solar), East Houston TX** — ≥ 17,251 at (29.809, −95.283) and ≥ 2,749 at (29.828, −95.279); the by-hand ~29.822, −95.279 falls in the first group's cell (a group is placed by cell, and its centroid is only the mean of its images).

## The UTC date was the wrong day, twice

**The 2026-08-26 UTC window holds nothing**; the "August 26" batch is stamped 05:54–06:59 UTC on **08-27**.
The June batch is likewise stamped 02:52–05:09 UTC on 06-25.
Grouped by UTC date, both land a day later than the by-hand reading.
The script therefore groups by **mean solar day** at each image's own longitude (UTC + lon/15 h): free, no timezone database, within about an hour of civil time, and it puts both batches back on 08-26 and 06-24.

**Flagged, not resolved: those two batches read as late-evening local captures** — 22:54–23:59 PDT and 21:52–00:09 CDT — while the September batch reads as 15:40–17:00 PDT.
Either the rig drives at night, or its clock was set to local time and Mapillary stored that as UTC, which would put the true captures in the early morning of the following day.
Nothing in `captured_at` distinguishes the two, so the solar day is a convention for grouping, not a claim about when the car was out.

## The catalog question: production had missed both Spokane batches

**Read by hand from production's catalog on 2026-09-22, and not in the committed metrics** (the script ran against a dev copy): Spokane's last Mapillary run there was **2026-09-13**, and the newest capture it saw was **2026-04-03**.
So the 09-15 batch postdates the run (`after_last_run`), and the 08-26 batch — captured 18 days *before* that run — was still not in it (`newer_than_seen`).
The second case is the one worth a flag of its own: captured before our run but not seen by it means uploaded or processed after it, and a heavy contributor's backlog can lag its captures by weeks.
The script reports both flags separately for exactly that reason.

## Decision and caveats

`scripts/mapillary_user_activity.py` answers the question for any username, on a laptop, and never from a `makelab*` host by default.
Its catalog match reads `checkpointing.frozen_bbox` for every enabled city, the same derivation the census keys on, and opens the catalog read-only.

- A group is matched by its **centroid** only, so a group straddling a frozen-grid edge is attributed to whichever side its mean falls on.
- Our Mapillary census is 360° only (#116); every uwrapid group is 100% pano, so all of it is imagery our runs would collect.
- A truncated window describes its newest images only; the 06-24 and 08-27 counts are floors.
- Re-run rather than trusting the production figures above — they were read once, by hand, and the next Spokane run will change them.
