import pytest

from gateway.models import CanonicalModel, ModelRegistry, ProviderModel
from gateway.protocols import Capability, ClientProtocol, normalize_request


def make_registry() -> ModelRegistry:
    return ModelRegistry(
        models=[
            CanonicalModel(
                id="claude-opus",
                aliases=frozenset({"opus", "claude-opus-latest"}),
                capabilities=frozenset({Capability.STREAMING, Capability.TOOL_CALLING}),
            )
        ],
        provider_models=[
            ProviderModel(
                id="provider-a-opus",
                canonical_model_id="claude-opus",
                provider_id="provider-a",
                upstream_model_id="provider-specific-opus",
                protocol=ClientProtocol.ANTHROPIC_MESSAGES,
                capabilities=frozenset({Capability.STREAMING, Capability.TOOL_CALLING}),
                priority=10,
            )
        ],
    )


def test_alias_resolution_and_provider_mapping() -> None:
    registry = make_registry()
    request = normalize_request(
        ClientProtocol.ANTHROPIC_MESSAGES,
        {"model": "OPUS", "stream": True, "messages": []},
    )

    assert registry.resolve("claude-opus-latest").id == "claude-opus"
    assert registry.eligible_provider_models(request)[0].upstream_model_id == (
        "provider-specific-opus"
    )
    assert [model.id for model in registry.list_enabled()] == ["claude-opus"]


def test_duplicate_aliases_are_rejected() -> None:
    with pytest.raises(ValueError, match="assigned more than once"):
        ModelRegistry(
            models=[
                CanonicalModel("one", frozenset({"shared"}), frozenset()),
                CanonicalModel("two", frozenset({"shared"}), frozenset()),
            ],
            provider_models=[],
        )


def test_capability_mismatch_returns_no_provider_models() -> None:
    registry = make_registry()
    request = normalize_request(
        ClientProtocol.ANTHROPIC_MESSAGES,
        {"model": "opus", "thinking": {"type": "enabled"}, "messages": []},
    )

    with pytest.raises(LookupError, match="reasoning"):
        registry.eligible_provider_models(request)
