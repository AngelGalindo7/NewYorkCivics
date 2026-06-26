"""Offline tests for ingest.deliver.subscribers — no network calls."""

from __future__ import annotations

from unittest.mock import patch

import pytest

from ingest.normalize.geocode import GeoResult

_GOOD_GEO = GeoResult(
    ok=True,
    bbl="1016500030",
    bin=None,
    community_district="111",
    latitude=40.7969,
    longitude=-73.9410,
)

_BAD_GEO = GeoResult(ok=False, reason="address not found in GeoSearch")


def test_add_subscriber_stores_row(tmp_path):
    from ingest.deliver.subscribers import add_subscriber, load_subscribers

    csv_path = tmp_path / "subs.csv"
    with patch("ingest.deliver.subscribers.geocode", return_value=_GOOD_GEO):
        sub = add_subscriber(
            "alice@example.com",
            "123 East 116th Street, New York, NY 10029",
            name="Alice",
            csv_path=csv_path,
        )

    assert sub["email"] == "alice@example.com"
    assert sub["bbl"] == "1016500030"
    assert sub["latitude"] == pytest.approx(40.7969)
    assert sub["community_district"] == "111"
    assert sub["zip"] == "10029"

    loaded = load_subscribers(csv_path=csv_path)
    assert len(loaded) == 1
    assert loaded[0]["email"] == "alice@example.com"
    assert loaded[0]["bbl"] == "1016500030"
    assert loaded[0]["latitude"] == pytest.approx(40.7969)
    assert loaded[0]["zip"] == "10029"


def test_add_subscriber_upsert_replaces_existing_email(tmp_path):
    """A second add_subscriber call with the same email replaces the prior row."""
    from ingest.deliver.subscribers import add_subscriber, load_subscribers

    csv_path = tmp_path / "subs.csv"
    with patch("ingest.deliver.subscribers.geocode", return_value=_GOOD_GEO):
        add_subscriber("alice@example.com", "123 East 116th St", csv_path=csv_path)

    updated_geo = GeoResult(
        ok=True,
        bbl="1016500099",
        community_district="111",
        latitude=40.80,
        longitude=-73.94,
    )
    with patch("ingest.deliver.subscribers.geocode", return_value=updated_geo):
        add_subscriber("alice@example.com", "456 East 116th St", csv_path=csv_path)

    loaded = load_subscribers(csv_path=csv_path)
    assert len(loaded) == 1
    assert loaded[0]["address"] == "456 East 116th St"
    assert loaded[0]["bbl"] == "1016500099"


def test_add_subscriber_geocode_failure_raises(tmp_path):
    """An unresolvable address raises ValueError; no CSV is written."""
    from ingest.deliver.subscribers import add_subscriber

    csv_path = tmp_path / "subs.csv"
    with (
        patch("ingest.deliver.subscribers.geocode", return_value=_BAD_GEO),
        pytest.raises(ValueError, match="Could not geocode"),
    ):
        add_subscriber("bob@example.com", "not a real place", csv_path=csv_path)

    assert not csv_path.exists()


def test_load_subscribers_returns_empty_list_when_file_absent(tmp_path):
    from ingest.deliver.subscribers import load_subscribers

    result = load_subscribers(csv_path=tmp_path / "nonexistent.csv")
    assert result == []


def test_multiple_subscribers_stored_and_loaded(tmp_path):
    """Two different subscribers coexist in the CSV."""
    from ingest.deliver.subscribers import add_subscriber, load_subscribers

    csv_path = tmp_path / "subs.csv"
    geo_a = GeoResult(
        ok=True, bbl="1016500030", community_district="111", latitude=40.7969, longitude=-73.94
    )
    geo_b = GeoResult(
        ok=True, bbl="1016500099", community_district="111", latitude=40.80, longitude=-73.94
    )

    with patch("ingest.deliver.subscribers.geocode", return_value=geo_a):
        add_subscriber("alice@example.com", "123 E 116th St", csv_path=csv_path)
    with patch("ingest.deliver.subscribers.geocode", return_value=geo_b):
        add_subscriber("bob@example.com", "456 E 116th St", csv_path=csv_path)

    loaded = load_subscribers(csv_path=csv_path)
    assert len(loaded) == 2
    assert {s["email"] for s in loaded} == {"alice@example.com", "bob@example.com"}


def test_subscriber_without_zip_in_address(tmp_path):
    """An address with no ZIP stores zip=None gracefully."""
    from ingest.deliver.subscribers import add_subscriber

    csv_path = tmp_path / "subs.csv"
    geo = GeoResult(
        ok=True, bbl="1016500030", community_district="111", latitude=40.7969, longitude=-73.94
    )

    with patch("ingest.deliver.subscribers.geocode", return_value=geo):
        sub = add_subscriber(
            "carol@example.com",
            "E 116th St & Lex Ave, New York, NY",
            csv_path=csv_path,
        )

    assert sub["zip"] is None


def test_load_subscribers_dict_shape_matches_build_digest_contract(tmp_path):
    """Loaded subscriber has all fields build_digest expects."""
    from ingest.deliver.subscribers import add_subscriber, load_subscribers

    csv_path = tmp_path / "subs.csv"
    with patch("ingest.deliver.subscribers.geocode", return_value=_GOOD_GEO):
        add_subscriber("alice@example.com", "123 East 116th St, NY 10029", csv_path=csv_path)

    sub = load_subscribers(csv_path=csv_path)[0]
    for field in ("email", "address", "bbl", "latitude", "longitude", "zip", "community_district"):
        assert field in sub, f"missing field: {field}"
