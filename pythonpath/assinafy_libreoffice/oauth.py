"""Assinafy public-client PKCE and single-use token rotation."""

from __future__ import annotations

import base64
import hashlib
import json
import secrets
import ssl
import threading
import time
from http.server import BaseHTTPRequestHandler, HTTPServer
from urllib.parse import parse_qs, urlencode, urlsplit

import httpx

from .config import SCOPES, Config

CLEAR_HISTORY = "history.replaceState(null, '', '/');"
CALLBACK_PAGE = (
    '<!doctype html><html lang="pt-BR"><meta charset="utf-8">'
    '<meta name="viewport" content="width=device-width,initial-scale=1">'
    "<title>Assinafy</title><script>" + CLEAR_HISTORY + "</script>"
    "<h1>Volte ao LibreOffice.</h1></html>"
).encode()
CALLBACK_CSP = (
    "default-src 'none'; script-src 'sha256-"
    + base64.b64encode(hashlib.sha256(CLEAR_HISTORY.encode()).digest()).decode()
    + "'; base-uri 'none'; form-action 'none'; frame-ancestors 'none'"
)
# Authorization errors a retry cannot fix; access_denied means the user declined.
REQUEST_ERRORS = {"invalid_scope", "invalid_request", "unsupported_response_type", "invalid_target"}
# Failures raised before a request reaches the server: DNS, connect, TLS handshake, or a proxy
# refusing the tunnel (httpcore raises ProxyError only before the request is sent).
UNSENT = (httpx.ConnectError, httpx.ConnectTimeout, httpx.ProxyError)


class ConnectionError(RuntimeError):
    """A connection must be established again by its owner."""


def challenge(verifier: str) -> str:
    return (
        base64.urlsafe_b64encode(hashlib.sha256(verifier.encode("ascii")).digest())
        .decode()
        .rstrip("=")
    )


def usable(tokens):
    return bool(tokens.get("access_token")) and tokens.get("expires_at", 0) > time.time() + 60


class OAuth:
    # Shared by every instance, so all holders of a connection in this process refresh in turn.
    # One office process owns a profile's password container (LibreOffice hands a second launch
    # on the same profile to the running one), so no cross-process lock is needed.
    lock = threading.RLock()

    def __init__(self, config: Config, save, tokens=None, transport=None, load=None):
        config.validate()
        self.config = config
        self.save = save
        self.tokens = tokens or {}
        # Reads the stored tokens, which are current when several holders share the store.
        self.load = load or (lambda: self.tokens)
        # httpx's default verified context, refusing TLS 1.0 and 1.1.
        tls = httpx.create_ssl_context()
        tls.minimum_version = ssl.TLSVersion.TLSv1_2
        self.http = httpx.Client(
            timeout=25, follow_redirects=False, transport=transport, verify=tls
        )
        self.metadata = None

    def discover(self):
        response = self.http.get(self.config.issuer + "/.well-known/oauth-authorization-server")
        response.raise_for_status()
        data = response.json()
        if not isinstance(data, dict):
            raise ConnectionError("Metadados OAuth inválidos.")
        expected = {
            "issuer": self.config.issuer,
            "authorization_endpoint": self.config.issuer + "/oauth/authorize",
            "token_endpoint": self.config.api_url + "/oauth/token",
            "revocation_endpoint": self.config.api_url + "/oauth/revoke",
        }
        if (
            any(data.get(k) != v for k, v in expected.items())
            or "S256" not in data.get("code_challenge_methods_supported", [])
            or "none" not in data.get("token_endpoint_auth_methods_supported", [])
        ):
            raise ConnectionError("Metadados OAuth incompatíveis com o ambiente configurado.")
        self.metadata = data
        return data

    def _replace(self, tokens):
        self.save(tokens)
        self.tokens = tokens

    def _exchange(self, body):
        metadata = self.metadata or self.discover()
        response = self.http.post(
            metadata["token_endpoint"],
            data={
                "client_id": self.config.client_id,
                "resource": self.config.resource,
                **body,
            },
        )
        if response.status_code != 200:
            raise ConnectionError(
                "A autorização expirou ou foi recusada. Conecte a conta novamente."
            )
        data = response.json()
        if not isinstance(data, dict):
            raise ConnectionError("Resposta OAuth inválida.")
        ttl = data.get("expires_in")
        if (
            not isinstance(data.get("access_token"), str)
            or not data["access_token"]
            or str(data.get("token_type", "")).lower() != "bearer"
            or isinstance(ttl, bool)
            or not isinstance(ttl, (int, float))
            or not 0 < ttl <= 86400
            or not isinstance(data.get("scope"), str)
        ):
            raise ConnectionError("Resposta OAuth inválida. Conecte a conta novamente.")
        refresh = data.get("refresh_token")
        if refresh is not None and (not isinstance(refresh, str) or not refresh):
            raise ConnectionError("Resposta de renovação inválida.")
        # Every refresh retires the token sent, so it must return a new one to save before use.
        if "refresh_token" in body and refresh in (None, body["refresh_token"]):
            raise ConnectionError("Resposta de renovação inválida.")
        if "documents:read" not in data["scope"].split():
            raise ConnectionError("A conta não autorizou a leitura dos documentos.")
        data.pop("id_token", None)
        data["expires_at"] = time.time() + ttl
        self._replace(data)
        return data

    def connect(self, open_browser, timeout=180):
        """Receive a validated callback forwarded by the registered HTTPS return page."""
        with self.lock:
            metadata = self.metadata or self.discover()
            result = {}

            class Callback(BaseHTTPRequestHandler):
                def setup(handler):
                    handler.request.settimeout(5)
                    super().setup()

                def do_GET(handler):
                    parsed = urlsplit(
                        handler.path if handler.path.startswith("/callback?") else "/invalid"
                    )
                    query = parse_qs(parsed.query, keep_blank_values=True)
                    valid = (
                        parsed.path == "/callback"
                        and not parsed.fragment
                        and len(handler.headers.get_all("Host", [])) == 1
                        and handler.headers.get("Host") == f"127.0.0.1:{server.server_port}"
                        and len(handler.path) < 8192
                        and all(len(v) == 1 for v in query.values())
                        and secrets.compare_digest(
                            query.get("state", [""])[0].encode(), state.encode()
                        )
                        and query.get("iss", [""])[0] == self.config.issuer
                        and ("code" in query) != ("error" in query)
                        and bool((query.get("code") or query.get("error") or [""])[0].strip())
                    )
                    handler.send_response(200 if valid else 400)
                    handler.send_header(
                        "Content-Type",
                        "text/html; charset=utf-8" if valid else "text/plain; charset=utf-8",
                    )
                    handler.send_header("Cache-Control", "no-store")
                    handler.send_header("Referrer-Policy", "no-referrer")
                    handler.send_header("X-Content-Type-Options", "nosniff")
                    handler.send_header("Content-Security-Policy", CALLBACK_CSP)
                    handler.end_headers()
                    handler.wfile.write(CALLBACK_PAGE if valid else b"Retorno invalido.")
                    if valid:
                        result.update({k: v[0] for k, v in query.items()})

                def log_message(self, *args):
                    pass

            with HTTPServer(("127.0.0.1", 0), Callback) as server:
                server.timeout = 0.2
                state = secrets.token_urlsafe(32) + "." + str(server.server_port)
                verifier = secrets.token_urlsafe(48)
                url = (
                    metadata["authorization_endpoint"]
                    + "?"
                    + urlencode(
                        {
                            "response_type": "code",
                            "client_id": self.config.client_id,
                            "redirect_uri": self.config.redirect_uri,
                            "scope": SCOPES,
                            "state": state,
                            "code_challenge": challenge(verifier),
                            "code_challenge_method": "S256",
                            "resource": self.config.resource,
                        }
                    )
                )
                open_browser(url)
                deadline = time.monotonic() + timeout
                while not result and time.monotonic() < deadline:
                    server.handle_request()
            if result.get("error") in REQUEST_ERRORS:
                raise ConnectionError(
                    "A Assinafy recusou o pedido de conexão (" + result["error"] + "). "
                    "Atualize a extensão ou contate o suporte."
                )
            if not result.get("code"):
                raise ConnectionError("Conexão cancelada ou expirada. Tente novamente.")
            self._exchange(
                {
                    "grant_type": "authorization_code",
                    "code": result["code"],
                    "redirect_uri": self.config.redirect_uri,
                    "code_verifier": verifier,
                }
            )

    def access_token(self, force=False):
        with self.lock:
            if not force and usable(self.tokens):
                return self.tokens["access_token"]
            stored = self.load()
            if stored.get("refresh_token") != self.tokens.get("refresh_token"):
                # Another holder rotated the connection, retiring our refresh token: use its tokens.
                self.tokens = stored
                if usable(stored):
                    return stored["access_token"]
            current = self.tokens
            old = current.get("refresh_token")
            if not old:
                raise ConnectionError("Conecte sua conta Assinafy nas configurações.")
            # Discovery sends no token, so its failures must not cost the connection.
            if self.metadata is None:
                self.discover()
            # Invalidate on disk before attempting the single-use exchange, including on crash.
            self._replace({})
            try:
                result = self._exchange({"grant_type": "refresh_token", "refresh_token": old})
            except UNSENT:
                # The token never reached the server, so it is still the current one.
                self._replace(current)
                raise
            except Exception as exc:
                self.tokens = {}
                raise ConnectionError(
                    "Não foi possível renovar com segurança. Reconecte a conta."
                ) from exc
            return result["access_token"]

    def require(self, scope):
        self.access_token()
        if scope not in self.tokens.get("scope", "").split():
            raise ConnectionError("Autorize a permissão " + scope + " conectando novamente.")

    def disconnect(self):
        with self.lock:
            # Revoke the stored tokens: another holder may have rotated ours.
            self.tokens = self.load()
            token = self.tokens.get("refresh_token") or self.tokens.get("access_token")
            if token:
                metadata = self.metadata or self.discover()
                response = self.http.post(
                    metadata["revocation_endpoint"],
                    data={
                        "client_id": self.config.client_id,
                        "token": token,
                    },
                )
                # 401 means the application itself was disabled, which already ended its tokens.
                if response.status_code not in (200, 401):
                    raise ConnectionError("Não foi possível revogar a conexão. Tente novamente.")
            self._replace({})

    def close(self):
        self.http.close()


def decode_tokens(value):
    if not value:
        return {}
    data = json.loads(value)
    if not isinstance(data, dict):
        raise ConnectionError("Credenciais locais inválidas. Conecte novamente.")
    return data
