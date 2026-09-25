import json
import ssl
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer
from urllib.parse import parse_qs, urlencode, urlsplit

import httpx
import pytest
from assinafy_libreoffice.config import SCOPES, Config, load_config
from assinafy_libreoffice.oauth import ConnectionError, OAuth, challenge
from assinafy_libreoffice.workflow import error_message

CONFIG = Config(
    client_id="test-public-client",
    redirect_uri="https://callback.example.invalid/libreoffice/oauth-callback",
)


def metadata():
    return {
        "issuer": CONFIG.issuer,
        "authorization_endpoint": CONFIG.issuer + "/oauth/authorize",
        "token_endpoint": CONFIG.api_url + "/oauth/token",
        "revocation_endpoint": CONFIG.api_url + "/oauth/revoke",
        "code_challenge_methods_supported": ["S256"],
        "token_endpoint_auth_methods_supported": ["none"],
    }


def token_response():
    return {
        "access_token": "test-access",
        "refresh_token": "test-rotated",
        "token_type": "Bearer",
        "expires_in": 3600,
        "scope": "documents:read documents:write",
    }


def test_pkce_rfc7636_vector():
    assert challenge("dBjftJeZ4CVP-mB92K27uhbUJU1p1r_wW1gFWFOEjXk") == (
        "E9Melhoa2OwvFrEMTJguCHaoeK1t8URWbuGJSstw-cM"
    )


@pytest.mark.parametrize(
    "uri",
    [
        "http://localhost/callback",
        "app://callback",
        "https://callback.invalid/#fragment",
        "https://user:pass@callback.invalid/",
        "https://callback.invalid/?x=1",
        "https://callback.invalid:invalid/callback",
        "https://callback.invalid:0/callback",
        "https://callback.invalid:65536/callback",
        "https://@callback.invalid/",
        "https://callback.invalid/\\other",
        "https://callback.invalid/\x00",
    ],
)
def test_redirect_rejected(uri):
    with pytest.raises(ValueError):
        Config(client_id="client", redirect_uri=uri).validate()


def test_oauth_client_requires_tls_1_2_or_newer():
    oauth = OAuth(CONFIG, lambda _: None)
    context = oauth.http._transport._pool._ssl_context
    assert context.minimum_version == ssl.TLSVersion.TLSv1_2
    assert context.verify_mode == ssl.CERT_REQUIRED
    oauth.http.close()


def test_production_defaults_require_client_id():
    config = Config()
    assert config.environment == "production"
    assert config.issuer == "https://auth.assinafy.com.br"
    assert config.resource == "https://api.assinafy.com.br"
    assert config.redirect_uri == "https://integrations.assinafy.com.br/libreoffice/oauth-callback"
    assert config.client_id
    config.validate()
    unconfigured = Config(client_id="")
    with pytest.raises(ValueError, match="client_id"):
        unconfigured.validate()


def test_deployment_rejects_other_environments(tmp_path):
    path = tmp_path / "deployment.json"
    path.write_text('{"environment":"unsupported"}')
    with pytest.raises(ValueError, match="produção"):
        load_config(path)


def test_issuer_or_token_host_mismatch_rejected():
    for key, value in (
        ("issuer", "https://other.invalid"),
        ("token_endpoint", "https://other.invalid/token"),
    ):
        data = {**metadata(), key: value}
        with OAuth(
            CONFIG,
            lambda _: None,
            transport=httpx.MockTransport(lambda _, data=data: httpx.Response(200, json=data)),
        ).http as http:
            oauth = OAuth(CONFIG, lambda _: None)
            oauth.http.close()
            oauth.http = http
            with pytest.raises(ConnectionError):
                oauth.discover()


def test_callback_rejects_wrong_state_issuer_and_duplicates_before_exchange():
    exchanges, persisted, errors = [], [], []
    authorization = {}
    browsers = []

    def upstream(request):
        if request.method == "GET":
            return httpx.Response(200, json=metadata())
        assert request.headers["content-type"] == "application/x-www-form-urlencoded"
        body = parse_qs(request.content.decode())
        exchanges.append(body)
        assert body["redirect_uri"] == [CONFIG.redirect_uri]
        assert body["resource"] == [CONFIG.resource]
        assert "client_secret" not in body
        assert challenge(body["code_verifier"][0]) == authorization["code_challenge"][0]
        return httpx.Response(200, json=token_response())

    def open_browser(url):
        assert urlsplit(url).scheme == "https"
        assert url.startswith("https://auth.assinafy.com.br/oauth/authorize?")
        authorization.update(parse_qs(urlsplit(url).query))
        assert authorization["client_id"] == [CONFIG.client_id]
        assert authorization["redirect_uri"] == [CONFIG.redirect_uri]
        assert authorization["resource"] == [CONFIG.resource]
        assert authorization["scope"] == [SCOPES]
        assert set(SCOPES.split()) == {"documents:read", "documents:write", "offline_access"}
        assert authorization["code_challenge_method"] == ["S256"]
        state = authorization["state"][0]
        port = state.split(".")[1]

        def browser():
            try:
                with httpx.Client(trust_env=False, timeout=5) as client:
                    callback = f"http://127.0.0.1:{port}/callback"
                    valid = {"state": state, "iss": CONFIG.issuer, "code": "single-use-code"}
                    for invalid in (
                        {**valid, "state": "wrong"},
                        {**valid, "state": "wrong-\u00e9"},
                        {**valid, "iss": "wrong"},
                        {"state": state, "code": "single-use-code"},
                        {"state": state, "error": "access_denied"},
                        {**valid, "code": ""},
                        {**valid, "code": " \t"},
                        {**valid, "error": "access_denied"},
                        {"state": state, "iss": CONFIG.issuer},
                        {"state": state, "iss": CONFIG.issuer, "error": ""},
                        {"state": state, "iss": CONFIG.issuer, "error": " "},
                    ):
                        assert client.get(callback, params=invalid).status_code == 400
                    assert (
                        client.get(callback + "?" + urlencode(valid) + "&state=second").status_code
                        == 400
                    )
                    assert client.get(callback + "/other", params=valid).status_code == 400
                    assert (
                        client.get(
                            callback, params=valid, headers={"Host": "other.invalid"}
                        ).status_code
                        == 400
                    )
                    for duplicate in ("&code=second", "&iss=second", "&extra=one&extra=two"):
                        assert (
                            client.get(callback + "?" + urlencode(valid) + duplicate).status_code
                            == 400
                        )
                    assert not exchanges
                    response = client.get(callback, params=valid)
                    assert response.status_code == 200
                    assert response.headers["cache-control"] == "no-store"
                    assert response.headers["x-content-type-options"] == "nosniff"
                    assert "script-src 'sha256-" in response.headers["content-security-policy"]
                    assert "history.replaceState" in response.text
                    assert "single-use-code" not in response.text
                    assert state not in response.text
            except Exception as exc:
                errors.append(exc)

        thread = threading.Thread(target=browser)
        browsers.append(thread)
        thread.start()

    oauth = OAuth(CONFIG, persisted.append, transport=httpx.MockTransport(upstream))
    try:
        oauth.connect(open_browser, timeout=5)
        for thread in browsers:
            thread.join(timeout=5)
            assert not thread.is_alive()
        assert not errors
        assert len(exchanges) == 1
        assert persisted[-1]["refresh_token"] == "test-rotated"
        assert oauth.access_token() == "test-access"
    finally:
        oauth.close()


def test_refresh_serialized_persisted_before_use():
    saved, calls, results = [], [], []

    def upstream(request):
        if request.method == "GET":
            return httpx.Response(200, json=metadata())
        assert saved[-1] == {}
        calls.append(parse_qs(request.content.decode()))
        return httpx.Response(200, json=token_response())

    oauth = OAuth(CONFIG, saved.append, {"refresh_token": "old"}, httpx.MockTransport(upstream))
    threads = [
        threading.Thread(target=lambda: results.append(oauth.access_token())) for _ in range(8)
    ]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()
    assert len(calls) == 1
    assert results == ["test-access"] * 8
    assert saved[0] == {} and saved[1]["refresh_token"] == "test-rotated"
    oauth.close()


def test_holders_of_one_connection_never_replay_a_rotated_token():
    store, server, sent, raced = {"tokens": '{"refresh_token": "r0"}'}, {"live": "r0"}, [], []

    def racing(request):
        if racer.ident is None:
            # second needs a token while first's refresh is in flight: it must wait its turn.
            racer.start()
            racer.join(0.2)
        return upstream(request)

    def upstream(request):
        body = parse_qs(request.content.decode())
        if request.url.path.endswith("/revoke"):
            sent.append(body["token"][0])
            if body["token"][0] == server["live"]:
                server["live"] = None
            return httpx.Response(200, json={})
        sent.append(body["refresh_token"][0])
        if body["refresh_token"][0] != server["live"]:
            server["live"] = None  # reusing a retired refresh token ends the whole connection
            return httpx.Response(400, json={"error": "invalid_grant"})
        server["live"] = f"r{len(sent)}"
        return httpx.Response(
            200,
            json={
                **token_response(),
                "access_token": f"a{len(sent)}",
                "refresh_token": f"r{len(sent)}",
            },
        )

    def save(tokens):
        store["tokens"] = json.dumps(tokens)

    def load():
        return json.loads(store["tokens"])

    first, second = (
        OAuth(CONFIG, save, load(), httpx.MockTransport(handler), load=load)
        for handler in (racing, upstream)
    )
    first.metadata = second.metadata = metadata()
    racer = threading.Thread(target=lambda: raced.append(second.access_token()))
    try:
        assert first.access_token() == "a1"
        racer.join()
        # second held r0, which first retired: it used the stored tokens and cleared nothing.
        assert raced == ["a1"] and sent == ["r0"] and load()["refresh_token"] == "r1"
        assert second.access_token(force=True) == "a2"
        # first still holds r1: disconnecting revokes the current token instead.
        first.disconnect()
        assert sent == ["r0", "r1", "r2"] and server["live"] is None and load() == {}
    finally:
        first.close()
        second.close()


def test_refresh_timeout_never_replays_old_token():
    calls, saved = [], []

    def upstream(request):
        calls.append(request)
        raise httpx.ReadTimeout("private upstream detail")

    oauth = OAuth(CONFIG, saved.append, {"refresh_token": "old"}, httpx.MockTransport(upstream))
    oauth.metadata = metadata()
    for _ in range(2):
        with pytest.raises(ConnectionError):
            oauth.access_token()
    assert len(calls) == 1 and saved == [{}]
    assert oauth.tokens == {}
    oauth.close()


def test_refresh_that_never_left_keeps_the_current_token():
    sent, saved = [], []

    def upstream(request):
        sent.append(parse_qs(request.content.decode())["refresh_token"])
        raise httpx.ConnectError("[SSL: UNSUPPORTED_PROTOCOL] unsupported protocol")

    tokens = {"access_token": "expired", "expires_at": 0, "refresh_token": "current"}
    oauth = OAuth(CONFIG, saved.append, tokens, httpx.MockTransport(upstream))
    oauth.metadata = metadata()
    for _ in range(2):
        with pytest.raises(httpx.ConnectError):
            oauth.access_token()
    assert sent == [["current"], ["current"]]
    assert saved == [{}, tokens, {}, tokens]
    assert oauth.tokens == tokens
    oauth.close()


def test_proxy_refusing_the_tunnel_keeps_the_current_token():
    received, saved = [], []

    class Proxy(BaseHTTPRequestHandler):
        def do_CONNECT(self):
            received.append(self.requestline + "\n" + str(self.headers))
            self.send_response(407)
            self.send_header("Proxy-Authenticate", 'Basic realm="proxy"')
            self.send_header("Content-Length", "0")
            self.end_headers()

        def log_message(self, *args):
            pass

    tokens = {"access_token": "expired", "expires_at": 0, "refresh_token": "current"}
    with HTTPServer(("127.0.0.1", 0), Proxy) as proxy:
        threading.Thread(target=proxy.serve_forever, daemon=True).start()
        transport = httpx.HTTPTransport(proxy=f"http://127.0.0.1:{proxy.server_port}")
        oauth = OAuth(CONFIG, saved.append, tokens, transport)
        oauth.metadata = metadata()
        try:
            with pytest.raises(httpx.ProxyError) as exc:
                oauth.access_token()
        finally:
            oauth.close()
            proxy.shutdown()
    assert len(received) == 1 and received[0].startswith("CONNECT api.assinafy.com.br:443 ")
    assert "current" not in received[0]
    assert saved == [{}, tokens] and oauth.tokens == tokens
    assert "Verifique a rede" in error_message(exc.value)


def test_discovery_failure_keeps_the_current_token():
    posts, saved = [], []

    def upstream(request):
        if request.method == "GET":
            return httpx.Response(503)
        posts.append(request)
        return httpx.Response(200, json=token_response())

    tokens = {"access_token": "expired", "expires_at": 0, "refresh_token": "current"}
    oauth = OAuth(CONFIG, saved.append, tokens, httpx.MockTransport(upstream))
    with pytest.raises(httpx.HTTPStatusError):
        oauth.access_token()
    assert not posts and not saved and oauth.tokens == tokens
    oauth.close()


@pytest.mark.parametrize("refresh", [None, "", "current"])
def test_refresh_without_a_new_refresh_token_requires_reconnect(refresh):
    sent, saved = [], []

    def upstream(request):
        sent.append(request)
        data = {**token_response(), "refresh_token": refresh}
        return httpx.Response(200, json={k: v for k, v in data.items() if v is not None})

    tokens = {"access_token": "expired", "expires_at": 0, "refresh_token": "current"}
    oauth = OAuth(CONFIG, saved.append, tokens, httpx.MockTransport(upstream))
    oauth.metadata = metadata()
    with pytest.raises(ConnectionError, match="Reconecte a conta") as exc:
        oauth.access_token()
    message = f"{exc.value} {exc.value.__cause__}"
    assert "current" not in message and "test-access" not in message
    with pytest.raises(ConnectionError):
        oauth.access_token()
    assert len(sent) == 1 and saved == [{}] and oauth.tokens == {}
    oauth.close()


@pytest.mark.parametrize("grant", ["authorization_code", "refresh_token"])
def test_token_endpoint_is_never_retried(grant):
    calls, saved = [], []

    def upstream(request):
        calls.append(request)
        raise httpx.ReadTimeout("maybe processed")

    oauth = OAuth(CONFIG, saved.append, transport=httpx.MockTransport(upstream))
    oauth.metadata = metadata()
    with pytest.raises(httpx.ReadTimeout):
        oauth._exchange({"grant_type": grant})
    assert len(calls) == 1 and not saved
    oauth.close()


@pytest.mark.parametrize(
    ("error", "message"), [("access_denied", "cancelada"), ("invalid_scope", "invalid_scope")]
)
def test_authorization_errors_stop_before_exchange(error, message):
    posts, browsers = [], []

    def upstream(request):
        if request.method == "GET":
            return httpx.Response(200, json=metadata())
        posts.append(request)
        return httpx.Response(200, json=token_response())

    def open_browser(url):
        state = parse_qs(urlsplit(url).query)["state"][0]
        callback = f"http://127.0.0.1:{state.split('.')[1]}/callback"
        params = {"state": state, "iss": CONFIG.issuer, "error": error}

        def browser():
            with httpx.Client(trust_env=False, timeout=5) as client:
                client.get(callback, params=params)

        browsers.append(threading.Thread(target=browser))
        browsers[-1].start()

    oauth = OAuth(CONFIG, lambda _: None, transport=httpx.MockTransport(upstream))
    try:
        with pytest.raises(ConnectionError, match=message):
            oauth.connect(open_browser, timeout=5)
    finally:
        for thread in browsers:
            thread.join(timeout=5)
        oauth.close()
    assert not posts


def test_refresh_storage_failure_sends_no_request():
    requests = []

    def failed_save(_):
        raise OSError("disk unavailable")

    oauth = OAuth(
        CONFIG,
        failed_save,
        {"refresh_token": "old"},
        httpx.MockTransport(lambda req: requests.append(req)),
    )
    oauth.metadata = metadata()
    with pytest.raises(OSError):
        oauth.access_token()
    assert requests == []
    oauth.close()


@pytest.mark.parametrize(
    "changes",
    [
        {"token_type": "Basic"},
        {"access_token": ""},
        {"expires_in": True},
        {"expires_in": -1},
        {"scope": "documents:write"},
        {"refresh_token": ""},
    ],
)
def test_invalid_token_response(changes):
    saved = []
    oauth = OAuth(
        CONFIG,
        saved.append,
        transport=httpx.MockTransport(
            lambda _: httpx.Response(200, json={**token_response(), **changes})
        ),
    )
    oauth.metadata = metadata()
    with pytest.raises(ConnectionError):
        oauth._exchange({"grant_type": "authorization_code"})
    assert not saved
    oauth.close()


@pytest.mark.parametrize("status", [200, 401, 500])
def test_revocation_and_no_id_token_usage(status):
    saved, calls = [], []

    def upstream(request):
        assert request.headers["content-type"] == "application/x-www-form-urlencoded"
        calls.append(parse_qs(request.content.decode()))
        return httpx.Response(status, json={})

    oauth = OAuth(
        CONFIG, saved.append, {"refresh_token": "test-refresh"}, httpx.MockTransport(upstream)
    )
    oauth.metadata = metadata()
    if status == 500:
        with pytest.raises(ConnectionError):
            oauth.disconnect()
        assert oauth.tokens and not saved
    else:
        oauth.disconnect()
        assert saved == [{}] and not oauth.tokens
    assert calls == [{"client_id": [CONFIG.client_id], "token": ["test-refresh"]}]
    oauth.close()


def test_redirects_never_receive_credentials():
    calls = []

    def upstream(request):
        calls.append(request)
        return httpx.Response(307, headers={"Location": "https://other.invalid/token"})

    oauth = OAuth(CONFIG, lambda _: None, transport=httpx.MockTransport(upstream))
    oauth.metadata = metadata()
    with pytest.raises(ConnectionError):
        oauth._exchange({"grant_type": "authorization_code"})
    assert len(calls) == 1
    oauth.close()
