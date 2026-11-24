import json
import time

import httpx
import pytest
from assinafy import ApiError, AssinafyError
from assinafy_libreoffice.contracts import parse_term
from assinafy_libreoffice.oauth import ConnectionError, OAuth
from assinafy_libreoffice.validation import validate_taxpayer_id
from assinafy_libreoffice.workflow import Workflow, error_message
from test_oauth import CONFIG, token_response

STAMP = "2026-10-23T12:00:00Z"
TERMS = {
    "resource": "document_contract", "document_id": "doc1", "status": "active",
    "term": 1, "value": 1200.50, "end_date": "2030-12-31", "updated_at": STAMP,
}


@pytest.fixture
def backend(monkeypatch):
    requests, response = [], {"status": 200, "data": TERMS}
    status, failure = [200], []

    def handle(request):
        requests.append(request)
        assert request.headers["Authorization"] == "Bearer test-access"
        assert "X-Api-Key" not in request.headers
        if request.url.path == "/v1/accounts":
            return httpx.Response(200, json={"status": 200, "data": [{"id": "account1"}]})
        if failure:
            raise failure[0]
        return httpx.Response(status[0], json=response, headers={
            "X-Pagination-Current-Page": "2", "X-Pagination-Total-Count": "21",
            "X-Pagination-Page-Count": "2", "X-Pagination-Per-Page": "20",
        })

    real_http = httpx.Client

    def http(*args, **kwargs):
        return real_http(*args, **(kwargs | {"transport": httpx.MockTransport(handle)}))

    monkeypatch.setattr(httpx, "Client", http)
    oauth = OAuth(CONFIG, lambda _: None, token_response() | {"expires_at": time.time() + 3600})
    workflow = Workflow(oauth, lambda *_: None)
    yield workflow, requests, response, status, failure
    oauth.close()


def test_list_filters_pagination_and_read_scope(backend):
    workflow, requests, response, _, _ = backend
    response["data"] = [TERMS]
    workflow.oauth.tokens["scope"] = "documents:read"
    result = workflow.list_contracts(2, "Test contract", "active")
    assert result == {"data": [TERMS], "meta": {
        "current_page": 2, "per_page": 20, "total": 21, "last_page": 2,
    }}
    request = requests[-1]
    assert request.method == "GET"
    assert request.url.path == "/v1/accounts/account1/contracts"
    assert dict(request.url.params) == {
        "page": "2", "per-page": "20", "search": "Test contract", "status": "active",
        "sort": "-updated_at", "expand": "document",
    }


@pytest.mark.parametrize("action,suffix", [("contract", ""), ("related_contract", "/related")])
@pytest.mark.parametrize("resource", ["document_contract", "document_contract_amendment"])
def test_contract_read_shapes(backend, action, suffix, resource):
    workflow, requests, response, _, _ = backend
    response["data"] = TERMS | {"resource": resource}
    assert getattr(workflow, action)("doc1") == response["data"]
    assert requests[-1].url.path == "/v1/accounts/account1/documents/doc1/contract" + suffix
    assert requests[-1].method == "GET"


@pytest.mark.parametrize("code,missing", [("NoContractTerms", True), (None, False)])
def test_missing_terms_are_distinct_from_missing_document(backend, code, missing):
    workflow, _, response, status, _ = backend
    status[0] = 404
    response.update(status=404, data=None, code=code)
    if missing:
        assert workflow.contract("doc1") is None
    else:
        with pytest.raises(ApiError) as exc:
            workflow.contract("doc1")
        assert exc.value.status_code == 404


@pytest.mark.parametrize("action,method,suffix,payload", [
    ("update_contract", "PUT", "", {"counterparty_name": "Example", "status": "draft"}),
    ("update_contract", "PUT", "", {"value": None, "metadata": None, "external_references": []}),
    ("update_contract", "PUT", "", {"value": None, "updated_at": None}),
    ("update_contract", "PUT", "", {"parent_document_id": "root1", "relation_type": "amendment",
                                     "value": 1500, "updated_at": STAMP}),
    ("update_contract", "PUT", "", {"parent_document_id": None, "updated_at": STAMP}),
    ("renew_contract", "POST", "/renew", {"term": 1, "updated_at": STAMP}),
    ("renew_contract", "POST", "/renew", {"end_date": "2031-12-31", "value": None}),
    ("terminate_contract", "POST", "/terminate", {"reason": "Agreed", "updated_at": STAMP}),
    ("terminate_contract", "POST", "/terminate", {}),
])
def test_contract_writes_preserve_body_and_workspace(backend, action, method, suffix, payload):
    workflow, requests, _, _, _ = backend
    assert getattr(workflow, action)("doc1", payload) == TERMS
    request = requests[-1]
    assert request.method == method
    assert request.url.path == "/v1/accounts/account1/documents/doc1/contract" + suffix
    assert json.loads(request.content) == payload


@pytest.mark.parametrize("action,payload", [
    ("update_contract", {"value": None}), ("renew_contract", {"term": 1}),
    ("terminate_contract", {"reason": "Test"}),
])
def test_contract_writes_require_write_scope(backend, action, payload):
    workflow, requests, _, _, _ = backend
    workflow.oauth.tokens["scope"] = "documents:read"
    with pytest.raises(ConnectionError, match="documents:write"):
        getattr(workflow, action)("doc1", payload)
    assert not requests


@pytest.mark.parametrize("action,payload", [
    ("update_contract", {}), ("update_contract", []),
    ("update_contract", {"updated_at": "bad"}), ("renew_contract", {"value": None}),
    ("renew_contract", {"term": True}), ("renew_contract", {"term": 0}),
    ("terminate_contract", {"reason": "x" * 501}),
])
def test_invalid_contract_body_never_writes(backend, action, payload):
    workflow, requests, _, _, _ = backend
    with pytest.raises((ValueError, AssinafyError)):
        getattr(workflow, action)("doc1", payload)
    assert all(request.method == "GET" for request in requests)


@pytest.mark.parametrize("identifier", ["../other", "doc/other", "doc?query", "doc%2fother"])
def test_contract_ids_cannot_escape_workspace(backend, identifier):
    workflow, requests, _, _, _ = backend
    with pytest.raises(AssinafyError):
        workflow.contract(identifier)
    assert all(request.url.path == "/v1/accounts" for request in requests)


@pytest.mark.parametrize("action,payload", [
    ("update_contract", {"value": 100, "updated_at": STAMP}),
    ("renew_contract", {"term": 1, "updated_at": STAMP}),
    ("terminate_contract", {"reason": "Test", "updated_at": STAMP}),
])
@pytest.mark.parametrize("error", [409, 403, 429, 500, "timeout"])
def test_failed_contract_writes_never_replay(backend, action, payload, error):
    workflow, requests, response, status, failure = backend
    if error == "timeout":
        failure.append(httpx.ReadTimeout("uncertain write"))
    else:
        status[0] = error
        response.update(status=error, data=None, code="ContractChanged")
    with pytest.raises(AssinafyError) as exc:
        getattr(workflow, action)("doc1", payload)
    assert len([r for r in requests if r.method != "GET"]) == 1
    if error == 409:
        assert "contrato mudou" in error_message(exc.value)


def test_contract_validation_shows_all_returned_fields(backend):
    workflow, _, response, status, _ = backend
    status[0] = 400
    response.update(status=400, errors=[
        {"field": "value", "code": "TooSmall", "message": "Valor inválido"},
        {"field": "currency", "code": "Invalid"},
    ])
    with pytest.raises(ApiError) as exc:
        workflow.update_contract("doc1", {"value": 1})
    assert "value: Valor inválido" in error_message(exc.value)
    assert "currency: Invalid" in error_message(exc.value)


@pytest.mark.parametrize("identity", ["10000098744", "100.000.987-44", "11222333000181",
                                      "12.ABC.345/01DE-35"])
def test_taxpayer_check_digits(identity):
    validate_taxpayer_id(identity)


@pytest.mark.parametrize("identity", ["", "garbage", "0" * 11, "1" * 11, "0" * 14,
                                      "10000098745", "12.ABC.345/01DE-34"])
def test_invalid_taxpayer_check_digits(identity):
    with pytest.raises(ValueError, match="CPF"):
        validate_taxpayer_id(identity)


@pytest.mark.parametrize("text,kind,expected", [
    ("", "money", None), ("0", "money", 0), ("1200.50", "money", 1200.5),
    ("9999999999999.99", "money", 9999999999999.99), (" brl ", "currency", "BRL"),
    ("2032-02-29", "date", "2032-02-29"), ("0", "days", 0), ("600", "months", 600),
])
def test_contract_form_field_parsing(text, kind, expected):
    assert parse_term(text, kind) == expected


@pytest.mark.parametrize("text,kind", [
    ("NaN", "money"), ("Infinity", "money"), ("-0.01", "money"), ("1.001", "money"),
    ("10000000000000", "money"), ("1,50", "money"), ("BR", "currency"),
    ("2030-02-29", "date"), ("20301231", "date"), ("0999-12-31", "date"),
    ("0", "months"), ("601", "months"), ("3651", "days"), ("1.5", "days"),
])
def test_contract_form_rejects_invalid_fields(text, kind):
    with pytest.raises(ValueError):
        parse_term(text, kind)
