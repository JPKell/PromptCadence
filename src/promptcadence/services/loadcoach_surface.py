"""promptcadence.services.loadcoach_surface — what LoadCoach serves, and who answered a turn.

Spec §11 contract 4 (as amended in Phase 2): every LoadCoach response's execution subject is
**verified** against the tier that requested it, and provider *kind* alone cannot settle it —
``openai_compatible`` is both a local llama.cpp server and a paid remote endpoint. The egress
class is resolved here, at the HTTP boundary, and the resolution today is identity:

    While LoadCoach serves one configured provider, verifying that the response's provider *is*
    the configured one is the verification.

Two facts make that sound in the single-provider era, and both are LoadCoach's own defaults:
``[providers] allow_remote = false`` (a remote provider is an explicit opt-in that LC-E1 turns
into registration), and every shipped task profile's ``allow_remote_providers = false``. The
configured provider is therefore local, a response from it is local, and a response from any
*other* provider kind is treated as remote — the conservative reading, which on a local tier is
the ``tier_violation`` that halts.

What LoadCoach's wire does **not** carry is the provider's own ``is_remote`` — routing knows it
(``ProviderFacts.is_remote``), ``/health``, ``/models``, ``/system/status`` and the routing
explanation do not render it. That is the obligation Phase 2 placed on LC-E1: the serving
provider's identity on every response. Until then :attr:`ProviderSurface.has_remote_provider` is
``False`` by construction, and ``/system/status`` (which C4 named as the source) is not where the
answer lives — ``/models`` is.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING

from baseaicore import ProviderKind

from promptcadence.domain.deviation import ExecutionSubject
from promptcadence.domain.errors import LoadCoachError
from promptcadence.domain.tiers import EgressClass

if TYPE_CHECKING:
    from promptcadence.infrastructure.loadcoach import LoadCoachClient, ModelInfo

__all__ = ["ProviderSurface", "load_provider_surface", "resolve_subject"]


@dataclass(frozen=True, slots=True)
class ProviderSurface:
    """The provider kinds LoadCoach's registry names — the facts a subject is verified against.

    Attributes:
        provider_kinds: Every :class:`baseaicore.ProviderKind` at least one registered model is
            served by.
        unknown_kinds: Kind strings this suite does not name. A registry with one of these
            cannot be verified against, and says so.
        model_count: How many models the registry lists, available or not.
    """

    provider_kinds: frozenset[ProviderKind]
    unknown_kinds: frozenset[str]
    model_count: int

    @property
    def single_kind(self) -> ProviderKind | None:
        """The one configured provider's kind, or ``None`` when there is not exactly one.

        ``None`` for an empty registry (nothing to verify against) and for several kinds (LC-E1
        has landed and identity is no longer the verification — the response must carry the
        provider's name, which this build does not read).
        """
        if len(self.provider_kinds) == 1 and not self.unknown_kinds:
            return next(iter(self.provider_kinds))
        return None

    @property
    def has_remote_provider(self) -> bool:
        """Whether LoadCoach has a remote provider registered — ``False`` until LC-E1.

        Not read from the wire, because the wire does not carry it (module docstring). In the
        single-provider era LoadCoach's own defaults keep the one provider local; the day a
        second provider is registrable, this becomes a field LoadCoach reports and this property
        reads it.
        """
        return False


def load_provider_surface(client: LoadCoachClient) -> ProviderSurface:
    """Read the provider surface from ``GET /models``.

    Args:
        client: The LoadCoach client.

    Returns:
        The surface. An empty registry is a surface with no kinds, not an error: it is the
        loop's job to refuse a turn it cannot verify, with the reason.

    Raises:
        LoadCoachUnavailableError: LoadCoach could not be reached.
        LoadCoachError: LoadCoach answered with something other than a model list.
    """
    known: set[ProviderKind] = set()
    unknown: set[str] = set()
    entries = client.models()
    for entry in entries:
        try:
            known.add(ProviderKind(entry.provider_kind))
        except ValueError:
            unknown.add(entry.provider_kind)
    return ProviderSurface(
        provider_kinds=frozenset(known), unknown_kinds=frozenset(unknown), model_count=len(entries)
    )


def resolve_subject(model: ModelInfo, *, surface: ProviderSurface) -> ExecutionSubject:
    """Resolve who answered a turn, with its egress class **verified** against the surface.

    Args:
        model: The response's ``model`` block.
        surface: What LoadCoach serves, read before the turn.

    Returns:
        The subject: ``LOCAL`` when the response's provider kind is the one configured provider's
        kind; ``REMOTE`` — the conservative reading — when it is any other kind.
        ``provider_name`` is ``None`` until LC-E1 carries it.

    Raises:
        LoadCoachError: If the surface cannot be verified against — an empty registry, an
            unknown kind, or several kinds (LC-E1's era, which needs the provider's identity on
            the response). The turn that produced the answer is then halted rather than recorded
            as verified, because "verified" is the one thing this function must never assume.
    """
    configured = surface.single_kind
    if configured is None:
        message = (
            "the execution subject cannot be verified: LoadCoach's registry names "
            f"{len(surface.provider_kinds)} known provider kind(s) and "
            f"{len(surface.unknown_kinds)} unknown; contract 4 needs exactly one configured "
            "provider until LC-E1 carries the serving provider's identity on every response"
        )
        raise LoadCoachError(
            message,
            details={
                "reason": "subject_unverifiable",
                "provider_kinds": sorted(kind.value for kind in surface.provider_kinds),
                "unknown_kinds": sorted(surface.unknown_kinds),
                "model_count": surface.model_count,
            },
        )
    kind = model.provider_kind
    egress = EgressClass.LOCAL if kind is configured else EgressClass.REMOTE
    return ExecutionSubject(
        model_canonical_id=model.canonical_id,
        provider_kind=kind,
        egress_class=egress,
        provider_name=None,
    )
