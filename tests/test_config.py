from agent_ops.config import get_settings


def test_defaults_are_offline():
    s = get_settings()
    assert s.llm_provider == "mock"
    assert s.max_iterations >= 1
    assert s.refund_approval_threshold > 0


def test_model_role_routing():
    s = get_settings()
    assert "haiku" in s.model_for_role("classifier")
    assert s.model_for_role("reasoner") == s.model_reasoner
    # Unknown roles fall back to the reasoner model.
    assert s.model_for_role("nonexistent") == s.model_reasoner


def test_paths_resolve_absolute():
    s = get_settings()
    assert s.db_file.is_absolute()
    assert s.sqlite_url.startswith("sqlite:///")
