import ssl
import threading
from urllib.parse import parse_qs, urlencode, urlsplit

import httpx
import pytest
from assinafy_libreoffice.config import SCOPES, Config, load_config
from assinafy_libreoffice.oauth import ConnectionError, OAuth, challenge

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
        "scope": "account:read documents:read documents:write",
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
        assert set(SCOPES.split()) == {
            "account:read",
            "documents:read",
            "documents:write",
            "offline_access",
        }
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


def test_revocation_and_no_id_token_usage():
    saved, calls = [], []

    def upstream(request):
        calls.append(parse_qs(request.content.decode()))
        return httpx.Response(200, json={})

    oauth = OAuth(
        CONFIG, saved.append, {"refresh_token": "test-refresh"}, httpx.MockTransport(upstream)
    )
    oauth.metadata = metadata()
    oauth.disconnect()
    assert calls[0]["token"] == ["test-refresh"] and saved == [{}]
    assert not oauth.tokens
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
