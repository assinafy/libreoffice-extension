"""Public configuration; credentials belong in LibreOffice's password container."""

from __future__ import annotations

import json
import os
import tempfile
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import urlsplit

SCOPES = "documents:read documents:write offline_access"


@dataclass(frozen=True)
class Config:
    client_id: str = "7s40fRN-k8oh6CbEevCT6yVoPW9vUxcP_hYBNt07_9XVqcxq"
    redirect_uri: str = "https://integrations.assinafy.com.br/libreoffice/oauth-callback"

    @property
    def environment(self) -> str:
        return "production"

    @property
    def issuer(self) -> str:
        return "https://auth.assinafy.com.br"

    @property
    def api_url(self) -> str:
        return "https://api.assinafy.com.br/v1"

    @property
    def resource(self) -> str:
        return self.api_url.removesuffix("/v1")

    def validate(self) -> None:
        if (
            not isinstance(self.client_id, str)
            or not self.client_id
            or len(self.client_id) > 1024
            or any(c.isspace() or ord(c) < 32 for c in self.client_id)
        ):
            raise ValueError("Configure o client_id público da aplicação Assinafy.")
        p = urlsplit(self.redirect_uri)
        if (
            p.scheme != "https"
            or not p.hostname
            or p.port == 0
            or p.username is not None
            or p.password is not None
            or p.fragment
            or p.query
            or "\\" in self.redirect_uri
            or any(c.isspace() or ord(c) < 32 for c in self.redirect_uri)
        ):
            raise ValueError(
                "Configure uma URL de retorno HTTPS, sem credenciais, query ou fragmento."
            )


def write_private(path: Path, value: dict) -> None:
    """Replace a local state file atomically with restrictive permissions."""
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    fd, temporary = tempfile.mkstemp(dir=path.parent, prefix=".assinafy-")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            json.dump(value, handle, ensure_ascii=False)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
    finally:
        Path(temporary).unlink(missing_ok=True)


def load_config(path: Path) -> Config:
    if not path.exists():
        return Config()
    value = json.loads(path.read_text(encoding="utf-8"))
    if value.get("environment") != "production":
        raise ValueError("Instale a distribuição de produção da Assinafy.")
    return Config(**{key: value[key] for key in ("client_id", "redirect_uri")})
