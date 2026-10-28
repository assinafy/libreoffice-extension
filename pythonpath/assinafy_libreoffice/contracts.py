"""Contract tracking through the bundled SDK's authenticated transport."""

from __future__ import annotations

import re
from datetime import date
from decimal import Decimal, InvalidOperation

from assinafy import ApiError
from assinafy.resources.base import BaseResource
from assinafy.utils import QUERY_PARAM_ALIASES, clean_params, validate_datetime

from .validation import validate_taxpayer_id

TERM_FIELDS = (
    ("counterparty_name", "Contraparte", "text"),
    ("counterparty_taxpayer_id", "CPF/CNPJ da contraparte", "taxpayer"),
    ("value", "Valor (ex.: 1200.50)", "money"),
    ("currency", "Moeda (ex.: BRL)", "currency"),
    ("start_date", "Início (AAAA-MM-DD)", "date"),
    ("end_date", "Término (AAAA-MM-DD)", "date"),
    ("renewal_term_months", "Prazo de renovação (meses)", "months"),
    ("notice_period_days", "Aviso prévio (dias)", "days"),
)
RELATIONS = (("amendment", "Aditivo"), ("renewal", "Renovação"), ("termination", "Rescisão"))


def parse_term(text, kind):
    """Parse a native field; blank means an explicit clear, never an omitted edit."""
    text = text.strip()
    if not text:
        return None
    if kind == "money":
        try:
            amount = Decimal(text)
            if not amount.is_finite() or not 0 <= amount <= Decimal("9999999999999.99"):
                raise ValueError
            if amount != amount.quantize(Decimal("0.01")):
                raise ValueError
        except (InvalidOperation, ValueError) as exc:
            raise ValueError(
                "Informe um valor positivo ou zero, com até duas casas decimais."
            ) from exc
        return float(amount)
    if kind in {"months", "days"}:
        minimum, maximum = (1, 600) if kind == "months" else (0, 3650)
        if not text.isascii() or not text.isdigit() or not minimum <= int(text) <= maximum:
            raise ValueError(f"Informe um inteiro entre {minimum} e {maximum}.")
        return int(text)
    if kind == "date":
        try:
            if not re.fullmatch(r"[0-9]{4}-[0-9]{2}-[0-9]{2}", text):
                raise ValueError
            if date.fromisoformat(text).year < 1000:
                raise ValueError
        except ValueError as exc:
            raise ValueError(
                "Informe uma data válida no formato AAAA-MM-DD, a partir de 1000."
            ) from exc
    elif kind == "currency":
        if not re.fullmatch(r"[A-Za-z]{3}", text):
            raise ValueError("Informe uma moeda de três letras, como BRL.")
        return text.upper()
    elif kind == "taxpayer":
        validate_taxpayer_id(text)
    elif len(text) > 255:
        raise ValueError("A contraparte deve ter até 255 caracteres.")
    return text


class Contracts(BaseResource):
    """Six CLM endpoints; request/response examples are in docs/INTEGRATION.md.

    Construct with ``client.get_http_client()`` and the authorized account ID.
    Reuses SDK authentication, TLS, errors, envelope handling and pagination.
    """

    # shortcut: SDK 2.0.1 has no CLM resource; replace this adapter when the SDK supplies it.
    def _path(self, document_id):
        return (
            f"accounts/{self._account_id()}/documents/"
            f"{self._path_id(document_id, 'Document ID')}/contract"
        )

    def list(self, params):
        """GET /accounts/{account}/contracts; filters in query, paginated contract objects out."""
        return self._call_list(
            "List contracts",
            lambda: self._http.get(
                f"accounts/{self._account_id()}/contracts",
                params=clean_params(params, QUERY_PARAM_ALIASES),
            ),
        )

    def get(self, document_id):
        """GET document contract; return terms/link, or None only for NoContractTerms."""
        path = self._path(document_id)
        try:
            return self._call_dict("Read contract", lambda: self._http.get(path))
        except ApiError as exc:
            if (
                exc.status_code == 404
                and isinstance(exc.response_data, dict)
                and exc.response_data.get("code") == "NoContractTerms"
            ):
                return None
            raise

    def related(self, document_id):
        """GET document contract/related; return root terms including all amendments."""
        path = self._path(document_id) + "/related"
        return self._call_dict("Read related contracts", lambda: self._http.get(path))

    def update(self, document_id, payload):
        """PUT partial terms or amendment delta, preserving null; return terms/link."""
        self._payload(payload)
        path = self._path(document_id)
        return self._call_dict("Save contract", lambda: self._http.put(path, json=payload))

    def renew(self, document_id, payload):
        """POST contract/renew with current term or target end_date; return renewed terms."""
        self._payload(payload)
        term = payload.get("term")
        if not payload.get("end_date") and (
            isinstance(term, bool) or not isinstance(term, int) or term < 1
        ):
            raise ValueError("Atualize o contrato para confirmar o período a renovar.")
        path = self._path(document_id) + "/renew"
        return self._call_dict("Renew contract", lambda: self._http.post(path, json=payload))

    def terminate(self, document_id, payload):
        """POST contract/terminate with reason/updated_at; return terminated terms."""
        self._payload(payload, allow_empty=True)
        reason = payload.get("reason")
        if reason is not None and (not isinstance(reason, str) or len(reason) > 500):
            raise ValueError("O motivo deve ter até 500 caracteres.")
        path = self._path(document_id) + "/terminate"
        return self._call_dict("Terminate contract", lambda: self._http.post(path, json=payload))

    @staticmethod
    def _payload(payload, *, allow_empty=False):
        if not isinstance(payload, dict) or (not payload and not allow_empty):
            raise ValueError("Informe os dados do contrato.")
        if payload.get("updated_at") is not None:
            validate_datetime(payload["updated_at"], "updated_at")
