import pytest

from aftertrace.adapters.base import HarnessAdapter, RawSession
from aftertrace.adapters.claude_code import ClaudeCodeAdapter
from aftertrace.adapters.codex import CodexAdapter
from aftertrace.adapters.opencode import OpenCodeAdapter
from aftertrace.core.errors import UnsupportedOperationError
from aftertrace.core.models import Harness, SessionReference, Source


@pytest.mark.parametrize("adapter_type", [CodexAdapter, ClaudeCodeAdapter, OpenCodeAdapter])
def test_adapter_contract_and_explicit_unavailable_operations(adapter_type):
    adapter = adapter_type()
    assert isinstance(adapter, HarnessAdapter)
    source = Source(harness=Harness.CODEX, display_name="Example", location="nonexistent")
    reference = SessionReference(
        source_id=source.id, source_session_id=None, source_locator="missing"
    )
    raw = RawSession(reference, b"sanitized placeholder")
    for method, args in [
        ("detect", ()),
        ("discover_sessions", (source,)),
        ("load_raw_session", (reference,)),
        ("normalize_session", (raw,)),
    ]:
        with pytest.raises(UnsupportedOperationError) as caught:
            getattr(adapter, method)(*args)
        assert caught.value.operation == method
        assert caught.value.source == adapter.source_name
