"""promptcadence.services.pricing — reading a tier's ``pricing_file`` into ``ModelPricing``.

ADR-0030 rule 1 is that **cost is derived and never stored**, which only works if the price the
derivation used can be found again. This module is where PromptCadence finds it: a remote tier
names a ``pricing_file``, that file holds :class:`~baseaicore.ModelPricing` observations with their
provenance, and every debit and every re-costing prices the same call against the same record.

**PromptCadence is the suite's first consumer of ``ModelPricing``**, so the file format is defined
here rather than mirrored from somewhere. It is JSON (ADR-0019: config is TOML, data is JSON), one
object with a ``records`` array, and each record is a ``ModelPricing`` written out field for
field — nothing summarized, nothing defaulted silently:

.. code-block:: json

    {
      "records": [
        {
          "provider_kind": "openai",
          "provider_model_name": "gpt-4o",
          "artifact_digest": null,
          "source": "provider_published",
          "observed_at": "2026-09-01T00:00:00Z",
          "effective_from": null,
          "effective_until": null,
          "price_tier": "standard",
          "region": null,
          "rates": {
            "currency": "USD",
            "input_per_million_tokens": "2.50",
            "output_per_million_tokens": "10.00",
            "cache_write_per_million_tokens": "3.125",
            "cache_read_per_million_tokens": "0.25"
          }
        }
      ]
    }

Three of its rules are load-bearing rather than stylistic:

* **Rates are decimal strings, never floats.** ``"2.50"`` goes through
  :meth:`baseaicore.Money.from_decimal` to whole nanos. A JSON float would already have lost the
  value before this module saw it, and a budget whose total does not equal the sum of its rows is
  the failure the integer arithmetic everywhere else exists to prevent.
* **An omitted rate is UNSUPPORTED, not zero.** A price list that states no cache-read rate cannot
  price a call that read from cache; :func:`baseaicore.estimate_cost` returns an UNSUPPORTED
  component and an untotalled estimate, which is exactly the ADR-0069 floor. Writing ``"0"`` there
  would be a fabricated zero (ADR-0016) that silently claims cache reads are free.
* **A record is an observation, not a fact about a model.** Several records may name the same
  weights — a standard tier and a batch tier, two regions, a superseded price and its replacement.
  :meth:`PricingCatalog.for_model` resolves them by the window they claim and, among those still
  claiming the instant, by which was observed most recently.

**No network, ever.** A pricing file is read from disk at startup and never fetched, so a
configuration that cannot be read is a refusal to start rather than a call that turns out to be
unpriceable halfway through a trajectory.
"""

from __future__ import annotations

import json
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Any, Final

from baseaicore import (
    UNSUPPORTED,
    ModelIdentity,
    ModelPricing,
    Money,
    PricingSource,
    ProviderKind,
    TokenRates,
    ValidationError,
    from_rfc3339,
    normalize_digest,
)

from promptcadence.config import ConfigurationError

if TYPE_CHECKING:
    from datetime import datetime

    from promptcadence.config import Settings

__all__ = ["PricingCatalog", "load_pricing_records"]

_RATE_FIELDS: Final = (
    "input_per_million_tokens",
    "output_per_million_tokens",
    "cache_write_per_million_tokens",
    "cache_read_per_million_tokens",
)


def _refuse(message: str, **details: Any) -> ConfigurationError:
    """Build the one refusal shape this module raises, naming the file and the field."""
    return ConfigurationError(message, details=details)


def _rates_of(document: Mapping[str, Any], *, path: Path, index: int) -> TokenRates:
    """Read one record's ``rates`` block, keeping "not stated" distinct from "free"."""
    block = document.get("rates")
    if not isinstance(block, Mapping):
        message = f"{path}: record {index} has no 'rates' object"
        raise _refuse(message, file=str(path), record=index, field="rates")
    currency = block.get("currency")
    if not isinstance(currency, str) or not currency.strip():
        message = f"{path}: record {index} states no 'rates.currency'"
        raise _refuse(message, file=str(path), record=index, field="rates.currency")
    stated: dict[str, Money] = {}
    for name in _RATE_FIELDS:
        raw = block.get(name)
        if raw is None:
            continue
        if not isinstance(raw, str):
            message = (
                f"{path}: record {index} states rates.{name}={raw!r}; a rate is a decimal "
                '*string* such as "2.50". A JSON number is a float, and a price that arrived as '
                "a float has already lost the value this suite's integer arithmetic protects."
            )
            raise _refuse(message, file=str(path), record=index, field=f"rates.{name}")
        try:
            stated[name] = Money.from_decimal(currency, raw)
        except ValidationError as exc:
            message = f"{path}: record {index} states an unreadable rates.{name}: {exc.message}"
            raise _refuse(message, file=str(path), record=index, field=f"rates.{name}") from exc
    try:
        return TokenRates(
            currency=currency,
            input_per_million_tokens=stated.get("input_per_million_tokens", UNSUPPORTED),
            output_per_million_tokens=stated.get("output_per_million_tokens", UNSUPPORTED),
            cache_write_per_million_tokens=stated.get(
                "cache_write_per_million_tokens", UNSUPPORTED
            ),
            cache_read_per_million_tokens=stated.get("cache_read_per_million_tokens", UNSUPPORTED),
        )
    except ValidationError as exc:
        message = f"{path}: record {index} has unusable rates: {exc.message}"
        raise _refuse(message, file=str(path), record=index, field="rates") from exc


def _instant(document: Mapping[str, Any], name: str, *, path: Path, index: int) -> datetime | None:
    """Read one RFC 3339 field, refusing a value that is present but unreadable."""
    raw = document.get(name)
    if raw is None:
        return None
    if not isinstance(raw, str):
        message = f"{path}: record {index} states {name}={raw!r}; expected an RFC 3339 string"
        raise _refuse(message, file=str(path), record=index, field=name)
    try:
        return from_rfc3339(raw)
    except ValidationError as exc:
        message = f"{path}: record {index} states an unreadable {name}: {exc.message}"
        raise _refuse(message, file=str(path), record=index, field=name) from exc


def _record_of(document: Mapping[str, Any], *, path: Path, index: int) -> ModelPricing:
    """Build one :class:`~baseaicore.ModelPricing` from one record object."""
    kind_raw = str(document.get("provider_kind"))
    try:
        kind = ProviderKind(kind_raw)
    except ValueError as exc:
        known = ", ".join(sorted(member.value for member in ProviderKind))
        message = (
            f"{path}: record {index} names provider_kind={kind_raw!r}, which is not a provider "
            f"this suite knows ({known})"
        )
        raise _refuse(message, file=str(path), record=index, field="provider_kind") from exc
    name = document.get("provider_model_name")
    if not isinstance(name, str) or not name.strip():
        message = f"{path}: record {index} names no provider_model_name"
        raise _refuse(message, file=str(path), record=index, field="provider_model_name")
    source_raw = str(document.get("source"))
    try:
        source = PricingSource(source_raw)
    except ValueError as exc:
        known = ", ".join(sorted(member.value for member in PricingSource))
        message = (
            f"{path}: record {index} names source={source_raw!r}; a price without stated "
            f"provenance cannot be weighed (ADR-0030). Expected one of: {known}"
        )
        raise _refuse(message, file=str(path), record=index, field="source") from exc
    observed_at = _instant(document, "observed_at", path=path, index=index)
    if observed_at is None:
        message = (
            f"{path}: record {index} states no observed_at; a price with no date is a price "
            "nobody can tell has gone stale"
        )
        raise _refuse(message, file=str(path), record=index, field="observed_at")
    digest_raw = document.get("artifact_digest")
    try:
        digest = normalize_digest(digest_raw) if isinstance(digest_raw, str) else None
        identity = ModelIdentity(
            provider_kind=kind, provider_model_name=name, artifact_digest=digest
        )
        return ModelPricing(
            identity=identity,
            rates=_rates_of(document, path=path, index=index),
            source=source,
            observed_at=observed_at,
            effective_from=_instant(document, "effective_from", path=path, index=index),
            effective_until=_instant(document, "effective_until", path=path, index=index),
            price_tier=document.get("price_tier") or None,
            region=document.get("region") or None,
        )
    except ValidationError as exc:
        message = f"{path}: record {index} is not a usable price observation: {exc.message}"
        raise _refuse(message, file=str(path), record=index) from exc


def load_pricing_records(path: Path) -> tuple[ModelPricing, ...]:
    """Read one pricing file into price observations.

    Args:
        path: The ``[tiers.<name>] pricing_file`` to read.

    Returns:
        Every record in the file, in file order. An empty ``records`` array is legitimate and
        loads to an empty tuple — a file that states no prices is a file that prices nothing, and
        the refusal for that belongs to the tier that used it, not to the reader.

    Raises:
        ConfigurationError: If the file is missing, is not readable, is not JSON, is not an object
            with a ``records`` array, or holds a record this module cannot turn into a
            :class:`~baseaicore.ModelPricing`. Every one of these is a startup refusal by design:
            a price list discovered to be unreadable mid-trajectory would leave real spend that
            cannot be costed.
    """
    try:
        text = path.read_text(encoding="utf-8")
    except OSError as exc:
        message = f"pricing file {path} cannot be read: {exc.strerror or exc}"
        raise _refuse(message, file=str(path)) from exc
    try:
        document = json.loads(text)
    except json.JSONDecodeError as exc:
        message = f"pricing file {path} is not valid JSON: {exc}"
        raise _refuse(message, file=str(path)) from exc
    if not isinstance(document, dict) or not isinstance(document.get("records"), list):
        message = (
            f"pricing file {path} must be a JSON object with a 'records' array of "
            "ModelPricing observations"
        )
        raise _refuse(message, file=str(path), field="records")
    records = []
    for index, entry in enumerate(document["records"]):
        if not isinstance(entry, dict):
            message = f"{path}: record {index} is not an object"
            raise _refuse(message, file=str(path), record=index)
        records.append(_record_of(entry, path=path, index=index))
    return tuple(records)


@dataclass(frozen=True, slots=True)
class PricingCatalog:
    """Every configured tier's price observations, loaded once at startup.

    Built by :meth:`from_settings` before anything runs, so an unreadable price list is a refusal
    to start rather than a trajectory that spends money nobody can account for. Local tiers hold no
    records at all and are meant to: a local model's cost is ``UNSUPPORTED``, never ``$0.00``
    (ADR-0016), and :meth:`for_model` returning ``None`` for one is the correct answer, not a gap.

    Attributes:
        by_tier: Tier name to that tier's records, in file order.
    """

    by_tier: Mapping[str, tuple[ModelPricing, ...]]

    @classmethod
    def from_settings(cls, settings: Settings) -> PricingCatalog:
        """Load every configured tier's ``pricing_file``.

        Args:
            settings: The validated configuration. Startup validation has already refused a remote
                tier whose ``pricing_file`` is empty, so a remote tier reaching here names a path.

        Returns:
            The catalogue. A local tier maps to an empty tuple whether or not it named a file —
            a price list on a local tier prices nothing, and honouring it would be the fabricated
            zero ADR-0016 forbids.

        Raises:
            ConfigurationError: If any named file cannot be read or holds an unusable record.
        """
        loaded: dict[str, tuple[ModelPricing, ...]] = {}
        for name, tier in settings.tiers.items():
            if not tier.remote or not tier.pricing_file.strip():
                loaded[name] = ()
                continue
            loaded[name] = load_pricing_records(Path(tier.pricing_file).expanduser())
        return cls(by_tier=loaded)

    def claiming(self, *, tier: str, at: datetime) -> tuple[ModelPricing, ...]:
        """Return every record configured for ``tier`` that claims the instant ``at``.

        What a **pre-flight estimate** has to work with. The model that will answer is LoadCoach's
        to choose and is not known until it has answered, so an estimate cannot name one record;
        the caller costs the estimate against all of these and takes the largest, because the only
        estimate that cannot under-state a budget is the tier's worst case. Under-stating is the
        failure that matters: an over-stated estimate refuses a step that would have fitted and
        says which cap refused it, while an under-stated one crosses the cap and says nothing.

        Args:
            tier: The tier whose file to look in.
            at: The instant to price at.

        Returns:
            The records still claiming ``at``, in file order. Empty for a local tier, and for a
            remote tier whose records have all expired.
        """
        return tuple(record for record in self.by_tier.get(tier, ()) if _claims(record, at))

    def for_model(self, *, tier: str, canonical_id: str, at: datetime) -> ModelPricing | None:
        """Return the price observation to cost a call on ``tier`` by ``canonical_id`` at ``at``.

        Matching is on the identity's two stable halves — the provider kind and the provider's own
        model name — with the artifact digest as an *optional* narrowing: a record that states a
        digest matches only that digest, and a record that states none matches the weights under
        any digest. That asymmetry is deliberate. A price list is usually written against a
        provider's product name, which survives a retag; pinning every record to a digest would
        make a routine retag silently unpriceable, while ignoring a digest a record *did* state
        would price one set of weights with another's rates.

        Args:
            tier: The tier whose file to look in.
            canonical_id: LoadCoach's ``model.canonical_id`` — ``provider/name`` or
                ``provider/name@sha256:…`` (ADR-0008).
            at: The instant to price at, normally when the call happened, so re-costing history
                later finds the same record.

        Returns:
            The most recently *observed* record that claims ``at``, or ``None`` when the tier holds
            no record for these weights. ``None`` is not free: the caller records the usage
            unpriced and says why.
        """
        candidates = [
            record
            for record in self.by_tier.get(tier, ())
            if _matches(record, canonical_id) and _claims(record, at)
        ]
        if not candidates:
            return None
        return max(candidates, key=lambda record: record.observed_at)


def _split(canonical_id: str) -> tuple[str, str, str | None]:
    """Split ``provider/name`` or ``provider/name@sha256:…`` without parsing the name itself."""
    prefix, _, remainder = canonical_id.partition("/")
    name, at, digest = remainder.rpartition("@")
    if not at or not digest.startswith("sha256:"):
        return prefix, remainder, None
    return prefix, name, digest


def _matches(record: ModelPricing, canonical_id: str) -> bool:
    """Whether one record's identity names the weights ``canonical_id`` names."""
    kind, name, digest = _split(canonical_id)
    if record.identity.provider_kind.value != kind or record.identity.provider_model_name != name:
        return False
    return record.identity.artifact_digest in (None, digest)


def _claims(record: ModelPricing, at: datetime) -> bool:
    """Whether one record says it applies at ``at``. An unstated bound is not a bound."""
    if record.effective_from is not None and at < record.effective_from:
        return False
    return not (record.effective_until is not None and at > record.effective_until)
