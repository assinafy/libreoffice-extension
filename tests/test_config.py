import json
import os
import stat

import pytest
from assinafy_libreoffice.config import Config, load_config, write_private
from assinafy_libreoffice.oauth import ConnectionError, decode_tokens


def test_private_state_is_replaced_atomically_with_private_permissions(tmp_path):
    path = tmp_path / "private" / "history.json"
    write_private(path, {"doc1": {"status": "uploaded"}})
    write_private(path, {"doc1": {"status": "sent"}})
    assert json.loads(path.read_text()) == {"doc1": {"status": "sent"}}
    assert stat.S_IMODE(path.stat().st_mode) == 0o600
    assert stat.S_IMODE(path.parent.stat().st_mode) == 0o700
    assert list(path.parent.iterdir()) == [path]


def test_failed_replacement_preserves_previous_state(tmp_path, monkeypatch):
    path = tmp_path / "history.json"
    write_private(path, {"doc1": "uploaded"})

    def fail(*args):
        raise OSError("disk unavailable")

    monkeypatch.setattr(os, "replace", fail)
    with pytest.raises(OSError):
        write_private(path, {"doc1": "sent"})
    assert json.loads(path.read_text()) == {"doc1": "uploaded"}
    assert list(tmp_path.iterdir()) == [path]


def test_public_deployment_and_default_configuration(tmp_path):
    path = tmp_path / "deployment.json"
    assert load_config(path) == Config()
    path.write_text(json.dumps({
        "environment": "production", "client_id": "test-public-client",
        "redirect_uri": "https://callback.example.invalid/return",
    }))
    config = load_config(path)
    config.validate()
    assert config.client_id == "test-public-client"


def test_stored_tokens_are_objects():
    assert decode_tokens("") == {}
    assert decode_tokens('{"access_token":"test-token"}') == {"access_token": "test-token"}
    with pytest.raises(ConnectionError):
        decode_tokens("[]")
