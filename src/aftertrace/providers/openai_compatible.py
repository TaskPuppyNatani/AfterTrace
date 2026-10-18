"""Configuration-only foundation; no HTTP client or inference is enabled."""

from aftertrace.core.errors import UnsupportedOperationError
from aftertrace.core.models import ProviderConfig, ProviderLocality
from aftertrace.providers.base import CompletionRequest, CompletionResult, ProviderStatus


class OpenAICompatibleProvider:
    def __init__(self, config: ProviderConfig) -> None:
        self.config = config
        self.provider_id = config.id

    def capabilities(self) -> set[str]:
        return set()

    def classify_locality(self) -> ProviderLocality:
        # Explicit configuration, not a permission grant or endpoint safety check.
        return self.config.locality

    def test_connection(self) -> ProviderStatus:
        return ProviderStatus(
            available=False, message="Provider connections are not implemented yet."
        )

    def embed(self, texts: list[str]) -> list[list[float]]:
        raise self._unsupported("embed")

    def complete(self, request: CompletionRequest) -> CompletionResult:
        raise self._unsupported("complete")

    def _unsupported(self, operation: str) -> UnsupportedOperationError:
        return UnsupportedOperationError(
            "Model inference is not implemented yet.",
            operation=operation,
            source=self.provider_id,
        )
