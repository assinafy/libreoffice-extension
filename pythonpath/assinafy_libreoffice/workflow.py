"""Assinafy document operations for the native LibreOffice UI."""

from __future__ import annotations

import hashlib
import re
from contextlib import suppress
from dataclasses import dataclass

from assinafy import ApiError, AssinafyClient, AssinafyError, ValidationError
from assinafy.utils import validate_datetime, validate_email

from .oauth import UNSENT, ConnectionError


@dataclass(frozen=True)
class Recipient:
    full_name: str
    email: str
    government_id: str = ""
    verification_method: str = "Email"
    step: int = 1

    def validate(self):
        if not isinstance(self.full_name, str) or not self.full_name.strip():
            raise ValueError("Informe o nome de cada signatário.")
        if len(self.full_name) > 255:
            raise ValueError("Informe um nome e um e-mail válidos para cada signatário.")
        try:
            validate_email(self.email)
        except ValidationError as exc:
            raise ValueError("Informe um e-mail válido para cada signatário.") from exc
        if self.verification_method not in {"Email", "DigitalCertificate"}:
            raise ValueError("Selecione Email ou DigitalCertificate.")
        if isinstance(self.step, bool) or not isinstance(self.step, int) or self.step < 1:
            raise ValueError("A etapa de assinatura deve ser um inteiro positivo.")
        if not isinstance(self.government_id, str) or len(self.government_id) > 32:
            raise ValueError("CPF/CNPJ inválido.")
        if self.verification_method == "DigitalCertificate" and not self.government_id.strip():
            raise ValueError("Assinatura com certificado exige CPF/CNPJ.")

    def pricing(self):
        return {"verification_method": self.verification_method, "notification_methods": ["Email"]}


def validate_request(recipients, message="", expires_at=None):
    if not recipients or len(recipients) > 100:
        raise ValueError("Informe entre 1 e 100 signatários.")
    if not isinstance(message, str) or len(message) > 2000:
        raise ValueError("A mensagem deve ter até 2000 caracteres.")
    try:
        validate_datetime(expires_at, "expires_at", allow_none=True)
    except ValidationError as exc:
        raise ValueError("Informe uma expiração ISO 8601 com fuso horário.") from exc
    emails = set()
    for recipient in recipients:
        recipient.validate()
        email = recipient.email.casefold()
        if email in emails:
            raise ValueError("Um e-mail não pode aparecer duas vezes no mesmo envio.")
        emails.add(email)
        if (
            recipient.verification_method == "DigitalCertificate"
            and sum(r.step == recipient.step for r in recipients) != 1
        ):
            raise ValueError("Cada signatário por certificado deve estar sozinho na sua etapa.")
    steps = {recipient.step for recipient in recipients}
    if steps != set(range(1, len(steps) + 1)):
        raise ValueError("As etapas devem começar em 1 e seguir sem intervalos: 1, 2, 3…")


def validate_pdf(data, *, upload=True):
    if not isinstance(data, bytes) or not data.startswith(b"%PDF-"):
        raise ValueError("Não foi possível gerar um PDF válido.")
    if upload and len(data) > 25 * 1024 * 1024:
        raise ValueError("O PDF excede o limite de 25 MiB da Assinafy.")


def require_id(data):
    value = data.get("id") if isinstance(data, dict) else None
    if not isinstance(value, str) or not re.fullmatch(r"[A-Za-z0-9_-]+", value):
        raise RuntimeError("A API não retornou um identificador válido.")
    return value


def error_message(error):
    if isinstance(error, (ConnectionError, ValueError)):
        return str(error)
    if isinstance(error, UNSENT) or isinstance(error.__cause__, UNSENT):
        return (
            "Não foi possível abrir uma conexão segura (TLS 1.2 ou superior) com a Assinafy. "
            "Verifique a rede e tente novamente."
        )
    if isinstance(error, ApiError):
        return {
            400: "Dados recusados. Confira destinatários, documento e permissões do plano.",
            401: "Sua autorização expirou. Reconecte a conta.",
            403: "Operação não autorizada. Confira o workspace e as permissões.",
            404: "O documento ou recurso não está disponível.",
            429: "Limite de requisições atingido. Aguarde e tente novamente.",
        }.get(
            error.status_code, "A Assinafy está indisponível. Consulte o status antes de reenviar."
        )
    return "A operação não foi concluída. Consulte Envios recentes antes de tentar novamente."


class Workflow:
    def __init__(self, oauth, record, factory=AssinafyClient):
        self.oauth = oauth
        self.record = record
        self.factory = factory
        self.account = None

    def _record(self, document_id, status):
        try:
            self.record(document_id, status)
        except Exception as exc:
            raise AssinafyError(
                "Não foi possível atualizar o histórico local.", {"document_id": document_id}
            ) from exc

    def client(self, **options):
        # assinafy >= 1.9.1 requires TLS 1.2+ on its own client, proxied transports included.
        return self.factory(
            token=self.oauth.access_token(), base_url=self.oauth.config.api_url, **options
        )

    def call(self, action, *, write=False):
        with self.oauth.lock:
            for attempt in range(2):
                self.oauth.require("documents:write" if write else "documents:read")
                try:
                    if self.account is None:
                        with self.client() as client:
                            accounts = client.accounts.list()
                            if len(accounts) != 1:
                                raise ConnectionError(
                                    "A conexão deve autorizar exatamente um workspace."
                                )
                            require_id(accounts[0])
                            self.account = accounts[0]
                    with self.client(account_id=self.account["id"]) as client:
                        return action(client)
                except ApiError as exc:
                    if exc.status_code != 401 or attempt:
                        raise
                    self.oauth.access_token(force=True)
            raise ConnectionError("Não foi possível selecionar o workspace autorizado.")

    def account_info(self):
        self.call(lambda client: None)
        return self.account

    def list_documents(self, page=1, search=""):
        if isinstance(page, bool) or not isinstance(page, int) or page < 1:
            raise ValueError("Página inválida.")
        return self.call(
            lambda c: c.documents.list(
                {"page": page, "per_page": 20, "search": search, "sort": "updated_at"}
            )
        )

    def document(self, document_id):
        def get(client):
            doc = client.documents.get(document_id)
            if doc.get("account_id") != self.account["id"]:
                raise ConnectionError("O documento não pertence ao workspace conectado.")
            return doc

        return self.call(get)

    def prepare(self, pdf, filename, recipients, message="", expires_at=None):
        validate_pdf(pdf)
        validate_request(recipients, message, expires_at)
        if (
            not isinstance(filename, str)
            or not filename.lower().endswith(".pdf")
            or any(c in filename for c in "/\\\r\n")
            or len(filename) > 255
        ):
            raise ValueError("Nome de arquivo PDF inválido.")
        doc = self.call(
            lambda c: c.documents.upload({"buffer": pdf, "file_name": filename}), write=True
        )
        document_id = require_id(doc)
        self._record(document_id, "uploaded")
        try:
            self.call(
                lambda c: c.documents.wait_until_ready(document_id, timeout=90, poll_interval=2)
            )
            estimate = self.estimate(document_id, recipients)
        except Exception as exc:
            raise AssinafyError(
                "Documento preservado para retomada.", {"document_id": document_id}
            ) from exc
        return {
            "document_id": document_id,
            "sha256": hashlib.sha256(pdf).hexdigest(),
            "filename": filename,
            "estimate": estimate,
        }

    def estimate(self, document_id, recipients):
        validate_request(recipients)
        self.document(document_id)
        return self.call(
            lambda c: c.assignments.estimate_cost(
                document_id,
                {
                    "method": "virtual",
                    "signers": [r.pricing() for r in recipients],
                },
            ),
            write=True,
        )

    def send(self, document_id, recipients, message="", expires_at=None):
        validate_request(recipients, message, expires_at)
        doc = self.document(document_id)
        if doc.get("assignment"):
            raise ValueError(
                "Este documento já tem um envio. Atualize o status antes de continuar."
            )
        self._record(document_id, "sending")
        try:
            signers = []
            for recipient in recipients:
                existing = self.call(lambda c, r=recipient: c.signers.find_by_email(r.email))
                if existing:
                    if existing.get("full_name", "").strip() != recipient.full_name.strip():
                        raise ValueError(
                            "O contato existente tem outro nome. Corrija-o na Assinafy."
                        )
                    signer = existing
                else:
                    signer = self.call(
                        lambda c, r=recipient: c.signers.create(
                            {
                                "full_name": r.full_name.strip(),
                                "email": r.email.strip(),
                            }
                        ),
                        write=True,
                    )
                signer_id = require_id(signer)
                if recipient.government_id:
                    self.call(
                        lambda c, sid=signer_id, r=recipient: c.signers.update(
                            sid, {"government_id": r.government_id}
                        ),
                        write=True,
                    )
                signers.append({"id": signer_id, "step": recipient.step, **recipient.pricing()})
            payload = {"method": "virtual", "signers": signers, "message": message}
            if expires_at:
                payload["expires_at"] = expires_at
            result = self.call(lambda c: c.assignments.create(document_id, payload), write=True)
            require_id(result)
        except Exception as exc:
            with suppress(Exception):
                self.record(document_id, "check_status")
            raise AssinafyError(
                "Consulte o documento antes de reenviar.", {"document_id": document_id}
            ) from exc
        self._record(document_id, "sent")
        return result

    def download(self, document_id, artifact="certificated"):
        if artifact not in {"original", "certificated", "certificate-page", "pades", "bundle"}:
            raise ValueError("Artefato inválido.")
        self.document(document_id)
        data = self.call(lambda c: c.documents.download(document_id, artifact))
        if artifact == "bundle":
            if not data.startswith(b"PK"):
                raise ValueError("O servidor não retornou um arquivo ZIP.")
        else:
            validate_pdf(data, upload=False)
        return data

    def resend(self, document_id, signer_id, *, estimate_only=False):
        doc = self.document(document_id)
        assignment = doc.get("assignment") or {}
        assignment_id = require_id(assignment)
        if not any(s.get("id") == signer_id for s in assignment.get("signers", [])):
            raise ValueError("O signatário não pertence a este envio.")
        return self.call(
            lambda c: (
                c.assignments.estimate_resend_cost(document_id, assignment_id, signer_id)
                if estimate_only
                else c.assignments.resend_notification(document_id, assignment_id, signer_id)
            ),
            write=True,
        )

    def delete(self, document_id):
        doc = self.document(document_id)
        statuses = self.call(lambda c: c.documents.statuses())
        if not any(
            s.get("code") == doc.get("status") and s.get("deletable") is True for s in statuses
        ):
            raise ValueError("A Assinafy não permite excluir documentos neste status.")
        self.call(lambda c: c.documents.delete(document_id), write=True)
        self._record(document_id, "deleted")
