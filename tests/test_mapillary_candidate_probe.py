"""
The paced Graph API candidate probe (scripts/mapillary_candidate_probe.py,
#406 acceptance item 4).

No network: the fetch primitive is substituted with an in-memory one returning
``HttpResult``s, as tests/test_mapillary_user_activity.py does, and the pacer
runs on an injected clock. The probe's contract is narrow and each clause is
pinned: one request per candidate with a 2 x 2 km bbox and limit <= 200, at
least 3 s between request starts, NO retry — the first non-200 stops the run —
dry-run by default, and a makelab host refused.
"""

import csv
import json
import socket

import pytest

from scripts import mapillary_candidate_probe as probe
from scripts import mapillary_user_activity as mua


class _Clock:
    def __init__(self):
        self.t = 1000.0

    def now(self):
        return self.t

    def sleep(self, s):
        self.t += s


class _Graph:
    """An in-memory Graph API: one queued HttpResult per request, in order."""

    def __init__(self, responses, clock=None):
        self.responses = list(responses)
        self.calls = []
        self.times = []
        self.clock = clock

    def __call__(self, url, params):
        self.calls.append((url, params))
        if self.clock is not None:
            self.times.append(self.clock.now())
        res = self.responses.pop(0)
        if isinstance(res, Exception):
            raise res
        return res


def _ok(images):
    return mua.HttpResult(200, json.dumps({"data": images}))


def _img(i, *, pano=True, creator="111", ms=1_762_214_400_000):
    return {"id": str(i), "captured_at": ms, "creator_id": creator, "is_pano": pano}


CANDIDATES = [
    {"name": "Alpha", "lat": 48.0, "lon": 2.0},
    {"name": "Beta", "lat": 60.0, "lon": 10.0},
    {"name": "Gamma", "lat": -30.0, "lon": -60.0},
]


def _run(tmp_path, graph, clock=None, limit=200, min_interval=3.0):
    clock = clock or _Clock()
    pacer = mua.Pacer(min_interval, sleep=clock.sleep, clock=clock.now)
    out = tmp_path / "probe.csv"
    log = tmp_path / "probe.csv.requests.jsonl"
    code = probe.run(
        CANDIDATES, fetch=graph, pacer=pacer, limit=limit, out_path=str(out), log_path=str(log)
    )
    with open(out, encoding="utf-8") as f:
        rows = list(csv.DictReader(f))
    with open(log, encoding="utf-8") as f:
        entries = [json.loads(line) for line in f]
    return code, rows, entries


def test_one_request_per_candidate_with_the_small_bbox_query(tmp_path):
    graph = _Graph([_ok([_img(1)]), _ok([]), _ok([_img(2, pano=False)])])
    code, rows, entries = _run(tmp_path, graph)

    assert code == probe.EXIT_OK
    assert len(graph.calls) == 3
    for (url, params), cand in zip(graph.calls, CANDIDATES, strict=True):
        assert url == "https://graph.mapillary.com/images"
        assert params["fields"] == "id,captured_at,creator_id,is_pano"
        assert params["limit"] == 200
        min_lon, min_lat, max_lon, max_lat = map(float, params["bbox"].split(","))
        # 2 km on each side, centred on the candidate.
        assert (max_lat - min_lat) * probe.KM_PER_DEG_LAT == pytest.approx(2.0, rel=1e-3)
        assert (min_lat + max_lat) / 2 == pytest.approx(cand["lat"], abs=1e-5)
        assert (min_lon + max_lon) / 2 == pytest.approx(cand["lon"], abs=1e-5)
    # Longitude widened by 1/cos(lat), so the box is square on the ground at 60 N.
    _, beta = graph.calls[1]
    w = [float(v) for v in beta["bbox"].split(",")]
    assert (w[2] - w[0]) == pytest.approx(2 * (w[3] - w[1]), rel=1e-3)
    assert [r["name"] for r in rows] == ["Alpha", "Beta", "Gamma"]
    assert [e["status"] for e in entries] == [200, 200, 200]


def test_the_limit_is_passed_through(tmp_path):
    graph = _Graph([_ok([]), _ok([]), _ok([])])
    _run(tmp_path, graph, limit=50)
    assert {p["limit"] for _, p in graph.calls} == {50}


def test_requests_are_at_least_three_seconds_apart(tmp_path):
    clock = _Clock()
    graph = _Graph([_ok([]), _ok([]), _ok([])], clock=clock)
    _run(tmp_path, graph, clock=clock)
    gaps = [b - a for a, b in zip(graph.times, graph.times[1:], strict=False)]
    assert len(gaps) == 2
    assert all(g >= 3.0 for g in gaps)


def test_the_summary_reads_panos_creators_and_dates(tmp_path):
    images = [_img(1, creator="7"), _img(2, creator="7"), _img(3, pano=False, creator="8")]
    graph = _Graph([_ok(images), _ok([]), _ok([])])
    _, rows, _ = _run(tmp_path, graph, limit=3)

    alpha = rows[0]
    assert alpha["images"] == "3"
    assert alpha["capped"] == "True"  # filled the limit: a floor
    assert alpha["panos"] == "2"
    assert alpha["top_creator_id"] == "7"
    assert alpha["top_creator_share"] == "0.667"
    assert alpha["newest_captured_utc"] == "2025-11-04"
    assert rows[1]["images"] == "0" and rows[1]["capped"] == "False"


@pytest.mark.parametrize(
    "response,expected_exit",
    [
        (mua.HttpResult(500, '{"error": {"message": "Please reduce the amount of data"}}'), 1),
        (mua.HttpResult(429, "{}", headers={"Retry-After": "1"}), 1),
        (mua.HttpResult(302, "", content_type="text/html"), probe.BLOCKED_EXIT),
        (mua.HttpResult(200, "<html>login</html>", content_type="text/html"), probe.BLOCKED_EXIT),
        (mua.TransientError("ConnectionError"), 1),
    ],
)
def test_the_first_non_200_stops_the_run_without_a_retry(tmp_path, response, expected_exit):
    graph = _Graph([_ok([_img(1)]), response, _ok([])])
    code, rows, entries = _run(tmp_path, graph)

    assert code == expected_exit
    assert len(graph.calls) == 2  # Beta was not retried and Gamma never asked
    assert [r["name"] for r in rows] == ["Alpha"]  # the answered one is kept
    assert len(entries) == 2
    assert entries[1]["name"] == "Beta"


def test_dry_run_is_the_default_and_sends_nothing(tmp_path, monkeypatch, capsys):
    path = tmp_path / "c.csv"
    path.write_text("name,lat,lon,notes\nAlpha,48.0,2.0,x\n", encoding="utf-8")

    def refuse(*a, **k):
        raise AssertionError("a dry run built an HTTP client")

    monkeypatch.setattr(probe, "make_requests_fetch", refuse)
    assert probe.main([str(path)]) == probe.EXIT_OK
    out = capsys.readouterr().out
    assert "DRY RUN: 1 request(s)" in out and "Alpha" in out


def test_execute_refuses_a_collection_host_before_any_request(tmp_path, monkeypatch):
    path = tmp_path / "c.csv"
    path.write_text("name,lat,lon\nAlpha,48.0,2.0\n", encoding="utf-8")
    monkeypatch.setattr(socket, "gethostname", lambda: "makelab2")

    def refuse(*a, **k):
        raise AssertionError("built an HTTP client on a collection host")

    monkeypatch.setattr(probe, "make_requests_fetch", refuse)
    with pytest.raises(SystemExit) as excinfo:
        probe.main([str(path), "--execute", "--out", str(tmp_path / "o.csv")])
    assert excinfo.value.code == probe.USAGE_EXIT


@pytest.mark.parametrize(
    "extra",
    [
        ["--limit", "201"],
        ["--limit", "0"],
        ["--min-interval", "2.9"],
        ["--min-interval", "nan"],
        ["--execute"],  # no --out
    ],
)
def test_usage_errors_exit_64(tmp_path, extra):
    path = tmp_path / "c.csv"
    path.write_text("name,lat,lon\nAlpha,48.0,2.0\n", encoding="utf-8")
    with pytest.raises(SystemExit) as excinfo:
        probe.main([str(path), *extra])
    assert excinfo.value.code == probe.USAGE_EXIT


def test_an_existing_out_is_never_overwritten(tmp_path):
    path = tmp_path / "c.csv"
    path.write_text("name,lat,lon\nAlpha,48.0,2.0\n", encoding="utf-8")
    out = tmp_path / "o.csv"
    out.write_text("earlier run\n", encoding="utf-8")
    with pytest.raises(SystemExit) as excinfo:
        probe.main([str(path), "--execute", "--out", str(out)])
    assert excinfo.value.code == probe.USAGE_EXIT
    assert out.read_text(encoding="utf-8") == "earlier run\n"


@pytest.mark.parametrize(
    "body",
    ["name,lat\nAlpha,48.0\n", "name,lat,lon\nAlpha,north,2.0\n", "name,lat,lon\n"],
)
def test_a_bad_candidates_file_is_a_usage_error(tmp_path, body):
    path = tmp_path / "c.csv"
    path.write_text(body, encoding="utf-8")
    assert probe.main([str(path)]) == probe.USAGE_EXIT


@pytest.mark.parametrize("explicit_log", [True, False])
def test_main_passes_every_flag_through_to_the_run(tmp_path, monkeypatch, explicit_log):
    """
    ``--min-interval``, ``--limit``, ``--out`` and ``--request-log`` must reach
    ``run`` and the Pacer as given: the other tests drive ``run`` directly, so a
    ``main`` that dropped one for its default would pass them all.
    """
    from streetscape_metadata_tracker import config as cfg

    path = tmp_path / "c.csv"
    path.write_text("name,lat,lon\nAlpha,48.0,2.0\n", encoding="utf-8")
    out = tmp_path / "o.csv"
    log = tmp_path / "custom.jsonl"
    monkeypatch.setattr(socket, "gethostname", lambda: "laptop")
    monkeypatch.setattr("dotenv.load_dotenv", lambda *a, **k: None)
    monkeypatch.setattr(cfg, "warn_if_credentials_world_readable", lambda *a, **k: None)
    monkeypatch.setattr(cfg, "load_config", lambda channel: {"access_token": "TOKEN"})
    sentinel = object()
    monkeypatch.setattr(probe, "make_requests_fetch", lambda token: (sentinel, token))
    seen = {}

    def fake_run(candidates, **kw):
        seen.update(kw, candidates=candidates)
        return probe.EXIT_OK

    monkeypatch.setattr(probe, "run", fake_run)
    argv = [str(path), "--execute", "--out", str(out), "--min-interval", "7.5", "--limit", "50"]
    if explicit_log:
        argv += ["--request-log", str(log)]

    assert probe.main(argv) == probe.EXIT_OK
    assert seen["fetch"] == (sentinel, "TOKEN")
    assert isinstance(seen["pacer"], mua.Pacer)
    assert seen["pacer"].min_interval_s == 7.5
    assert seen["limit"] == 50
    assert seen["out_path"] == str(out)
    assert seen["log_path"] == (str(log) if explicit_log else f"{out}.requests.jsonl")
    assert [c["name"] for c in seen["candidates"]] == ["Alpha"]
