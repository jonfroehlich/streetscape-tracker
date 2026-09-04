# KartaView's viewer will not open the photos we link to

Issue #312.
Measured 2026-09-02, after a click on a pano dot in `city.html` opened `kartaview.org/details/8313353/936` and the page rendered "Ups! Sequence cannot be loaded…" with `Cannot read properties of null (reading 'photos')` in the console.

Numbers come from [`kartaview-viewer-deeplink_metrics.json`](kartaview-viewer-deeplink_metrics.json), produced by
`scripts/kartaview_details_probe.py --csv <the Krabi 2026-08-28 KartaView run> --docs-dir docs/experiments`.
39 requests to `api.kartaview.org/details` plus a 4-sequence authenticated re-ask and 3 v2 health checks, paced to the documented 1,000/hr authenticated limit; laptop only (`refuse_on_collection_host`).
There is no distribution to quote — the outcome is the same for every sequence — so what follows is a response matrix and two controls.

## The link format is right, and that is the part worth writing down

KartaView's own single-page app writes exactly the URL we publish.
From their `main.*.js`, the routine that rewrites the address bar as a viewer session moves between photos:

```js
updatePageUrl = function () { …
  t.location.replaceState(e + t.currentPhotoCache.sequenceId + "/" +
                          t.currentPhotoCache.sequenceIndex + "/" + t.sidebarTab)
```

So `details/{sequence_id}/{sequence_index}` is their canonical form, not our guess at one, and the pano behind the failing link is real and fully processed:
`GET /2.0/photo/?sequenceId=8313353&sequenceIndex=936` returns photo `1855176953`, `autoImgProcessingStatus: FINISHED`, with live `cdn.kartaview.org` URLs.
`POST /1.0/sequence/photo-list/` returns all 1,440 photos of that sequence with index 936 present.

**The failing link and a wrong link look identical from the outside, which is the trap.**
Anyone who meets this error and reaches for `viewerUrl` in `www/js/streetscape-utils.js` will be editing code that is already correct.

## What actually fails: one v1 endpoint, for every sequence

The sequence page loads through a single call — `POST https://api.kartaview.org/details`, body `id=<sequenceId>&platform=web` — and then dereferences `response.osv.photos`.
That endpoint returns `osv: null`, which *is* the console error.

| Probed | Sequences | Outcome |
|---|---|---|
| The Krabi run's own sequences | 38 | `apiCode 400` "not found", `osv: null` |
| **KartaView's documented example sequence `6187609`** | 1 | `apiCode 400` "not found", `osv: null` |
| Re-asked with `KARTAVIEW_ACCESS_TOKEN` | 4 | identical; the token changes no verdict |
| v2 controls on the same host (`/2.0/sequence/{id}`, `…/photos`, `/2.0/photo/`) | 3 | all `apiCode 600`, healthy |

**Their own example failing is the whole argument.**
It goes through the identical call and has nothing to do with our data, our sequences, our token or our request shape, so no property of this project can be the cause.

Three confounds were ruled out rather than argued away:

- **Not our credential.** Anonymous and authenticated responses are byte-equivalent in verdict; `token_changes_verdict_for` is empty.
- **Not per-IP throttling of the probe.** An earlier ad-hoc pass had spent ~50 requests in ten minutes; a re-ask after twelve minutes of total quiet returned the same "not found", and the committed run is paced to the documented limit throughout.
- **Not a host-wide outage.** The v2 endpoints answer 600 for the very sequences whose `/details` call returns null.

**The failure is not one stable state**, which matters for anyone re-running this.
The first ad-hoc pass (not committed — it predates the script) saw three shapes within an hour: `apiCode 603` "The requested sequence still processing photo" for 4 of 38 sequences, an HTTP 500 "User Authentication passport issue" for the other 34, and then `apiCode 400` "not found" for everything including the control.
Treat a single green sequence as noise; the verdict worth acting on is the control's.

## Decision: a map-view fallback, offered first

The popup now carries two links (`buildViewerLinksHtml` in `www/js/city.js`, mirrored in `vis.PROVIDER_DISPLAY`):

1. **`View location on KartaView map`** → `https://kartaview.org/map/@{pano_lat},{pano_lon},19z`
2. **`Exact photo (KartaView's viewer is often broken)`** → the unchanged `details/{seq}/{idx}` URL

The order is the point: the link that works is the one a reader reaches first.
The map view is served by the v2 stack that answers, and its coverage tiles were measured serving real content up to z20 — z19 frames the pano's own track rather than a neighbourhood-sized blur, where KartaView's own marketing links sit at 13z–15z.

Two properties of the fallback are deliberate:

- **It is keyed on the pano's position, not the grid point's.** The reader is being sent to the imagery, not to the sample point that found it.
- **It covers strictly more rows than the link it backs up.** Every linkable row carries `pano_lat`/`pano_lon` (`OK`, `NO_DATE` and `FLAT_ONLY` all populate them; only `ZERO_RESULTS` is blank, and it never gets a link), so rows with a null `sequence_id` — which never had a photo link at all — now get one link instead of none.

The precise link stays because it is the only URL that names the exact pano a run sampled, and because the format is right: if KartaView repairs `/details`, the fix here is re-ordering two entries and dropping a caveat from a label, not rediscovering the URL.

## Replication, and what this cannot tell you

```bash
python scripts/kartaview_details_probe.py \
  --csv https://makeabilitylab.cs.washington.edu/public/streetscape-tracker/data/<a kartaview run>.csv.gz \
  --docs-dir docs/experiments
```

~40 requests, ~2.5 minutes authenticated (~24 anonymous), no catalog and no collection host.
It measures **the call the page depends on, not the page**: a sequence whose `/details` answers could still fail to render for some other reason, so a clean run is necessary and not sufficient — confirm in a browser before concluding the viewer recovered.

And there is nobody to report this to.
KartaView has no developer forum and its `kartaview/openstreetcam.org` tracker is unstaffed (see [`kartaview-feasibility.md`](kartaview-feasibility.md)), so for this provider the standing expectation is decay without notice: an endpoint we depend on can stop answering and nothing outside our own probes will say so.
