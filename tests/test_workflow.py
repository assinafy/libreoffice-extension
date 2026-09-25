import json
import ssl
import time
from dataclasses import replace

import httpx
import pytest
from assinafy import AssinafyClient, AssinafyError
from assinafy_libreoffice.oauth import ConnectionError, OAuth
from assinafy_libreoffice.workflow import (
    Recipient,
    Workflow,
    error_message,
    validate_pdf,
    validate_request,
)
from test_oauth import CONFIG, token_response

CONTACT = "recipient" + "@" + "example.invalid"
RECIPIENT = Recipient("Test Recipient", CONTACT)
PDF = b"%PDF-1.7\nTest bytes"


@pytest.fixture
def backend(monkeypatch):
    events, records = [], []
    doc = {"id": "doc1", "account_id": "account1", "status": "metadata_ready", "assignment": None}
    failures = {}
    signer = {"id": "signer1", "full_name": RECIPIENT.full_name, "email": CONTACT}

    def request(req):
        path = req.url.path.removeprefix("/v1/")
        events.append((req.method, path, req.content))
        assert req.headers["Authorization"] == "Bearer test-access"
        assert "X-Api-Key" not in req.headers
        if (req.method, path) in failures:
            problem = failures[(req.method, path)]
            if isinstance(problem, Exception):
                raise problem
            return httpx.Response(problem, json={"status": problem})
        if path == "accounts":
            data = [{"id": "account1", "name": "Test workspace"}]
        elif path == "accounts/account1/documents":
            data = doc if req.method == "POST" else [doc]
        elif path == "documents/doc1":
            data = doc
        elif path.endswith("estimate-cost"):
            data = {"has_sufficient_resources": True, "total_credits": 0}
        elif path == "accounts/account1/signers":
            data = signer if req.method == "POST" else []
        elif path == "accounts/account1/signers/signer1":
            data = signer
        elif path == "documents/doc1/assignments":
            doc["assignment"] = {"id": "assignment1", "signers": [signer]}
            data = doc["assignment"]
        elif path.endswith("/download/certificated"):
            return httpx.Response(200, content=PDF)
        elif path.endswith("/resend"):
            data = {"is_sent": True}
        elif path == "documents/statuses":
            data = [{"code": "metadata_ready", "deletable": True}]
        else:
            raise AssertionError((req.method, path))
        return httpx.Response(200, json={"status": 200, "data": data})

    real_http = httpx.Client

    def client(*a, **kw):
        kw["transport"] = httpx.MockTransport(request)
        return real_http(*a, **kw)

    monkeypatch.setattr(httpx, "Client", client)
    oauth = OAuth(CONFIG, lambda _: None, {**token_response(), "expires_at": time.time() + 3600})
    workflow = Workflow(oauth, lambda *a: records.append(a), AssinafyClient)
    yield workflow, events, records, doc, failures
    oauth.close()


def test_prepare_and_send_contract(backend):
    workflow, events, records, doc, _ = backend
    prepared = workflow.prepare(PDF, "contract.pdf", [RECIPIENT])
    assert prepared["document_id"] == "doc1"
    assert records == [("doc1", "uploaded")]
    assert not any("/signers" in path or path.endswith("/assignments") for _, path, _ in events)
    upload = next(
        body for method, path, body in events if method == "POST" and path.endswith("/documents")
    )
    assert b'name="file"' in upload and PDF in upload
    workflow.send("doc1", [RECIPIENT], "Test message")
    payload = json.loads(
        next(body for method, path, body in events if path.endswith("/assignments"))
    )
    assert payload == {
        "method": "virtual",
        "signers": [
            {
                "id": "signer1",
                "step": 1,
                "verification_method": "Email",
                "notification_methods": ["Email"],
            }
        ],
        "message": "Test message",
    }
    assert records[-1] == ("doc1", "sent")
    assert workflow.download("doc1") == PDF
    assert workflow.resend("doc1", "signer1")["is_sent"]


@pytest.mark.parametrize(
    "recipient",
    [
        replace(RECIPIENT, email="invalid"),
        replace(RECIPIENT, full_name=""),
        replace(RECIPIENT, step=True),
        replace(RECIPIENT, step=0),
        replace(RECIPIENT, verification_method="DigitalCertificate"),
    ],
)
def test_invalid_contacts_fail_before_upload(backend, recipient):
    workflow, events, _, _, _ = backend
    with pytest.raises((ValueError, AssinafyError)):
        workflow.prepare(PDF, "test.pdf", [recipient])
    assert not events


def test_order_certificate_validation():
    cert = replace(RECIPIENT, government_id="00000000000", verification_method="DigitalCertificate")
    other = replace(RECIPIENT, email="second" + "@" + "example.invalid")
    with pytest.raises(ValueError):
        validate_request([cert, other])
    validate_request([cert, replace(other, step=2)])
    with pytest.raises(ValueError):
        validate_request([RECIPIENT, RECIPIENT])


@pytest.mark.parametrize("steps", [(2,), (1, 3), (10**12,)])
def test_invalid_order_fails_before_any_request(backend, steps):
    workflow, events, records, _, _ = backend
    recipients = [
        replace(RECIPIENT, email=f"recipient{index}@example.invalid", step=step)
        for index, step in enumerate(steps)
    ]
    for action in (
        lambda: workflow.prepare(PDF, "test.pdf", recipients),
        lambda: workflow.estimate("doc1", recipients),
        lambda: workflow.send("doc1", recipients),
    ):
        with pytest.raises(ValueError, match="etapas"):
            action()
        assert not events and not records


def test_partial_failure_preserves_id(backend):
    workflow, events, records, _, failures = backend
    failures[("GET", "documents/doc1")] = 404
    with pytest.raises(AssinafyError) as exc:
        workflow.prepare(PDF, "test.pdf", [RECIPIENT])
    assert exc.value.context["document_id"] == "doc1"
    assert records == [("doc1", "uploaded")]
    assert not any(method == "DELETE" for method, _, _ in events)


@pytest.mark.parametrize("status", ["uploaded", "sending", "check_status", "sent"])
def test_history_failure_preserves_document_id_without_replaying(backend, status):
    workflow, events, records, _, failures = backend

    def record(document_id, value):
        if value == status:
            raise OSError("history unavailable")
        records.append((document_id, value))

    workflow.record = record
    if status == "check_status":
        failures[("POST", "documents/doc1/assignments")] = httpx.ReadTimeout("interrupted")
    with pytest.raises(AssinafyError) as exc:
        if status == "uploaded":
            workflow.prepare(PDF, "test.pdf", [RECIPIENT])
        else:
            workflow.send("doc1", [RECIPIENT])
    assert exc.value.context["document_id"] == "doc1"
    assert sum(path == "documents/doc1/assignments" for _, path, _ in events) <= 1
    assert not any(method == "DELETE" for method, _, _ in events)
    if status == "sending":
        assert not any(method in {"POST", "PUT"} for method, _, _ in events)


def test_input_errors_are_actionable_before_upload(backend):
    workflow, events, _, _, _ = backend
    for recipients, expiration, expected in (
        ([replace(RECIPIENT, email="invalid")], None, "e-mail"),
        ([RECIPIENT], "not-a-date", "ISO 8601"),
    ):
        with pytest.raises(ValueError) as exc:
            workflow.prepare(PDF, "test.pdf", recipients, expires_at=expiration)
        assert expected in error_message(exc.value)
        assert not events


def test_ambiguous_send_is_not_replayed(backend):
    workflow, events, records, _, failures = backend
    failures[("POST", "documents/doc1/assignments")] = httpx.ReadTimeout("private details")
    with pytest.raises(AssinafyError) as exc:
        workflow.send("doc1", [RECIPIENT])
    assert exc.value.context["document_id"] == "doc1"
    assert records[-1] == ("doc1", "check_status")
    assert sum(path == "documents/doc1/assignments" for _, path, _ in events) == 1


def test_workspace_boundary_and_path_validation(backend):
    workflow, events, _, doc, _ = backend
    doc["account_id"] = "other"
    with pytest.raises(ConnectionError):
        workflow.download("doc1")
    assert not any("download" in path for _, path, _ in events)
    with pytest.raises(AssinafyError):
        workflow.document("../other")


def test_sdk_client_requires_tls_1_2_even_if_the_runtime_allows_less(monkeypatch):
    default_context = ssl.create_default_context

    def legacy_default(*args, **kwargs):
        context = default_context(*args, **kwargs)
        context.minimum_version = ssl.TLSVersion.MINIMUM_SUPPORTED
        return context

    monkeypatch.setattr(ssl, "create_default_context", legacy_default)
    monkeypatch.setenv("HTTPS_PROXY", "http://127.0.0.1:9")
    oauth = OAuth(CONFIG, lambda _: None, {**token_response(), "expires_at": time.time() + 3600})
    with Workflow(oauth, lambda *a: None).client() as client:
        http = client.get_http_client()
        transports = [t for t in (http._transport, *http._mounts.values()) if t is not None]
        assert len(transports) >= 2
        for transport in transports:
            context = transport._pool._ssl_context
            assert context.minimum_version == ssl.TLSVersion.TLSv1_2
            assert context.verify_mode == ssl.CERT_REQUIRED
    oauth.close()


def test_connection_failures_are_reported_as_network_errors(backend):
    workflow, _, _, _, failures = backend
    failures[("GET", "accounts")] = httpx.ConnectError("[SSL: UNSUPPORTED_PROTOCOL]")
    with pytest.raises(AssinafyError) as exc:
        workflow.account_info()
    assert "TLS 1.2" in error_message(exc.value)
    assert "TLS" not in error_message(AssinafyError("x", {"document_id": "doc1"}))


def test_scope_prevents_writes(backend):
    workflow, events, _, _, _ = backend
    workflow.oauth.tokens["scope"] = "documents:read"
    with pytest.raises(ConnectionError):
        workflow.prepare(PDF, "test.pdf", [RECIPIENT])
    assert not events


def test_existing_assignment_and_deletion_status(backend):
    workflow, events, _, doc, _ = backend
    doc["assignment"] = {"id": "existing"}
    with pytest.raises(ValueError):
        workflow.send("doc1", [RECIPIENT])
    doc["status"] = "certificated"
    with pytest.raises(ValueError):
        workflow.delete("doc1")
    assert not any(method == "DELETE" for method, _, _ in events)
    doc["status"] = "metadata_ready"
    workflow.delete("doc1")
    assert any(method == "DELETE" for method, _, _ in events)


def test_resume_estimates_existing_document_without_another_upload(backend):
    workflow, events, _, _, _ = backend
    assert workflow.estimate("doc1", [RECIPIENT])["has_sufficient_resources"]
    assert not any(path == "accounts/account1/documents" for _, path, _ in events)


def test_certificate_updates_identity_before_assignment(backend):
    workflow, events, _, _, _ = backend
    recipient = replace(
        RECIPIENT, government_id="00000000000", verification_method="DigitalCertificate"
    )
    workflow.send("doc1", [recipient])
    creation = next(
        json.loads(body)
        for method, path, body in events
        if method == "POST" and path == "accounts/account1/signers"
    )
    assert "government_id" not in creation
    identity = next(
        json.loads(body)
        for method, path, body in events
        if method == "PUT" and path.endswith("signers/signer1")
    )
    assert identity == {"government_id": "00000000000"}
    assignment = json.loads(events[-1][2])
    assert assignment["signers"][0]["verification_method"] == "DigitalCertificate"


def test_refreshed_scopes_checked_before_retrying_a_write(backend, monkeypatch):
    workflow, events, _, _, failures = backend
    workflow.account_info()
    failures[("POST", "accounts/account1/documents")] = 401
    access_token = workflow.oauth.access_token

    def refresh(force=False):
        if force:
            workflow.oauth.tokens["scope"] = "documents:read"
        return access_token()

    monkeypatch.setattr(workflow.oauth, "access_token", refresh)
    with pytest.raises(ConnectionError):
        workflow.prepare(PDF, "test.pdf", [RECIPIENT])
    assert sum(method == "POST" for method, _, _ in events) == 1


def test_signed_pdf_may_be_larger_than_upload_limit():
    data = b"%PDF-1.7\n" + b"x" * (25 * 1024 * 1024)
    with pytest.raises(ValueError):
        validate_pdf(data)
    validate_pdf(data, upload=False)
