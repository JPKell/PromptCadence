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
