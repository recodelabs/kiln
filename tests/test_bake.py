import pytest

from kiln.bake import (
    BakeError,
    LevelSpec,
    parse_alias_args,
    parse_country_arg,
    parse_level_arg,
    slugify,
)


def test_slugify_lowercases_folds_and_hyphenates():
    assert slugify("Alkaleri East") == "alkaleri-east"
    assert slugify("Grand-Popo / Centre") == "grand-popo-centre"
    assert slugify("  N'Djaména ") == "n-djamena"


def test_slugify_collapses_runs_and_strips_edges():
    assert slugify("A  --  B") == "a-b"


def test_parse_country_arg():
    assert parse_country_arg("Nigeria=NGA") == ("Nigeria", "NGA")


def test_parse_country_arg_rejects_missing_code():
    with pytest.raises(BakeError):
        parse_country_arg("Nigeria")


def test_parse_level_arg_with_and_without_code():
    assert parse_level_arg("state=state:statecode") == LevelSpec(
        name="state", name_prop="state", code_prop="statecode"
    )
    assert parse_level_arg("ward=ward") == LevelSpec(
        name="ward", name_prop="ward", code_prop=None
    )


def test_parse_level_arg_rejects_bad_shapes():
    for bad in ("ward", "ward=", "=ward", "ward=a:b:c"):
        with pytest.raises(BakeError):
            parse_level_arg(bad)


def test_parse_alias_args():
    assert parse_alias_args(["lga=lga_alt_names", "ward=ward_alt_names"]) == {
        "lga": "lga_alt_names",
        "ward": "ward_alt_names",
    }
    with pytest.raises(BakeError):
        parse_alias_args(["lga"])
