"""run_cities.py parsing tests — the cities.txt line → argv contract.

Covers parse_city_line (comment/blank skipping, shlex tokenization of
quoted and unquoted names) and split_city_and_flags (rejoining unquoted
multi-token city names ahead of the first --flag), which together decide
exactly what city string streetscape_tracker.py receives.
"""

import run_cities


def test_parse_city_line_skips_comments_and_blanks():
    assert run_cities.parse_city_line("# a comment") is None
    assert run_cities.parse_city_line("   # indented comment") is None
    assert run_cities.parse_city_line("") is None
    assert run_cities.parse_city_line("   \n") is None


def test_parse_city_line_tokenizes_unquoted_and_quoted_names():
    # Unquoted: shlex splits the name on whitespace (rejoined later by
    # split_city_and_flags)
    assert run_cities.parse_city_line("Seattle, WA --width 2000") == [
        "Seattle,",
        "WA",
        "--width",
        "2000",
    ]
    # Quoted: the name survives as one token
    assert run_cities.parse_city_line('"Seattle, WA" --width 2000') == [
        "Seattle, WA",
        "--width",
        "2000",
    ]


def test_split_city_and_flags_rejoins_unquoted_names():
    name, flags = run_cities.split_city_and_flags(
        ["Grand", "Marais,", "MN", "--width", "2000", "--height", "1500"]
    )
    assert name == "Grand Marais, MN"
    assert flags == ["--width", "2000", "--height", "1500"]


def test_split_city_and_flags_flag_values_never_join_the_name():
    # Once a --flag appears, later bare tokens are flag VALUES, not name parts
    name, flags = run_cities.split_city_and_flags(["Bend,", "OR", "--step", "25"])
    assert name == "Bend, OR"
    assert flags == ["--step", "25"]


def test_split_city_and_flags_quoted_name_no_flags():
    assert run_cities.split_city_and_flags(["Seattle, WA"]) == ("Seattle, WA", [])


def test_load_cities_reads_file_skipping_noise(tmp_path):
    p = tmp_path / "cities.txt"
    p.write_text('# US cities\n\nSeattle, WA --width 2000\n"Bend, OR"\n')
    assert run_cities.load_cities(str(p)) == [
        ["Seattle,", "WA", "--width", "2000"],
        ["Bend, OR"],
    ]


def test_a_connection_limit_above_batch_size_is_clamped_like_the_child_does(monkeypatch, capsys):
    """run_cities.py forwards both flags to streetscape_tracker.py, whose own
    parser clamps rather than refuses since issue #359. The batch driver must
    agree with it -- refusing here would reject an argv the child accepts --
    and clamps BEFORE forwarding so a batch warns once, not once per city."""
    import sys

    monkeypatch.setattr(
        sys,
        "argv",
        ["run_cities.py", "cities.txt", "--connection-limit", "150", "--batch-size", "100"],
    )
    assert run_cities.parse_args().connection_limit == 100
    assert "exceeds --batch-size" in capsys.readouterr().err

    monkeypatch.setattr(
        sys,
        "argv",
        ["run_cities.py", "cities.txt", "--connection-limit", "50", "--batch-size", "100"],
    )
    assert run_cities.parse_args().connection_limit == 50
    assert "exceeds --batch-size" not in capsys.readouterr().err
