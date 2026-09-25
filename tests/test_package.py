import ast
import json
import os
import runpy
import zipfile
from pathlib import Path
from xml.etree import ElementTree as ET

import pytest


def test_registration_and_build_archive(tmp_path):
    import importlib.util

    spec = importlib.util.spec_from_file_location("builder", "scripts/build_oxt.py")
    builder = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(builder)
    output = tmp_path / "assinafy.oxt"
    builder.build(builder.Config(), output)
    with zipfile.ZipFile(output) as archive:
        names = set(archive.namelist())
        assert "description.xml" in names and "assinafy_addon.py" in names
        assert "pythonpath/assinafy/client.py" in names
        assert "pythonpath/certifi/cacert.pem" in names
        assert not any("__pycache__" in name or name.endswith(".pth") for name in names)
        assert {
            name.removeprefix("pythonpath/assinafy_libreoffice/")
            for name in names
            if name.startswith("pythonpath/assinafy_libreoffice/")
        } == {"__init__.py", "config.py", "oauth.py", "ui.py", "workflow.py", "deployment.json"}
        assert not any(name.startswith(("tests/", ".env", ".git", ".local/")) for name in names)
        assert not any(name.endswith("direct_url.json") for name in names)
        assert not any("tests" in Path(name).parts for name in names)
        assert b"SelfTest" not in archive.read("assinafy_addon.py")
        assert b"self-test.json" not in archive.read("pythonpath/assinafy_libreoffice/ui.py")
        manifest = ET.fromstring(archive.read("META-INF/manifest.xml"))
        for entry in manifest:
            assert entry.attrib["{http://openoffice.org/2001/manifest}full-path"] in names
        deployment = json.loads(archive.read("pythonpath/assinafy_libreoffice/deployment.json"))
        assert deployment["client_id"] == builder.Config.client_id
        assert deployment["client_id"] and "client_secret" not in deployment
        assert deployment["environment"] == "production"
        assert deployment["redirect_uri"] == (
            "https://integrations.assinafy.com.br/libreoffice/oauth-callback"
        )
        ast.parse(archive.read("assinafy_addon.py"))


def test_invalid_configuration_rejected_before_replacing_output(tmp_path):
    builder = runpy.run_path("scripts/build_oxt.py")
    output = tmp_path / "release.oxt"
    output.write_bytes(b"existing release")
    with pytest.raises(ValueError, match="client_id"):
        builder["build"](builder["Config"](client_id=" "), output)
    assert output.read_bytes() == b"existing release"


def test_dependency_drift_rejected_before_replacing_output(tmp_path, monkeypatch):
    import importlib.metadata

    builder = runpy.run_path("scripts/build_oxt.py")
    output = tmp_path / "release.oxt"
    output.write_bytes(b"existing release")
    monkeypatch.setattr(importlib.metadata, "version", lambda _: "0.0.0")
    with pytest.raises(ValueError, match="requirements-oxt.txt"):
        builder["build"](builder["Config"](), output)
    assert output.read_bytes() == b"existing release"


def test_editable_or_local_sdk_rejected_before_replacing_output(tmp_path, monkeypatch):
    import importlib.metadata

    builder = runpy.run_path("scripts/build_oxt.py")
    output = tmp_path / "release.oxt"
    output.write_bytes(b"existing release")
    read = importlib.metadata.PathDistribution.read_text
    editable = '{"url": "file:///python-sdk", "dir_info": {"editable": true}}'
    monkeypatch.setattr(
        importlib.metadata.PathDistribution,
        "read_text",
        lambda self, name: editable if name == "direct_url.json" else read(self, name),
    )
    with pytest.raises(ValueError, match="venv limpo"):
        builder["build"](builder["Config"](), output)
    assert output.read_bytes() == b"existing release"


def test_source_contains_no_live_test_contacts():
    private_values = [
        value.strip().casefold()
        for value in os.environ.get("ASSINAFY_TEST_RECIPIENTS", "").split(",")
        if value.strip()
    ]
    for folder in (Path("pythonpath"), Path("tests"), Path("scripts")):
        for path in folder.rglob("*.py"):
            assert not any(token in path.read_text().casefold() for token in private_values)
