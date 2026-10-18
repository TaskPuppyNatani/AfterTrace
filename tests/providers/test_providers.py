import pytest

from aftertrace.core.errors import UnsupportedOperationError
from aftertrace.core.models import ProviderConfig, ProviderLocality
from aftertrace.providers.base import CompletionRequest, ModelProvider
from aftertrace.providers.openai_compatible import OpenAICompatibleProvider


@pytest.mark.parametrize("locality", list(ProviderLocality))
def test_provider_contract_does_not_enable_inference(locality):
    config = ProviderConfig(
        display_name="Example backend",
        endpoint="https://example.invalid/v1",
        locality=locality,
    )
    provider = OpenAICompatibleProvider(config)
    assert isinstance(provider, ModelProvider)
    assert provider.provider_id == config.id
    assert provider.classify_locality() is locality
    assert provider.capabilities() == set()
    assert provider.test_connection().available is False
    with pytest.raises(UnsupportedOperationError):
        provider.embed(["Sanitized example"])
    with pytest.raises(UnsupportedOperationError):
        provider.complete(CompletionRequest(prompt="Sanitized example", purpose="test"))
