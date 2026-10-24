"""The model catalog and the reviewed price table describe the same models."""

from decimal import Decimal

from worldd.lane import load_catalog, load_prices, price_table_id


def test_every_offered_model_has_a_reviewed_price_and_valid_efforts():
    catalog, prices = load_catalog(), load_prices()["models"]
    levels = catalog["effort_levels"]
    assert set(catalog["models"]) == set(prices)
    for model, entry in catalog["models"].items():
        assert entry["group"] in {"current", "earlier", "retired"}, model
        assert set(entry["efforts"]) <= set(levels), model
        # Effort levels stay in their natural order, low to high.
        assert entry["efforts"] == sorted(entry["efforts"], key=levels.index), model
        if entry["efforts"]:
            assert entry["default_effort"] in entry["efforts"], model
        else:
            assert "default_effort" not in entry, model


def test_tiered_rows_reprice_every_rate_above_their_threshold():
    for model, row in load_prices()["models"].items():
        tier = row.get("long_context")
        if not tier:
            continue
        assert 0 < tier["above_prompt_tokens"] < row["context_window"], model
        for name in ("input", "output", "cache_read", "cache_write", "cache_write_1h"):
            if name in row:
                assert Decimal(tier[name]) >= Decimal(row[name]), (model, name)


def test_catalog_labels_do_not_change_the_price_table_identity(monkeypatch, tmp_path):
    before = price_table_id(load_prices(), perception_prices={})
    edited = tmp_path / "models.json"
    edited.write_text('{"models": {}, "effort_levels": []}')
    monkeypatch.setenv("WORLD_MODEL_CATALOG", str(edited))
    assert load_catalog() == {"models": {}, "effort_levels": []}
    assert price_table_id(load_prices(), perception_prices={}) == before
