"""Build a self-contained OXT using an explicit file allowlist."""

from __future__ import annotations

import argparse
import importlib.metadata
import json
import shutil
import sys
import zipfile
from pathlib import Path
from xml.etree import ElementTree as ET

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "pythonpath"))
from assinafy_libreoffice import __version__  # noqa: E402
from assinafy_libreoffice.config import Config  # noqa: E402

DEPENDENCIES = dict(
    line.split("==") for line in (ROOT / "requirements-oxt.txt").read_text().splitlines() if line
)


def build(config, output):
    config.validate()
    versions = {name: importlib.metadata.version(name) for name in DEPENDENCIES}
    if versions != DEPENDENCIES:
        raise ValueError("Instale as versões fixadas em requirements-oxt.txt antes de empacotar.")
    # PEP 610: editable, local-path, VCS and URL installs record direct_url.json; PyPI ones never.
    if any(importlib.metadata.distribution(n).read_text("direct_url.json") for n in DEPENDENCIES):
        raise ValueError("Empacote a partir de um venv limpo, com as dependências do PyPI.")
    ns = {"d": "http://openoffice.org/extensions/description/2006"}
    assert ET.parse(ROOT / "description.xml").find("d:version", ns).get("value") == __version__
    staging = ROOT / "build" / "oxt"
    if staging.exists():
        shutil.rmtree(staging)
    staging.mkdir(parents=True)
    for name in (
        "description.xml",
        "description.txt",
        "META-INF/manifest.xml",
        "Addons.xcu",
        "ProtocolHandler.xcu",
        "assinafy_addon.py",
        "icons/assinafy.png",
        "LICENSE",
    ):
        target = staging / name
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(ROOT / name, target)
    package = Path("pythonpath/assinafy_libreoffice")
    (staging / package).mkdir(parents=True)
    for name in ("__init__.py", "config.py", "oauth.py", "ui.py", "workflow.py"):
        shutil.copyfile(ROOT / package / name, staging / package / name)
    (staging / "pythonpath/assinafy_libreoffice/deployment.json").write_text(
        json.dumps(
            {
                "environment": config.environment,
                "client_id": config.client_id,
                "redirect_uri": config.redirect_uri,
            }
        ),
        encoding="utf-8",
    )
    for name in sorted(DEPENDENCIES):
        dist = importlib.metadata.distribution(name)
        files = dist.files or []
        for file in files:
            parts = file.parts
            if (
                not parts
                or ".." in parts
                or str(file).endswith((".pyc", ".pth"))
                or "__pycache__" in parts
                or "tests" in parts
                or file.name in {"direct_url.json", "RECORD", "INSTALLER"}
            ):
                continue
            source = Path(dist.locate_file(file))
            if source.is_file() and (
                parts[0].replace("-", "_") == name.replace("-", "_")
                or parts[0] == name + ".py"
                or ".dist-info" in parts[0]
            ):
                target = staging / "pythonpath" / file
                target.parent.mkdir(parents=True, exist_ok=True)
                shutil.copyfile(source, target)
    (staging / "DEPENDENCIES.json").write_text(json.dumps(versions, indent=2) + "\n")
    output.parent.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(output, "w", zipfile.ZIP_DEFLATED) as archive:
        for source in sorted(staging.rglob("*")):
            if source.is_file():
                archive.write(source, source.relative_to(staging))
    print(output)
    print("Environment:", config.environment)
    print("OAuth callback:", config.redirect_uri)
    print("OAuth configured:", bool(config.client_id))


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--client-id", default=Config.client_id)
    parser.add_argument("--redirect-uri", default=Config.redirect_uri)
    parser.add_argument(
        "--output", type=Path, default=ROOT / "dist" / f"assinafy-{__version__}.oxt"
    )
    args = parser.parse_args()
    build(Config(client_id=args.client_id, redirect_uri=args.redirect_uri), args.output)
