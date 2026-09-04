"""Phase 5's pricing file: the format, its refusals, and how a record is matched.

The format is defined by this application — PromptCadence is the suite's first consumer of
``baseaicore.ModelPricing`` — so every rule in it is asserted here rather than inherited from
somewhere that already had one. The refusals matter as much as the successes: a price list is read
once at startup precisely so that a broken one is a refusal to start rather than real spend nobody
can cost.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any

import pytest
from baseaicore import UNSUPPORTED, Money, PricingSource, ProviderKind, TokenUsage, estimate_cost

from promptcadence.config import ConfigurationError
from promptcadence.services.pricing import PricingCatalog, load_pricing_records

if TYPE_CHECKING:
    from pathlib import Path

_AT = datetime(2026, 9, 3, 12, 0, tzinfo=UTC)
_DIGEST = "sha256:" + "b" * 64


def _record(**overrides: Any) -> dict[str, Any]:
    record: dict[str, Any] = {
        "provider_kind": "ollama",
        "provider_model_name": "qwen3:8b",
        "source": "provider_published",
        "observed_at": "2026-09-01T00:00:00Z",
        "rates": {
            "currency": "USD",
            "input_per_million_tokens": "2.50",
            "output_per_million_tokens": "10.00",
        },
    }
    record.update(overrides)
    return record


def _write(tmp_path: Path, *records: dict[str, Any]) -> Path:
    path = tmp_path / "prices.json"
    path.write_text(json.dumps({"records": list(records)}), encoding="utf-8")
    return path


def test_a_complete_record_loads_with_its_provenance_intact(tmp_path: Path) -> None:
    (record,) = load_pricing_records(_write(tmp_path, _record(price_tier="standard")))
    assert record.identity.provider_kind is ProviderKind.OLLAMA
    assert record.identity.provider_model_name == "qwen3:8b"
    assert record.source is PricingSource.PROVIDER_PUBLISHED
    assert record.price_tier == "standard"
    assert record.rates.input_per_million_tokens == Money.from_decimal("USD", "2.50")


def test_an_omitted_rate_is_unsupported_and_makes_the_estimate_a_floor(tmp_path: Path) -> None:
    """ "Not stated" is not "free". A call using that class cannot be fully priced (ADR-0069)."""
    (record,) = load_pricing_records(_write(tmp_path, _record()))
    assert record.rates.cache_read_per_million_tokens is UNSUPPORTED
    estimate = estimate_cost(
        TokenUsage(
            input_tokens=1_000_000,
            output_tokens=0,
            cache_write_tokens=0,
            cache_read_tokens=500,
        ),
        record,
        at=_AT,
    )
    assert estimate.is_complete is False, "a used class with no rate cannot be totalled"
    assert estimate.input_cost == Money.from_decimal("USD", "2.50")


def test_a_record_stating_a_digest_matches_only_that_digest(tmp_path: Path) -> None:
    """A record pinned to weights prices those weights, and no others that share the name."""
    catalog = PricingCatalog(
        by_tier={"t": load_pricing_records(_write(tmp_path, _record(artifact_digest=_DIGEST)))}
    )
    assert catalog.for_model(tier="t", canonical_id=f"ollama/qwen3:8b@{_DIGEST}", at=_AT)
    assert catalog.for_model(tier="t", canonical_id="ollama/qwen3:8b", at=_AT) is None
    other = "sha256:" + "c" * 64
    assert catalog.for_model(tier="t", canonical_id=f"ollama/qwen3:8b@{other}", at=_AT) is None


def test_a_record_stating_no_digest_matches_the_weights_under_any_digest(tmp_path: Path) -> None:
    """A price list is written against a product name, which survives a retag."""
    catalog = PricingCatalog(by_tier={"t": load_pricing_records(_write(tmp_path, _record()))})
    assert catalog.for_model(tier="t", canonical_id=f"ollama/qwen3:8b@{_DIGEST}", at=_AT)
    assert catalog.for_model(tier="t", canonical_id="ollama/qwen3:8b", at=_AT)
    assert catalog.for_model(tier="t", canonical_id="ollama/llama3:8b", at=_AT) is None
    assert catalog.for_model(tier="t", canonical_id="vllm/qwen3:8b", at=_AT) is None


def test_a_record_outside_its_effective_window_does_not_claim_the_instant(tmp_path: Path) -> None:
    """Extrapolating a price beyond the window it was quoted for is guessing (ADR-0030)."""
    path = _write(
        tmp_path,
        _record(effective_from="2026-10-01T00:00:00Z"),
        _record(effective_until="2026-08-01T00:00:00Z"),
    )
    catalog = PricingCatalog(by_tier={"t": load_pricing_records(path)})
    assert catalog.for_model(tier="t", canonical_id="ollama/qwen3:8b", at=_AT) is None
    assert catalog.claiming(tier="t", at=_AT) == ()
    later = datetime(2026, 10, 2, tzinfo=UTC)
    assert catalog.for_model(tier="t", canonical_id="ollama/qwen3:8b", at=later)


def test_among_records_claiming_the_instant_the_most_recently_observed_wins(
    tmp_path: Path,
) -> None:
    """Several observations of one model are a set, not a contradiction; recency decides."""
    old = _record(observed_at="2026-08-01T00:00:00Z")
    new = _record(observed_at="2026-09-02T00:00:00Z")
    new["rates"] = dict(new["rates"], input_per_million_tokens="3.00")
    catalog = PricingCatalog(by_tier={"t": load_pricing_records(_write(tmp_path, old, new))})
    chosen = catalog.for_model(tier="t", canonical_id="ollama/qwen3:8b", at=_AT)
    assert chosen is not None
    assert chosen.rates.input_per_million_tokens == Money.from_decimal("USD", "3.00")


@pytest.mark.parametrize(
    ("record", "fragment"),
    [
        ({"provider_kind": "openai"}, "not a provider this suite knows"),
        ({"provider_model_name": ""}, "names no provider_model_name"),
        ({"source": "guessed"}, "without stated provenance"),
        ({"observed_at": None}, "states no observed_at"),
        ({"observed_at": "yesterday"}, "unreadable observed_at"),
        ({"rates": {}}, "states no 'rates.currency'"),
        ({"rates": None}, "has no 'rates' object"),
    ],
)
def test_an_unusable_record_is_refused_naming_the_field(
    tmp_path: Path, record: dict[str, Any], fragment: str
) -> None:
    with pytest.raises(ConfigurationError) as raised:
        load_pricing_records(_write(tmp_path, _record(**record)))
    assert fragment in str(raised.value)


def test_a_file_that_is_not_an_object_with_records_is_refused(tmp_path: Path) -> None:
    path = tmp_path / "prices.json"
    path.write_text("[]", encoding="utf-8")
    with pytest.raises(ConfigurationError, match="must be a JSON object"):
        load_pricing_records(path)
    path.write_text("{ not json", encoding="utf-8")
    with pytest.raises(ConfigurationError, match="not valid JSON"):
        load_pricing_records(path)
    path.write_text('{"records": ["nope"]}', encoding="utf-8")
    with pytest.raises(ConfigurationError, match="is not an object"):
        load_pricing_records(path)


def test_an_empty_records_array_loads_to_nothing_rather_than_refusing(tmp_path: Path) -> None:
    """A file that states no prices prices nothing; the refusal belongs to the tier that used it."""
    path = tmp_path / "prices.json"
    path.write_text('{"records": []}', encoding="utf-8")
    assert load_pricing_records(path) == ()


def test_an_unreadable_rate_names_the_field_it_could_not_read(tmp_path: Path) -> None:
    record = _record()
    record["rates"] = dict(record["rates"], input_per_million_tokens="two fifty")
    with pytest.raises(ConfigurationError, match="rates.input_per_million_tokens"):
        load_pricing_records(_write(tmp_path, record))
