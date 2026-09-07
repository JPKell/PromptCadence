"""Tests for promptcadence.services.loadcoach_surface: verified, never assumed (contract 4)."""

from __future__ import annotations

import pytest
from baseaicore import ProviderKind
from fastapi.testclient import TestClient
from tests.fakes.loadcoach_app import FakeLoadCoach, FakeModel, build_fake_app

from promptcadence.domain.errors import LoadCoachError
from promptcadence.domain.tiers import EgressClass
from promptcadence.infrastructure.loadcoach import LoadCoachClient, ModelInfo
from promptcadence.services.loadcoach_surface import (
    ProviderSurface,
    load_provider_surface,
    resolve_subject,
)


def _model(canonical_id: str) -> ModelInfo:
    return ModelInfo(
        canonical_id=canonical_id,
        model_ref=None,
        runtime_profile_hash=None,
        served_context=None,
        served_context_source=None,
        target_gpu_index=None,
    )


def test_the_surface_is_read_from_models_not_system_status() -> None:
    fake = FakeLoadCoach(
        model=FakeModel(canonical_id="llamacpp/x@sha256:" + "a" * 64, provider_kind="llamacpp")
    )
    client = LoadCoachClient(TestClient(build_fake_app(fake), base_url="http://loadcoach.test"))
    surface = load_provider_surface(client)
    assert surface.single_kind is ProviderKind.LLAMACPP
    assert surface.model_count == 1
    assert surface.has_remote_provider is False


def test_a_matching_kind_is_local_and_a_foreign_kind_is_remote() -> None:
    surface = ProviderSurface(
        provider_kinds=frozenset({ProviderKind.OLLAMA}), unknown_kinds=frozenset(), model_count=2
    )
    local = resolve_subject(_model("ollama/q@sha256:" + "a" * 64), surface=surface)
    assert local.egress_class is EgressClass.LOCAL
    assert local.provider_name is None
    foreign = resolve_subject(_model("openai_compatible/gpt@sha256:" + "b" * 64), surface=surface)
    assert foreign.egress_class is EgressClass.REMOTE


@pytest.mark.parametrize(
    "surface",
    [
        ProviderSurface(frozenset(), frozenset(), 0),
        ProviderSurface(frozenset({ProviderKind.OLLAMA, ProviderKind.VLLM}), frozenset(), 3),
        ProviderSurface(frozenset({ProviderKind.OLLAMA}), frozenset({"mystery"}), 2),
    ],
)
def test_an_unverifiable_surface_refuses_rather_than_assumes(surface: ProviderSurface) -> None:
    with pytest.raises(LoadCoachError) as excinfo:
        resolve_subject(_model("ollama/q@sha256:" + "a" * 64), surface=surface)
    assert excinfo.value.details["reason"] == "subject_unverifiable"


# --- LoadCoach 1.1: the registration's declared egress class on the wire (ADR-0098) ------------


def _declared(canonical_id: str, *, provider_name: str, is_remote: bool) -> ModelInfo:
    return ModelInfo(
        canonical_id=canonical_id,
        model_ref=None,
        runtime_profile_hash=None,
        served_context=None,
        served_context_source=None,
        target_gpu_index=None,
        provider_name=provider_name,
        is_remote=is_remote,
    )


def test_a_mixed_registry_reports_its_remote_registrations_by_name() -> None:
    fake = FakeLoadCoach(
        remote_model=FakeModel(
            canonical_id="openai_compatible/gpt@sha256:" + "b" * 64,
            provider_kind="openai_compatible",
            provider_name="openrouter",
            is_remote=True,
        )
    )
    client = LoadCoachClient(TestClient(build_fake_app(fake), base_url="http://loadcoach.test"))
    surface = load_provider_surface(client)
    assert surface.provider_kinds == {ProviderKind.OLLAMA, ProviderKind.OPENAI_COMPATIBLE}
    assert surface.single_kind is None, "two kinds: identity is no longer the verification"
    assert surface.has_remote_provider is True
    assert surface.remote_provider_names == {"openrouter"}
    assert surface.model_count == 2


def test_a_declared_egress_class_is_the_verification_on_a_mixed_registry() -> None:
    surface = ProviderSurface(
        provider_kinds=frozenset({ProviderKind.OLLAMA, ProviderKind.OPENAI_COMPATIBLE}),
        unknown_kinds=frozenset(),
        model_count=2,
        remote_provider_names=frozenset({"openrouter"}),
    )
    local = resolve_subject(
        _declared("ollama/q@sha256:" + "a" * 64, provider_name="ollama", is_remote=False),
        surface=surface,
    )
    assert local.egress_class is EgressClass.LOCAL and local.provider_name == "ollama"
    remote = resolve_subject(
        _declared(
            "openai_compatible/gpt@sha256:" + "b" * 64, provider_name="openrouter", is_remote=True
        ),
        surface=surface,
    )
    assert remote.egress_class is EgressClass.REMOTE and remote.provider_name == "openrouter"
    # A local llama.cpp server behind an openai_compatible registration is local because it
    # *declared* so — the kind alone could never have said (ADR-0055 rule 4).
    local_compatible = resolve_subject(
        _declared("openai_compatible/x@sha256:" + "c" * 64, provider_name="llama", is_remote=False),
        surface=surface,
    )
    assert local_compatible.egress_class is EgressClass.LOCAL


def test_a_declared_answer_from_a_kind_the_registry_never_named_is_unverifiable() -> None:
    surface = ProviderSurface(
        provider_kinds=frozenset({ProviderKind.OLLAMA}), unknown_kinds=frozenset(), model_count=1
    )
    with pytest.raises(LoadCoachError) as excinfo:
        resolve_subject(
            _declared("vllm/x@sha256:" + "d" * 64, provider_name="ghost", is_remote=False),
            surface=surface,
        )
    assert excinfo.value.details["reason"] == "subject_unverifiable"


def test_the_remote_fact_reads_false_when_loadcoach_cannot_be_read() -> None:
    from promptcadence.services.loadcoach_surface import remote_provider_registered

    unreachable = LoadCoachClient.from_settings(base_url="http://127.0.0.1:9", timeout_seconds=1)
    try:
        assert remote_provider_registered(unreachable) is False
    finally:
        unreachable.close()
    fake = FakeLoadCoach()
    client = LoadCoachClient(TestClient(build_fake_app(fake), base_url="http://loadcoach.test"))
    assert remote_provider_registered(client) is False, "one local registration: no remote"
