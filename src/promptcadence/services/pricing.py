"""promptcadence.services.pricing — where each tier's ``pricing_file`` is, and what it holds.

ADR-0030 rule 1 is that **cost is derived and never stored**, which only works if the price the
derivation used can be found again. This module is where PromptCadence finds it: a remote tier
names a ``pricing_file``, that file holds :class:`~baseaicore.ModelPricing` observations with their
provenance, and every debit and every re-costing prices the same call against the same record.

**The reader itself is no longer here.** PromptCadence was the suite's first consumer of
``ModelPricing`` and wrote both the format and its parser; ADR-0072 then fixed the format as a
suite-wide record and named the trigger that would move the parser — a second consumer needing the
reader itself. IdeaPress transcribed it at row J1, and row K4 moved it into ``loadledger.pricing``,
where a single reader keeps the ``pricing_hash`` join sound across both applications (ADR-0110).
The format, its refusals and the matching rules live there and are documented there.

What stays here is this application's edge, and only that:

* **Which key names each file.** A remote tier's ``pricing_file`` is PromptCadence configuration,
  startup validation has already refused a remote tier that names none, and a **local** tier's
  catalogue is deliberately ignored — a local model's cost is ``UNSUPPORTED``, never ``$0.00``
  (ADR-0016), so honouring a price list on one would be a fabricated figure.
* **How a broken file is reported.** :class:`~loadledger.PricingFileError` is re-raised as this
  application's :class:`~promptcadence.config.ConfigurationError`, so an unreadable price list is
  the refusal to start ADR-0072 §7 requires, in the vocabulary an operator is already reading.
* **The container.** A trajectory may run under any of several configured tiers, so the catalogue
  is a per-tier map rather than one flat list, and :meth:`PricingCatalog.claiming` exposes the
  worst-case set a pre-flight estimate costs against before a backend has chosen a model.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING

from loadledger import PricingFileError
from loadledger.pricing import load_pricing_records as _load_pricing_records
from loadledger.pricing import price_for_model, records_claiming

from promptcadence.config import ConfigurationError

if TYPE_CHECKING:
    from datetime import datetime

    from baseaicore import ModelPricing

    from promptcadence.config import Settings

__all__ = ["PricingCatalog", "load_pricing_records"]


def load_pricing_records(path: Path) -> tuple[ModelPricing, ...]:
    """Read one ADR-0072 pricing file, reporting a broken one as a configuration mistake.

    Args:
        path: The ``[tiers.<name>] pricing_file`` to read.

    Returns:
        Every record in the file, in file order. An empty ``records`` array is legitimate and
        loads to an empty tuple — a file that states no prices is a file that prices nothing, and
        the refusal for that belongs to the tier that used it, not to the reader.

    Raises:
        ConfigurationError: If the file is missing, is not readable, is not JSON, is not an object
            with a ``records`` array, or holds a record ADR-0072's rules cannot turn into a
            :class:`~baseaicore.ModelPricing`. The package's message and ``details`` are kept
            whole; only the exception type changes, because a price list an operator wrote is part
            of this application's configuration and a refusal in a different vocabulary would send
            them looking in the wrong place. Every one of these is a startup refusal by design: a
            price list discovered to be unreadable mid-trajectory would leave real spend that
            cannot be costed.
    """
    try:
        return _load_pricing_records(path)
    except PricingFileError as exc:
        raise ConfigurationError(exc.message, details=dict(exc.details)) from exc


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
        return records_claiming(self.by_tier.get(tier, ()), at=at)

    def for_model(self, *, tier: str, canonical_id: str, at: datetime) -> ModelPricing | None:
        """Return the price observation to cost a call on ``tier`` by ``canonical_id`` at ``at``.

        Delegates the matching rules to :func:`loadledger.pricing.price_for_model`, where they are
        documented: the provider kind and the provider's own model name must agree, a record that
        states a digest matches only that digest, one that states none matches those weights under
        any digest, and among the records claiming the instant the most recently observed wins.

        Args:
            tier: The tier whose file to look in.
            canonical_id: LoadCoach's ``model.canonical_id`` — ``provider/name`` or
                ``provider/name@sha256:…`` (ADR-0008).
            at: The instant to price at, normally when the call happened, so re-costing history
                later finds the same record.

        Returns:
            The record to cost against, or ``None`` when the tier holds none for these weights.
            ``None`` is not free: the caller records the usage unpriced and says why.
        """
        return price_for_model(self.by_tier.get(tier, ()), canonical_id=canonical_id, at=at)
