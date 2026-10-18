"""Backend-agnostic optional model provider port."""

from dataclasses import dataclass
from typing import Protocol, runtime_checkable

from aftertrace.core.models import ProviderLocality


@dataclass(frozen=True, kw_only=True)
class ProviderStatus:
    available: bool
    message: str


@dataclass(frozen=True, kw_only=True)
class CompletionRequest:
    prompt: str
    purpose: str
    model: str | None = None


@dataclass(frozen=True, kw_only=True)
class CompletionResult:
    text: str
    provider_id: str
    provider_locality: ProviderLocality
    model: str | None


@runtime_checkable
class ModelProvider(Protocol):
    provider_id: str

    def capabilities(self) -> set[str]: ...
    def classify_locality(self) -> ProviderLocality: ...
    def test_connection(self) -> ProviderStatus: ...
    def embed(self, texts: list[str]) -> list[list[float]]: ...
    def complete(self, request: CompletionRequest) -> CompletionResult: ...
