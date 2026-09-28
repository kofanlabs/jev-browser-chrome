import os

import pytest

import jev_credentials
from jev_ultrafast import model
from scripts import setup_macos


@pytest.mark.skipif(os.name == "nt", reason="Requires POSIX os.fchmod and 0600 file-permission semantics")
def test_mac_key_permissions_and_provider(monkeypatch, tmp_path):
    monkeypatch.setattr(setup_macos, "ROOT", tmp_path)
    monkeypatch.setattr(jev_credentials, "ROOT", tmp_path)
    monkeypatch.setattr(jev_credentials.sys, "platform", "darwin")
    for key in ["TYPESAFE_API_KEY", "AI_GATEWAY_API_KEY", "TYPESAFE_BASE_URL", "TYPESAFE_DEFAULT_MODEL"]:
        monkeypatch.delenv(key, raising=False)
    setup_macos.save_key("vercel", "test-secret-that-is-not-a-real-key")
    path = tmp_path / "config/macos-key.json"
    assert path.stat().st_mode & 0o777 == 0o600
    jev_credentials.prepare_provider()
    assert jev_credentials.os.environ["TYPESAFE_BASE_URL"] == "https://ai-gateway.vercel.sh/typesafe"
    # Register environment changes for pytest cleanup as well.
    for key in ["TYPESAFE_API_KEY", "AI_GATEWAY_API_KEY", "TYPESAFE_BASE_URL", "TYPESAFE_DEFAULT_MODEL"]:
        monkeypatch.delenv(key, raising=False)
    path.chmod(0o644)
    with pytest.raises(RuntimeError, match="0600"):
        jev_credentials.prepare_provider()


@pytest.mark.parametrize("native_confidence", [None, 0.72])
def test_gateway_uses_native_confidence(monkeypatch, native_confidence):
    monkeypatch.setenv("TYPESAFE_API_KEY", "test-only")
    monkeypatch.setenv("JEV_SYSTEMONE_ENDPOINT", "https://ai-gateway.vercel.sh/typesafe/v1/systemone")

    def post(url, key, body, extra_headers=None):
        assert url == "https://ai-gateway.vercel.sh/v4/ai/evaluation-model"
        assert extra_headers["ai-model-id"] == "typesafe-ai/jev"
        assert "model" not in body
        return {
            "answers": {
                "operation": {"type": "choice", "choice": "DONE", "probabilities": {"DONE": 1.0, "BLOCKED": 0.0}}
            },
            "providerMetadata": {"typesafe": {"confidence": {"operation": native_confidence}}},
        }

    monkeypatch.setattr(model, "post_json", post)
    page = {"url": "https://example.test", "title": "Done", "text": "Done", "actions": []}
    if native_confidence is None:
        with pytest.raises(ValueError, match="Invalid TypeSafe"):
            model.choose(page, "done", [])
    else:
        assert model.choose(page, "done", [])["confidence"] == native_confidence
