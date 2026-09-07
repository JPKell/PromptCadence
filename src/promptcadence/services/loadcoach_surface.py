"""promptcadence.services.loadcoach_surface — what LoadCoach serves, and who answered a turn.

Spec §11 contract 4 (as amended in Phase 2): every LoadCoach response's execution subject is
**verified** against the tier that requested it, and provider *kind* alone cannot settle it —
``openai_compatible`` is both a local llama.cpp server and a paid remote endpoint. The egress
class is resolved here, at the HTTP boundary.

Two eras, one function. **LoadCoach 1.1 (LC-E1)** declares a registration's egress class and
carries it on every response's ``model`` block as ``is_remote``, beside the registration's
``provider_name`` (ADR-0055 rule 4: declared, never inferred from the kind). When a response
carries it, that is the verification: the response's kind must be one the registry names, and
the egress class is what the registration declared. **Before it** — a response with no
``is_remote`` — the single-provider rule of the first era applies: while LoadCoach serves one
configured provider, verifying that the response's provider *is* the configured one is the
verification, and a response from any other kind is treated as remote, the conservative reading.

The **remote-provider fact** — whether LoadCoach has a registration declaring ``remote = true`` —
is read from ``GET /models`` when its entries carry ``is_remote``. LoadCoach 1.1.0 records the
flag on its ``models`` table and does not render it in that listing (found at I2, ADR-0098's
correction), so on that version the fact reads ``False`` until LoadCoach renders it: a remote
registration is invisible before its first turn, and a remote tier stays unavailable-honest with
``loadcoach_has_no_remote_provider``. That is the safe default, and it is never inferred from a
provider kind.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING

from baseaicore import ProviderKind

from promptcadence.domain.deviation import ExecutionSubject
from promptcadence.domain.errors import LoadCoachError, LoadCoachUnavailableError
from promptcadence.domain.tiers import EgressClass

if TYPE_CHECKING:
    from promptcadence.infrastructure.loadcoach import LoadCoachClient, ModelInfo

__all__ = [
    "ProviderSurface",
    "load_provider_surface",
    "remote_provider_registered",
    "resolve_subject",
]


@dataclass(frozen=True, slots=True)
class ProviderSurface:
    """The provider kinds LoadCoach's registry names — the facts a subject is verified against.

    Attributes:
        provider_kinds: Every :class:`baseaicore.ProviderKind` at least one registered model is
            served by.
        unknown_kinds: Kind strings this suite does not name. A registry with one of these
            cannot be verified against, and says so.
        model_count: How many models the registry lists, available or not.
        remote_provider_names: The names of the registrations whose ``/models`` entries declare
            ``is_remote``. Empty when none does — and empty on a LoadCoach whose listing does not
            render the flag, which is the safe reading (module docstring).
    """

    provider_kinds: frozenset[ProviderKind]
    unknown_kinds: frozenset[str]
    model_count: int
    remote_provider_names: frozenset[str] = frozenset()

    @property
    def single_kind(self) -> ProviderKind | None:
        """The one configured provider's kind, or ``None`` when there is not exactly one.

        ``None`` for an empty registry (nothing to verify against) and for several kinds — the
        mixed registry LC-E1 makes possible, where a response must carry ``is_remote`` to be
        verified.
        """
        if len(self.provider_kinds) == 1 and not self.unknown_kinds:
            return next(iter(self.provider_kinds))
        return None

    @property
    def has_remote_provider(self) -> bool:
        """Whether LoadCoach has a registration declaring itself remote (ADR-0098 rule 1)."""
        return bool(self.remote_provider_names)


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
    remote: set[str] = set()
    entries = client.models()
    for entry in entries:
        try:
            known.add(ProviderKind(entry.provider_kind))
        except ValueError:
            unknown.add(entry.provider_kind)
        if entry.is_remote is True:
            remote.add(entry.provider_name or entry.provider_kind)
    return ProviderSurface(
        provider_kinds=frozenset(known),
        unknown_kinds=frozenset(unknown),
        model_count=len(entries),
        remote_provider_names=frozenset(remote),
    )


def remote_provider_registered(client: LoadCoachClient) -> bool:
    """The remote-provider fact, read from LoadCoach, with the safe default on any failure.

    Args:
        client: The LoadCoach client.

    Returns:
        ``True`` only when ``GET /models`` names at least one registration declaring
        ``is_remote``. An unreachable LoadCoach, an unreadable answer, or a listing that does not
        render the flag all read ``False``: a remote tier is then unavailable with
        ``loadcoach_has_no_remote_provider``, which is a recorded refusal rather than a guess.
    """
    try:
        return load_provider_surface(client).has_remote_provider
    except (LoadCoachUnavailableError, LoadCoachError):
        return False


def resolve_subject(model: ModelInfo, *, surface: ProviderSurface) -> ExecutionSubject:
    """Resolve who answered a turn, with its egress class **verified** against the surface.

    Args:
        model: The response's ``model`` block.
        surface: What LoadCoach serves, read before the turn.

    Returns:
        The subject. When the response declares ``is_remote`` (LoadCoach 1.1), the egress class
        is the declaration and ``provider_name`` is the registration's, provided the response's
        kind is one the registry names. Otherwise the single-provider rule: ``LOCAL`` when the
        response's kind is the one configured provider's kind, ``REMOTE`` — the conservative
        reading — when it is any other kind, and ``provider_name`` is ``None``.

    Raises:
        LoadCoachError: If the surface cannot be verified against — an empty registry, an
            unknown kind, a response whose kind the registry does not name, or a mixed registry
            answering without ``is_remote``. The turn that produced the answer is then halted
            rather than recorded as verified, because "verified" is the one thing this function
            must never assume.
    """
    kind = model.provider_kind
    if model.is_remote is not None:
        if kind in surface.provider_kinds and not surface.unknown_kinds:
            return ExecutionSubject(
                model_canonical_id=model.canonical_id,
                provider_kind=kind,
                egress_class=EgressClass.REMOTE if model.is_remote else EgressClass.LOCAL,
                provider_name=model.provider_name,
            )
        message = (
            f"the execution subject cannot be verified: the response names provider kind "
            f"{kind.value!r}, which LoadCoach's registry does not "
            f"({sorted(k.value for k in surface.provider_kinds)}; "
            f"{len(surface.unknown_kinds)} unknown)"
        )
        raise LoadCoachError(
            message,
            details={
                "reason": "subject_unverifiable",
                "provider_kinds": sorted(k.value for k in surface.provider_kinds),
                "unknown_kinds": sorted(surface.unknown_kinds),
                "model_count": surface.model_count,
            },
        )
    configured = surface.single_kind
    if configured is None:
        message = (
            "the execution subject cannot be verified: LoadCoach's registry names "
            f"{len(surface.provider_kinds)} known provider kind(s) and "
            f"{len(surface.unknown_kinds)} unknown, and the response does not declare "
            "is_remote; contract 4 needs exactly one configured provider until the response "
            "carries the serving registration's declared egress class (LoadCoach 1.1)"
        )
        raise LoadCoachError(
            message,
            details={
                "reason": "subject_unverifiable",
                "provider_kinds": sorted(k.value for k in surface.provider_kinds),
                "unknown_kinds": sorted(surface.unknown_kinds),
                "model_count": surface.model_count,
            },
        )
    egress = EgressClass.LOCAL if kind is configured else EgressClass.REMOTE
    return ExecutionSubject(
        model_canonical_id=model.canonical_id,
        provider_kind=kind,
        egress_class=egress,
        provider_name=None,
    )
