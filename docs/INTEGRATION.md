# LibreOffice integration

The [live API reference](https://api.assinafy.com.br/v1/docs) and its
[OpenAPI document](https://api.assinafy.com.br/v1/docs/openapi.json) define the
wire contract. The extension bundles the published Assinafy Python SDK 2.0.0.
Every SDK resource method includes request and response documentation in its
docstring; the complete SDK reference is in the
[official repository](https://github.com/assinafy/python-sdk/tree/v2.0.0).

## Runtime and boundaries

LibreOffice 26.8+ supplies Python 3.11+ and PyUNO. Keep the 3.11 syntax baseline
for the embedded interpreter. Python has annual stable releases and five years
of support rather than an LTS edition; see the
[Python support table](https://devguide.python.org/versions/).

`assinafy_addon.py` registers three native commands. `ui.py` owns UNO, PDF
export, preview, native confirmations and the password container. `workflow.py`
uses the SDK for account, contact, document and assignment operations. `oauth.py`
implements the public application's PKCE callback and token rotation.

Network operations run in a worker. Document access, credential storage and
dialogs run on the office thread through `App.on_main()`. One office process
owns its profile. Tests use an isolated profile.

## Connection

The production public client and registered HTTPS callback are supplied in
`Config` and embedded in `deployment.json` when packaging. There is no client
secret. The extension requests `documents:read documents:write offline_access`.

1. Discover `https://auth.assinafy.com.br/.well-known/oauth-authorization-server`.
   Check the issuer and authorization, token and revocation endpoints, S256,
   and support for public clients (`none`).
2. Bind an ephemeral IPv4 loopback port. Generate a new verifier and random
   state for each attempt; the state also identifies the loopback port.
3. Open `https://auth.assinafy.com.br/oauth/authorize` with these parameters:

   ```text
   response_type=code
   client_id=PUBLIC_CLIENT_ID
   redirect_uri=REGISTERED_HTTPS_CALLBACK
   scope=documents:read documents:write offline_access
   state=RANDOM_STATE.PORT
   code_challenge=BASE64URL_SHA256_VERIFIER
   code_challenge_method=S256
   resource=https://api.assinafy.com.br
   ```

4. `web/oauth-callback.js`, deployed on the registered HTTPS page, clears the
   browser's query history, validates issuer/state shape and forwards only
   `code`, `state`, `iss` or `error` to `/callback` on `127.0.0.1`. The loopback
   handler checks the exact state, issuer, Host, path, single-valued parameters
   and mutually exclusive code/error before accepting the callback.
5. Exchange the code with a form-encoded `POST /v1/oauth/token`:

   ```text
   grant_type=authorization_code
   client_id=PUBLIC_CLIENT_ID
   resource=https://api.assinafy.com.br
   code=ONE_TIME_CODE
   redirect_uri=REGISTERED_HTTPS_CALLBACK
   code_verifier=VERIFIER
   ```

   The response is plain JSON, without the resource envelope:

   ```json
   {
     "access_token": "ACCESS_TOKEN",
     "token_type": "Bearer",
     "expires_in": 3600,
     "scope": "documents:read documents:write",
     "refresh_token": "REFRESH_TOKEN"
   }
   ```

6. Persist tokens before use. `offline_access` does not appear in the returned
   scope. An ID token is neither requested nor used. A custom LibreOffice
   master password permits persistent storage; otherwise storage is in memory.
7. `accounts.list()` selects the single workspace authorized by the connection.
   Subsequent SDK clients send `Authorization: Bearer ACCESS_TOKEN`.

Refresh posts `grant_type=refresh_token`, `refresh_token`, `client_id` and
`resource` to the same endpoint. Every successful refresh must return a new
refresh token valid for a fresh 30 days. A process-wide lock serializes refresh
and revocation; saved tokens are re-read under the lock. Invalidate the old
stored token before sending it, and save the replacement before using it.
Only DNS, connection, TLS-handshake or proxy-tunnel failures preserve the old
token. A timeout, malformed response, missing replacement or `invalid_grant`
requires reconnection. Token requests never retry automatically.

Disconnect posts `client_id` and `token` to `/v1/oauth/revoke`, then clears
local tokens. HTTPS clients verify certificates and require TLS 1.2 or newer.
Redirects never receive tokens.

## SDK calls used by the extension

Paths below are relative to `https://api.assinafy.com.br/v1`.
Resource JSON responses have the wire shape
`{"status":200,"message":null,"data":VALUE}`. The SDK unwraps `data`;
paginated lists become `{"data":[...],"meta":{...}}` using response headers.
Errors use the same envelope with `data: null` and raise `ApiError`.

| SDK method | Request | SDK return |
| --- | --- | --- |
| `accounts.list()` | `GET /accounts`; no body | List of Account objects; exactly one required for OAuth. |
| `documents.list(params)` | `GET /accounts/{accountId}/documents?page=1&per-page=20&search=TERM&sort=-updated_at`; no body | `{"data":[Document],"meta":{"current_page":1,"per_page":20,"total":1,"last_page":1}}`. |
| `documents.get(id)` | `GET /documents/{id}`; no body | Document, including its assignment and signers when present. |
| `documents.upload(source)` | `POST /accounts/{accountId}/documents`; multipart `file` containing PDF bytes and filename | Document; its ID is saved immediately. |
| `documents.wait_until_ready(id, timeout=90, poll_interval=2)` | Repeated document GET requests | Document when processing finishes; raises on terminal failure or timeout. |
| `assignments.estimate_cost(id, payload)` | `POST /documents/{id}/assignments/estimate-cost`; pricing JSON below | CostEstimate below; creates no invitation. |
| `signers.find_by_email(email)` | Paged `GET /accounts/{accountId}/signers?search=EMAIL&per-page=100&page=1`; no body | Exact case-insensitive Signer match, or `None`. |
| `signers.create(payload)` | `POST /accounts/{accountId}/signers`; contact JSON below | Signer below. |
| `signers.update(id, payload)` | `PUT /accounts/{accountId}/signers/{id}`; only missing `government_id` and/or `whatsapp_phone_number` | Updated Signer. Existing conflicting details stop the send. |
| `assignments.create(id, payload)` | `POST /documents/{id}/assignments`; assignment JSON below | Assignment below; sends invitations and can consume credits. |
| `assignments.estimate_resend_cost(doc, assignment, signer)` | `POST /documents/{doc}/assignments/{assignment}/signers/{signer}/estimate-resend-cost`; no body | CostEstimate. |
| `assignments.resend_notification(doc, assignment, signer)` | `POST /documents/{doc}/assignments/{assignment}/signers/{signer}/resend`; no body | `{"is_sent":true}`. Uses the assignment's existing channel. |
| `documents.download(id, artifact)` | `GET /documents/{id}/download/{artifact}`; no body | Raw bytes: PDF, or ZIP for `bundle`. |
| `documents.statuses()` | `GET /documents/statuses`; no body | List of `{"code":"metadata_ready","deletable":true}`. |
| `documents.delete(id)` | `DELETE /documents/{id}`; no body | Wire `data: []`, SDK `None`; allowed only when the current status is deletable. |

The complete resource schemas, including optional fields, are published under
Account, Document, Assignment, AssignmentSigner, Signer and CostEstimate in
the live OpenAPI document. The extension preserves SDK response objects.

An Account response contains:

```json
{
  "resource": "account",
  "id": "account-id",
  "name": "Example workspace",
  "primary_color": null,
  "secondary_color": null,
  "notification_sender_type": "User",
  "roles": ["owner"],
  "is_delete_allowed": true,
  "created_at": "2026-10-01T12:00:00Z"
}
```

### Contact and document payloads

The upload source passed to the SDK is
`{"buffer": pdf_bytes, "file_name": "contract.pdf"}`; the SDK creates the
multipart body. Uploads are limited to 25 MiB and 2,000 pages. The native PDF
preview checks pages before upload.

Contact creation sends a full name and email, plus the nonempty identity and
phone fields entered by the user:

```json
{
  "full_name": "Example Signer",
  "email": "signer@example.invalid",
  "whatsapp_phone_number": "+15555550100",
  "government_id": "00000000000"
}
```

The address and phone above are examples. The zero CPF demonstrates field
shape only; production identity verification requires a valid CPF/CNPJ.
A Signer response contains:

```json
{
  "resource": "signer",
  "id": "signer-id",
  "full_name": "Example Signer",
  "email": "signer@example.invalid",
  "whatsapp_phone_number": "+15555550100",
  "government_id": "00000000000",
  "has_accepted_terms": false
}
```

A Document response contains:

```json
{
  "resource": "document",
  "id": "document-id",
  "account_id": "account-id",
  "template_id": null,
  "name": "contract.pdf",
  "status": "metadata_ready",
  "artifacts": {"original": "https://api.assinafy.com.br/v1/documents/document-id/download/original"},
  "is_closed": false,
  "signing_url": "https://api.assinafy.com.br/v1/sign/document-id",
  "decline_reason": null,
  "declined_by": null,
  "tags": [],
  "assignment": null,
  "pages": [],
  "created_at": "2026-10-01T12:00:00Z",
  "updated_at": "2026-10-01T12:00:00Z"
}
```

Processing populates `pages`; an assignment populates `assignment`. Downloads
use the SDK's authenticated download endpoint rather than following returned
artifact URLs. Workspace ownership is checked before document operations.

### Verification, pricing and invitations

| Verification | Notification | Additional requirement |
| --- | --- | --- |
| `Email` | `Email` | Email address. |
| `Whatsapp` | `Whatsapp` | E.164 phone and paid plan. |
| `DigitalCertificate` | `Email` or `Whatsapp` | CPF/CNPJ, enabled account feature, signer alone in its step. |
| `IdDocumentFaceMatchBasic` | `Email` or `Whatsapp` | Valid CPF and enabled identity verification feature. |

The extension uses virtual assignments. Steps start at 1 and are contiguous;
signers sharing a step work in parallel. Every signer has exactly one
notification channel. A WhatsApp notification always requires a phone.

Pricing request:

```json
{
  "method": "virtual",
  "signers": [{"verification_method": "Whatsapp", "notification_methods": ["Whatsapp"]}]
}
```

CostEstimate response:

```json
{
  "documents": 1,
  "credits": 0.45,
  "needs_extra_document": false,
  "extra_document_cost": 0,
  "total_credits": 0.45,
  "breakdown": [{"code": "NotificationWhatsapp", "name": "Whatsapp Notification", "quantity": 1, "unit_cost": 0.45, "cost": 0.45}],
  "document_balance": 10,
  "credit_balance": 10,
  "has_sufficient_resources": true,
  "blocking_reason": null,
  "message": null
}
```

Treat the returned balance and breakdown as authoritative. An estimate does
not reserve credits or enable a plan feature. The create endpoint revalidates
both. A false `has_sufficient_resources` stops invitation creation.

Assignment creation sends:

```json
{
  "method": "virtual",
  "signers": [{"id": "signer-id", "step": 1, "verification_method": "Whatsapp", "notification_methods": ["Whatsapp"]}],
  "message": "Please sign the document.",
  "expires_at": "2030-12-31T23:59:00Z"
}
```

`expires_at` is omitted when unset; otherwise it must be an RFC 3339 timestamp
at least one hour in the future. An Assignment response contains:

```json
{
  "resource": "assignment",
  "id": "assignment-id",
  "sender_email": "sender@example.invalid",
  "method": "virtual",
  "expires_at": "2030-12-31T23:59:00Z",
  "message": "Please sign the document.",
  "signers": [{
    "resource": "signer",
    "id": "signer-id",
    "full_name": "Example Signer",
    "email": "signer@example.invalid",
    "whatsapp_phone_number": "+15555550100",
    "government_id": null,
    "has_accepted_terms": false,
    "verification_method": "Whatsapp",
    "notification_methods": ["Whatsapp"],
    "step": 1,
    "notified": true,
    "completed": false,
    "identity_verification_status": null,
    "notification_history": [{
      "event": "signature_request",
      "status": "sent",
      "error_code": null,
      "error_message": null,
      "sent_at": "2026-10-01T12:00:00Z",
      "failed_at": null
    }]
  }],
  "copy_receivers": [],
  "items": [],
  "summary": {"signer_count": 1, "completed_count": 0, "signers": []},
  "signing_urls": [{"signer_id": "signer-id", "url": "https://api.assinafy.com.br/v1/sign/document-id"}]
}
```

Some fields are context-dependent or nullable; the live schemas describe
availability. Failed notification history entries use `status: "failed"`,
`failed_at`, `error_code` and `error_message`.

The owner can inspect delivery history and explicitly resend. Later steps are
notified only after every signer in the previous step finishes. Biometric
`InReview` and document `pending_identity_review` require an account decision
in the Assinafy application. Biometric evidence and A1/A3 keys remain in the
hosted signing flow; the extension never collects them.

## Confirmation and recovery

1. Export and review the exact PDF bytes locally.
2. Confirm workspace, PDF and recipients before upload.
3. Save the returned document ID, wait for metadata, and estimate cost.
4. Confirm recipients, verification, channel, phone, steps, message, expiration
   and credits before creating the assignment. Cancel preserves the upload.
5. After an uncertain send, inspect the document before trying again. Writes
   never retry after network errors. A definite 401 permits one refresh and
   retry, with scopes checked again before the operation.
6. Resume an unassigned upload by reviewing its original PDF and estimating
   again. Never upload a second copy automatically.
7. Download original, certificated, certificate-page, PAdES or bundle to a new
   filename. Existing local files are preserved. Delete requires native user
   confirmation and the API's current `deletable` status.

## Signed webhooks

The desktop extension checks status on demand and does not register an inbound
webhook receiver. It requests no webhook permissions and leaves workspace
webhook settings intact. A server integration can use the bundled SDK's
`webhooks` resource and `WebhookVerifier.verify_delivery()`.

Accounts allow one endpoint, or up to three on paid plans. Use
`list_endpoints`, `create_endpoint`, `get_endpoint`, `update_endpoint` and
`delete_endpoint`; the legacy subscriptions resource addresses only the oldest
endpoint. Creation accepts `url`, `email`, `events`, `name`, `is_active` and
`signing_enabled`. For example:

```json
{
  "url": "https://receiver.example.invalid/assinafy",
  "email": "operator@example.invalid",
  "events": ["document_ready", "signer_signed_document"],
  "name": "Document archive",
  "is_active": true,
  "signing_enabled": true
}
```

The endpoint response contains those fields, `id`, `created_at`, `updated_at`,
and the creation-time `secret` when signing is enabled. Store that secret
securely on the receiving server. OAuth cannot retrieve or rotate it through
the secret endpoints; if lost, recreate the endpoint. API-key integrations
can use `get_endpoint_secret` and `rotate_endpoint_secret`.

Signed deliveries use Standard Webhooks. Verify the unmodified raw body and
the `webhook-id`, `webhook-timestamp`, `webhook-signature` headers with the
endpoint's secret. The signed content is `id.timestamp.body`, using the
base64-decoded key after `whsec_` and HMAC-SHA256; signatures have the
`v1,BASE64` form. The SDK compares in constant time and enforces a five-minute
clock window. The legacy body-only `verify()` does not verify this contract.
Deduplicate by `webhook-id` after verification and before side effects.

The event body has `id`, `event`, `message`, `payload`, `origin`, `created_at`,
`subject`, `object`, `account_id`. Subject/object are polymorphic resources.
Return 2xx after durable acceptance; deliveries have an initial attempt plus
one retry after three seconds. Each endpoint has an independent circuit
breaker after ten failed events. Use dispatch history to diagnose failures.

## Development and packaging

Build from a clean Python 3.11 virtual environment with PyPI dependencies:

```sh
python3.11 -m venv .venv
.venv/bin/python -m pip install -r requirements-oxt.txt
.venv/bin/python -m pip install pytest ruff build
.venv/bin/python -m pytest
.venv/bin/ruff check .
.venv/bin/python scripts/build_oxt.py
docker build -f scripts/Dockerfile.smoke -t assinafy-libreoffice-smoke .
docker run --rm --init -v "$PWD:/workspace:ro" assinafy-libreoffice-smoke
```

`requirements-oxt.txt` pins the SDK and every runtime dependency. Keep the SDK
pin in `pyproject.toml` synchronized. The packager rejects version drift and
path, URL or editable installs. Its explicit allowlist includes application
code, public deployment settings, SDK dependencies and their licenses.

When changing the integration, run the full matching SDK release's tests
against the installed PyPI distribution, with the SDK's `pythonpath` setting
overridden so its source does not replace the installed package. Check the
extension on Python 3.11 and the latest stable Python as well as the real
LibreOffice smoke test. The smoke test covers registration, dispatch, four PDF
exports, native dialogs, signing choices, confirmations and password storage.

Use runtime environment variables for sandbox credentials and authorized test
contacts. Keep profiles, documents, logs and credentials out of packages and
version control. Native user confirmation is required before notifications or
document deletion. `AGENTS.md` and `CLAUDE.md` are local, ignored guidance.
