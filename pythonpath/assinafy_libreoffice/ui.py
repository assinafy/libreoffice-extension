"""Native UNO dialogs. Document and credential access stays on the office thread."""

from __future__ import annotations

import json
import re
import tempfile
import threading
from pathlib import Path

import uno
import unohelper
from com.sun.star.awt import XActionListener, XCallback, XWindowListener
from com.sun.star.awt.MessageBoxButtons import BUTTONS_OK, BUTTONS_YES_NO, DEFAULT_BUTTON_NO
from com.sun.star.awt.MessageBoxType import ERRORBOX, INFOBOX, QUERYBOX
from com.sun.star.document.MacroExecMode import NEVER_EXECUTE
from com.sun.star.document.UpdateDocMode import NO_UPDATE

from .config import load_config, write_private
from .contracts import RELATIONS, TERM_FIELDS, parse_term
from .oauth import OAuth, decode_tokens
from .workflow import (
    Recipient,
    Workflow,
    error_message,
    validate_pdf,
    validate_request,
)

FILTERS = {
    "com.sun.star.text.TextDocument": ("writer_pdf_Export", "Writer"),
    "com.sun.star.sheet.SpreadsheetDocument": ("calc_pdf_Export", "Calc"),
    "com.sun.star.presentation.PresentationDocument": ("impress_pdf_Export", "Impress"),
    "com.sun.star.drawing.DrawingDocument": ("draw_pdf_Export", "Draw"),
}


def prop(name, value):
    result = uno.createUnoStruct("com.sun.star.beans.PropertyValue")
    result.Name, result.Value = name, value
    return result


def service(ctx, name):
    return ctx.ServiceManager.createInstanceWithContext("com.sun.star." + name, ctx)


class Action(unohelper.Base, XActionListener):
    def __init__(self, fn):
        self.fn = fn

    def actionPerformed(self, event):
        self.fn()

    def disposing(self, event):
        pass


class Callback(unohelper.Base, XCallback):
    def __init__(self, fn):
        self.fn = fn

    def notify(self, data):
        self.fn()


class Shown(unohelper.Base, XWindowListener):
    def __init__(self, fn):
        self.fn = fn

    def windowShown(self, event):
        if self.fn:
            fn, self.fn = self.fn, None
            fn()

    def windowHidden(self, event):
        pass

    def windowMoved(self, event):
        pass

    def windowResized(self, event):
        pass

    def disposing(self, event):
        pass


class Dialog:
    def __init__(self, app, title, width=420, height=280):
        self.app = app
        self.model = service(app.ctx, "awt.UnoControlDialogModel")
        for key, value in {
            "Title": title,
            "Width": width,
            "Height": height,
            "Moveable": True,
            "Closeable": True,
        }.items():
            self.model.setPropertyValue(key, value)
        self.control = service(app.ctx, "awt.UnoControlDialog")
        self.control.setModel(self.model)
        self.listeners = []
        self.controls = {}
        self.values = {}

    def add(self, kind, name, x, y, w, h, **values):
        model = self.model.createInstance("com.sun.star.awt.UnoControl" + kind + "Model")
        for key, value in {
            "Name": name,
            "PositionX": x,
            "PositionY": y,
            "Width": w,
            "Height": h,
            "TabIndex": len(self.controls),
            **values,
        }.items():
            setattr(model, key, value)
        self.model.insertByName(name, model)
        control = self.control.getControl(name)
        self.controls[name] = control
        return control

    def label(self, name, text, x, y, w, h=14):
        return self.add("FixedText", name, x, y, w, h, Label=text, MultiLine=True)

    def edit(self, name, label, value, x, y, w, h=18, **options):
        self.label(name + "_label", label, x, y, w)
        return self.add("Edit", name, x, y + 15, w, h, Text=value, HelpText=label, **options)

    def button(self, name, text, x, y, w=90, fn=None, kind=0):
        control = self.add("Button", name, x, y, w, 20, Label=text, PushButtonType=kind)
        if fn:

            def safe():
                try:
                    fn()
                except Exception as exc:
                    self.app.error(exc)

            listener = Action(safe)
            self.listeners.append(listener)
            control.addActionListener(listener)
        return control

    def execute(self):
        self.control.createPeer(service(self.app.ctx, "awt.Toolkit"), self.app.parent())
        try:
            result = self.control.execute()
            for name, control in self.controls.items():
                model = control.getModel()
                self.values[name] = {
                    key: model.getPropertyValue(key)
                    for key in ("Text", "StringItemList", "SelectedItems", "State")
                    if model.getPropertySetInfo().hasPropertyByName(key)
                }
            return result
        finally:
            self.control.dispose()


class App:
    def __init__(self, ctx):
        self.ctx = ctx
        self.desktop = service(ctx, "frame.Desktop")
        self.async_callback = service(ctx, "awt.AsyncCallback")
        self.main_thread = threading.get_ident()
        self.pending = set()
        self.frame = None
        settings = service(ctx, "util.PathSubstitution")
        self.directory = (
            Path(uno.fileUrlToSystemPath(settings.substituteVariables("$(user)", True)))
            / "assinafy"
        )
        self.directory.mkdir(mode=0o700, exist_ok=True)
        deployment = Path(__file__).with_name("deployment.json")
        self.config = load_config(deployment)
        self.vault = service(ctx, "task.PasswordContainer")
        self.interaction = service(ctx, "task.InteractionHandler")
        self.workflow = None
        self.in_dialog = False

    def parent(self):
        frame = self.frame or self.desktop.getCurrentFrame()
        return frame.getContainerWindow() if frame else None

    def post(self, fn):
        callback = None

        def invoke():
            try:
                fn()
            finally:
                self.pending.discard(callback)

        callback = Callback(invoke)
        self.pending.add(callback)
        self.async_callback.addCallback(callback, None)

    def on_main(self, fn):
        if threading.get_ident() == self.main_thread:
            return fn()
        done = threading.Event()
        result = {}

        def invoke():
            if done.is_set():
                return
            try:
                result["value"] = fn()
            except Exception as exc:
                result["error"] = exc
            finally:
                done.set()

        self.post(invoke)
        if not done.wait(300):
            done.set()
            raise RuntimeError("O LibreOffice não respondeu a tempo.")
        if "error" in result:
            raise result["error"]
        return result.get("value")

    def box(self, text, query=False, error=False):
        box = service(self.ctx, "awt.Toolkit").createMessageBox(
            self.parent(),
            QUERYBOX if query else ERRORBOX if error else INFOBOX,
            BUTTONS_YES_NO | DEFAULT_BUTTON_NO if query else BUTTONS_OK,
            "Assinafy",
            text,
        )
        try:
            return box.execute() == 2
        finally:
            box.dispose()

    def error(self, exc):
        message = error_message(exc)
        document_id = getattr(exc, "context", {}).get("document_id")
        if document_id and re.fullmatch(r"[A-Za-z0-9_-]+", str(document_id)):
            message += "\n\nDocumento: " + document_id
        self.box(message, error=True)

    def busy(self, title, work):
        dialog = Dialog(self, title, 310, 65)
        dialog.model.Closeable = False
        dialog.label("waiting", "Aguarde. O LibreOffice continua disponível.", 12, 17, 285, 30)
        result = {}

        def finished():
            dialog.control.endExecute()

        def run():
            try:
                result["value"] = work()
            except Exception as exc:
                result["error"] = exc
            self.post(finished)

        listener = Shown(
            lambda: threading.Thread(target=run, daemon=True, name="assinafy-network").start()
        )
        dialog.control.addWindowListener(listener)
        dialog.listeners.append(listener)
        dialog.execute()
        if "error" in result:
            raise result["error"]
        return result.get("value")

    def token_key(self):
        return self.config.issuer + "/libreoffice/" + self.config.client_id

    def save_tokens(self, tokens):
        def save():
            key = self.token_key()
            # Replace, never merge: a record keeping memory and persistent copies reads stale.
            self.vault.remove(key, "oauth")
            self.vault.removePersistent(key, "oauth")
            if not tokens:
                return
            passwords = (json.dumps(tokens),)
            if (
                self.vault.isPersistentStoringAllowed()
                and self.vault.hasMasterPassword()
                and not self.vault.isDefaultMasterPasswordUsed()
            ):
                self.vault.addPersistent(key, "oauth", passwords, self.interaction)
            else:
                self.vault.add(key, "oauth", passwords, self.interaction)

        self.on_main(save)

    def load_tokens(self):
        def load():
            record = self.vault.findForName(self.token_key(), "oauth", self.interaction)
            users = record.UserList
            return decode_tokens(users[0].Passwords[0]) if users and users[0].Passwords else {}

        return self.on_main(load)

    def get_workflow(self):
        if self.workflow is None:
            self.config.validate()
            oauth = OAuth(self.config, self.save_tokens, self.load_tokens(), load=self.load_tokens)
            self.workflow = Workflow(oauth, self.record)
        return self.workflow

    def record(self, document_id, status):
        path = self.directory / "history.json"
        history = json.loads(path.read_text(encoding="utf-8")) if path.exists() else {}
        history[document_id] = {"status": status, "environment": self.config.environment}
        write_private(path, history)

    def open_url(self, url):
        service(self.ctx, "system.SystemShellExecute").execute(url, "", 0)

    def settings(self):
        dialog = Dialog(self, "Assinafy — conexão", 400, 200)
        dialog.label("environment", "Ambiente: " + self.config.environment, 14, 14, 370)
        ready = bool(self.config.client_id and self.config.redirect_uri)
        message = (
            (
                "Conecte sua conta no navegador e escolha o workspace.\n"
                "A extensão não solicita sua senha Assinafy."
            )
            if ready
            else (
                "Esta distribuição aguarda o client_id público da Assinafy.\n"
                "Instale o pacote configurado quando ele estiver disponível."
            )
        )
        dialog.label("intro", message, 14, 40, 370, 40)
        dialog.label(
            "storage",
            "Para manter a conexão após fechar o aplicativo, configure uma senha "
            "mestra em Ferramentas > Opções > Segurança > Senhas. Sem ela, a conexão fica "
            "somente nesta sessão.",
            14,
            90,
            370,
            45,
        )

        def connect():
            workflow = self.get_workflow()
            self.busy(
                "Conectando à Assinafy",
                lambda: workflow.oauth.connect(
                    lambda url: self.on_main(lambda: self.open_url(url))
                ),
            )
            workflow.account = None
            account = self.busy("Selecionando workspace", workflow.account_info)
            self.box("Conectado ao workspace: " + account.get("name", account["id"]))

        def disconnect():
            workflow = self.get_workflow()
            self.busy("Desconectando", workflow.oauth.disconnect)
            workflow.account = None
            self.box("Conexão revogada.")

        dialog.button("connect", "Conectar", 14, 157, fn=connect).setEnable(ready)
        dialog.button("disconnect", "Desconectar", 112, 157, fn=disconnect).setEnable(ready)
        dialog.button("close", "Fechar", 296, 157, kind=2)
        dialog.execute()

    def active_document(self):
        frame = self.frame or self.desktop.getCurrentFrame()
        doc = frame.getController().getModel() if frame and frame.getController() else None
        if doc is not None:
            for name, (pdf_filter, module) in FILTERS.items():
                if doc.supportsService(name):
                    return (
                        doc,
                        pdf_filter,
                        {"title": doc.Title, "module": module, "modified": bool(doc.isModified())},
                    )
        raise ValueError("Abra um documento no Writer, Calc, Impress ou Draw.")

    def recipient_dialog(self):
        d = Dialog(self, "Adicionar signatário", 420, 350)
        d.edit("name", "~Nome completo", "", 14, 12, 392)
        d.edit("email", "~E-mail", "", 14, 55, 392)
        d.edit("phone", "~WhatsApp (E.164, com + e código do país)", "", 14, 98, 392)
        d.edit("government", "CPF/CNPJ (certificado); CPF (biometria)", "", 14, 141, 392)
        methods = (
            ("Email", "E-mail"),
            ("Whatsapp", "WhatsApp"),
            ("DigitalCertificate", "Certificado ICP-Brasil (A1/A3)"),
            ("IdDocumentFaceMatchBasic", "Biometria (documento + face)"),
        )
        d.label("method_label", "~Verificação", 14, 184, 290)
        d.add(
            "ListBox",
            "method",
            14,
            200,
            290,
            20,
            Dropdown=True,
            StringItemList=tuple(label for _, label in methods),
            SelectedItems=(0,),
        )
        d.edit("step", "~Etapa", "1", 320, 184, 86)
        d.label("notification_label", "~Notificação do convite", 14, 231, 392)
        d.add(
            "ListBox", "notification", 14, 247, 290, 20,
            Dropdown=True, StringItemList=("Automática", "E-mail", "WhatsApp"),
            SelectedItems=(0,),
        )
        d.label("hint", "Mesma etapa: paralelo. Certificado: sozinho na etapa.", 14, 279, 392, 25)
        d.button("add", "Adicionar", 216, 314, kind=1)
        d.button("cancel", "Cancelar", 316, 314, kind=2)
        if d.execute() != 1:
            return None
        values = d.values
        recipient = Recipient(
            values["name"]["Text"].strip(),
            values["email"]["Text"].strip(),
            values["government"]["Text"].strip(),
            methods[values["method"]["SelectedItems"][0]][0],
            int(values["step"]["Text"]),
            values["phone"]["Text"].strip(),
            ("", "Email", "Whatsapp")[values["notification"]["SelectedItems"][0]],
        )
        recipient.validate()
        return recipient

    def compose(self, document_id=None):
        d = Dialog(self, "Assinafy — preparar envio", 440, 330)
        if document_id:
            workflow = self.get_workflow()
            existing = self.busy("Consultando documento", lambda: workflow.document(document_id))
            doc, info = None, {"title": existing.get("name", document_id), "module": "Assinafy"}
        else:
            doc, _, info = self.active_document()
        d.label("document", info["module"] + " · " + info["title"], 14, 12, 410, 25)
        d.label("recipients_label", "Signatários", 14, 42, 310)
        listing = d.add("ListBox", "recipients", 14, 60, 310, 100, Dropdown=False)
        recipients = []

        def refresh():
            listing.getModel().StringItemList = tuple(
                f"{r.step}. {r.full_name} <{r.email}> — {r.verification_method} / "
                f"{r.pricing()['notification_methods'][0]}" for r in recipients
            )

        def add():
            recipient = self.recipient_dialog()
            if recipient:
                recipients.append(recipient)
                refresh()

        def remove():
            selected = listing.getSelectedItemPos()
            if selected >= 0:
                recipients.pop(selected)
                refresh()

        d.button("add", "Adicionar…", 334, 60, fn=add)
        d.button("remove", "Remover", 334, 87, fn=remove)
        d.edit("message", "Mensagem do convite", "", 14, 172, 410, 42, MultiLine=True)
        d.edit(
            "expires", "Expiração ISO 8601 (opcional; ex.: 2030-12-31T23:59:00Z)", "", 14, 238, 410
        )
        d.button("continue", "Revisar PDF…", 222, 293, 102, kind=1)
        d.button("cancel", "Cancelar", 334, 293, kind=2)
        if d.execute() == 1:
            return self.send_flow(
                recipients,
                d.values["message"]["Text"],
                d.values["expires"]["Text"].strip() or None,
                doc=doc,
                document_id=document_id,
            )
        return {"status": "cancelled"}

    def preview(self, path):
        pdf = self.desktop.loadComponentFromURL(
            path.as_uri(),
            "_blank",
            0,
            (
                prop("Hidden", True),
                prop("ReadOnly", True),
                prop("FilterName", "draw_pdf_import"),
                prop("MacroExecutionMode", NEVER_EXECUTE),
                prop("UpdateDocMode", NO_UPDATE),
            ),
        )
        if pdf is None:
            raise ValueError("O visualizador de PDF do LibreOffice não está disponível.")
        try:
            pages = pdf.getDrawPages()
            count = pages.getCount()
            if not 0 < count <= 2000:
                raise ValueError("O documento deve ter entre 1 e 2000 páginas.")
            d = Dialog(self, "Assinafy — revisar o PDF exportado", 470, 405)
            image = d.add(
                "ImageControl",
                "page",
                12,
                12,
                446,
                337,
                ScaleImage=True,
                ScaleMode=1,
                Border=1,
                HelpText="Página do PDF que será enviado",
            )
            label = d.label("page_number", "", 114, 358, 120)
            index = [0]

            def show_page(delta=0):
                index[0] = max(0, min(count - 1, index[0] + delta))
                page = pages.getByIndex(index[0])
                png = path.with_name("preview-" + str(index[0]) + ".png")
                if not png.exists():
                    exporter = service(self.ctx, "drawing.GraphicExportFilter")
                    exporter.setSourceDocument(page)
                    width, height = page.Width, page.Height
                    success = exporter.filter(
                        (
                            prop("URL", png.as_uri()),
                            prop("MediaType", "image/png"),
                            prop(
                                "FilterData",
                                uno.Any(
                                    "[]com.sun.star.beans.PropertyValue",
                                    (
                                        prop("PixelWidth", int(1000 * width / max(width, height))),
                                        prop(
                                            "PixelHeight", int(1000 * height / max(width, height))
                                        ),
                                    ),
                                ),
                            ),
                        )
                    )
                    if not success or not png.exists():
                        raise ValueError("Não foi possível renderizar a página do PDF.")
                image.getModel().ImageURL = png.as_uri()
                label.getModel().Label = f"Página {index[0] + 1} de {count}"

            d.button("previous", "Anterior", 12, 355, fn=lambda: show_page(-1))
            d.button("next", "Próxima", 240, 355, fn=lambda: show_page(1))
            d.button("accept", "PDF revisado", 350, 355, 108, kind=1)
            d.label("local", "Confira todas as páginas antes de continuar.", 12, 386, 446)
            show_page()
            return d.execute() == 1
        finally:
            pdf.close(True)

    def send_flow(self, recipients, message="", expires_at=None, doc=None, document_id=None):
        validate_request(recipients, message, expires_at)
        workflow = self.get_workflow()
        account = self.busy("Consultando workspace", workflow.account_info)
        if document_id:
            existing = self.busy("Consultando documento", lambda: workflow.document(document_id))
            if existing.get("assignment"):
                raise ValueError("Este documento já tem um envio. Consulte o status.")
            info = {"title": existing.get("name", document_id)}
        elif doc is None:
            doc, pdf_filter, info = self.active_document()
        else:
            pdf_filter, module = next(v for k, v in FILTERS.items() if doc.supportsService(k))
            info = {"title": doc.Title, "module": module}
        filename = re.sub(r"[\\/\r\n]", "-", Path(info["title"]).stem)[:240] + ".pdf"
        with tempfile.TemporaryDirectory(prefix="assinafy-preview-") as temporary:
            path = Path(temporary) / filename
            if document_id:
                raw = self.busy(
                    "Baixando PDF original", lambda: workflow.download(document_id, "original")
                )
                path.write_bytes(raw)
            else:
                doc.storeToURL(
                    path.as_uri(), (prop("FilterName", pdf_filter), prop("Overwrite", False))
                )
            pdf = path.read_bytes()
            validate_pdf(pdf)
            if not self.preview(path):
                return {"status": "cancelled"}
            summary = "\n".join(
                f"Etapa {r.step}: {r.full_name} <{r.email}>\n"
                f"  {r.verification_method} {r.government_id}\n"
                f"  Notificação: {r.pricing()['notification_methods'][0]} "
                f"{r.whatsapp_phone_number}"
                for r in recipients
            )
            if not document_id and not self.box(
                f"Workspace: {account.get('name', account['id'])}\nArquivo: {filename}\n\n"
                f"{summary}\n\nEnviar este PDF à Assinafy para preparar o pedido e "
                "consultar o custo? Os convites serão confirmados na próxima etapa.",
                query=True,
            ):
                return {"status": "cancelled"}
            if document_id:
                estimate = self.busy(
                    "Consultando custo", lambda: workflow.estimate(document_id, recipients)
                )
                prepared = {"document_id": document_id, "estimate": estimate}
            else:
                prepared = self.busy(
                    "Preparando documento",
                    lambda: workflow.prepare(pdf, filename, recipients, message, expires_at),
                )
        document_id = prepared["document_id"]
        estimate = prepared["estimate"]
        if estimate.get("has_sufficient_resources") is not True:
            self.box(
                "O workspace não tem recursos suficientes para este envio.\n"
                "Documento preservado: " + document_id
            )
            return {"status": "prepared", "document_id": document_id}
        cost = estimate.get("total_credits", estimate.get("credits", "—"))
        if not self.box(
            f"Enviar os convites pelos canais indicados agora?\n\n{summary}\n\n"
            f"Mensagem: {message}\nExpiração: {expires_at or 'sem expiração'}\n"
            f"Créditos estimados: {cost}\nDocumento: {document_id}",
            query=True,
        ):
            return {"status": "prepared", "document_id": document_id}
        assignment = self.busy(
            "Enviando convites", lambda: workflow.send(
                document_id, recipients, message, expires_at,
                max_total_credits=estimate["total_credits"],
            )
        )
        self.box("Convites enviados.\nDocumento: " + document_id)
        return {"status": "sent", "document_id": document_id, "assignment_id": assignment["id"]}

    def history(self, *, contracts=False):
        workflow = self.get_workflow()
        d = Dialog(self, "Assinafy — contratos" if contracts else "Assinafy — documentos", 510, 345)
        search = d.edit("search", "Buscar documentos", "", 14, 12, 270 if contracts else 370)
        statuses = ("", "draft", "active", "expired", "terminated")
        if contracts:
            d.label("filter_label", "Situação do contrato", 294, 12, 100)
            status_filter = d.add(
                "ListBox", "filter", 294, 27, 90, 20, Dropdown=True,
                StringItemList=("Todos", "Rascunho", "Ativo", "Expirado", "Encerrado"),
                SelectedItems=(0,),
            )
        d.label("documents_label", "Documentos do workspace conectado", 14, 59, 480)
        listing = d.add("ListBox", "documents", 14, 78, 482, 175, Dropdown=False)
        rows, page = [], [1]

        def refresh(delta=0, *, reset=False):
            page[0] = 1 if reset else max(1, page[0] + delta)
            query = search.Text
            current_page = page[0]
            status = (
                statuses[(status_filter.getModel().SelectedItems or (0,))[0]] if contracts else ""
            )
            result = self.busy(
                "Consultando contratos" if contracts else "Consultando documentos",
                lambda: workflow.list_contracts(current_page, query, status) if contracts
                else workflow.list_documents(current_page, query),
            )
            rows[:] = result["data"]
            listing.getModel().StringItemList = tuple(
                (
                    f"{(r.get('document') or {}).get('name', r['document_id'])} · "
                    f"{r.get('status', '')} · {r.get('end_date') or 'sem término'}"
                    if contracts else f"{r.get('name', '')} · {r.get('status', '')} · {r['id']}"
                ) for r in rows
            )

        def details():
            selected = listing.getSelectedItemPos()
            if selected < 0:
                raise ValueError("Selecione um documento.")
            if contracts:
                self.contract_details(rows[selected]["document_id"])
            else:
                self.document_details(rows[selected]["id"])
            refresh()

        d.button("search_button", "Buscar", 394, 27, 102, fn=lambda: refresh(reset=True))
        d.button("previous", "Anterior", 14, 271, fn=lambda: refresh(-1))
        d.button("next", "Próxima", 114, 271, fn=lambda: refresh(1))
        d.button("details", "Acompanhar…", 304, 271, fn=details)
        d.button("close", "Fechar", 404, 271, kind=2)
        d.label(
            "hint",
            "Para cadastrar termos, abra um documento em Envios recentes > Acompanhar > Contrato."
            if contracts else "Atualize o status antes de repetir um envio que falhou ou expirou.",
            14,
            310,
            482,
        )
        refresh()
        d.execute()

    def document_details(self, document_id):
        workflow = self.get_workflow()
        doc = self.busy("Atualizando status", lambda: workflow.document(document_id))
        d = Dialog(self, "Assinafy — acompanhar", 490, 355)
        d.label("document", doc.get("name", document_id), 14, 12, 460)
        status = d.label("status", "", 14, 34, 460, 28)
        signers = (doc.get("assignment") or {}).get("signers", [])
        listing = d.add(
            "ListBox",
            "signers",
            14,
            68,
            460,
            128,
            Dropdown=False,
        )

        def refresh():
            nonlocal doc
            doc = self.busy("Atualizando status", lambda: workflow.document(document_id))
            status.getModel().Label = "Status: " + doc.get("status", "") + " · " + document_id
            signers[:] = (doc.get("assignment") or {}).get("signers", [])
            listing.getModel().StringItemList = tuple(
                f"{s.get('step', 1)}. {s.get('full_name', '')} <{s.get('email', '')}> · "
                + ("assinado" if s.get("completed") else "pendente")
                + " · " + str(s.get("identity_verification_status") or "")
                for s in signers
            )
            d.controls["resume"].setEnable(not bool(doc.get("assignment")))

        def resume():
            self.compose(document_id)
            refresh()

        def resend():
            selected = listing.getSelectedItemPos()
            if selected < 0:
                raise ValueError("Selecione um signatário.")
            signer = signers[selected]
            signer_id = signer["id"]
            current = self.busy("Atualizando destinatário", lambda: workflow.document(document_id))
            signer = next(
                (s for s in (current.get("assignment") or {}).get("signers", [])
                 if s.get("id") == signer_id), None,
            )
            if signer is None:
                raise ValueError("O signatário não pertence mais a este envio.")
            estimate = self.busy(
                "Consultando custo",
                lambda: workflow.resend(document_id, signer["id"], estimate_only=True),
            )
            if estimate.get("has_sufficient_credits") is not True:
                raise ValueError("O workspace não tem recursos suficientes para reenviar.")
            if self.box(
                "Reenviar convite para " + (signer.get("email") or signer["id"]) + "?\n"
                "Canais: " + ", ".join(signer.get("notification_methods") or ["não informado"])
                + "\n"
                "WhatsApp: " + (signer.get("whatsapp_phone_number") or "—") + "\n"
                "Créditos estimados: " + str(estimate.get("total", "—")),
                query=True,
            ):
                self.busy(
                    "Reenviando convite",
                    lambda: workflow.resend(document_id, signer_id, confirmed=signer),
                )
                refresh()

        def notifications():
            selected = listing.getSelectedItemPos()
            if selected < 0:
                raise ValueError("Selecione um signatário.")
            signer = signers[selected]
            history = signer.get("notification_history") or []
            self.box(
                "Notificações: " + signer.get("full_name", "") + "\n"
                + "Canais: " + ", ".join(signer.get("notification_methods") or ["não informado"])
                + "\n\n" + ("\n".join(
                    f"{entry.get('event', '')}: {entry.get('status', '')} · "
                    f"{entry.get('sent_at') or entry.get('failed_at') or '—'} · "
                    f"{entry.get('error_code') or ''} · {entry.get('error_message') or ''}"
                    for entry in history
                ) or "Nenhuma notificação registrada. Etapas futuras aguardam a anterior.")
            )

        def download():
            artifact = artifacts[selection.getSelectedItemPos()][0]
            suffix = ".zip" if artifact == "bundle" else ".pdf"
            picker = service(self.ctx, "ui.dialogs.FilePicker")
            picker.initialize((2,))
            picker.setTitle("Salvar documento Assinafy")
            picker.setDefaultName(
                Path(doc.get("name", "documento.pdf")).stem + "-" + artifact + suffix
            )
            picker.appendFilter(suffix[1:].upper(), "*" + suffix)
            try:
                if picker.execute() != 1:
                    return
                target = Path(uno.fileUrlToSystemPath(picker.getFiles()[0]))
            finally:
                picker.dispose()
            raw = self.busy("Baixando documento", lambda: workflow.download(document_id, artifact))
            if target.exists():
                raise ValueError("Escolha outro nome. O arquivo existente foi preservado.")
            with target.open("xb") as handle:
                handle.write(raw)
            self.box("Documento salvo em " + str(target))

        def delete():
            if self.box(
                "Excluir este documento da Assinafy?\n\n"
                + doc.get("name", document_id)
                + "\n\nEsta ação exclui o documento; não preserva uma cópia assinada. "
                "Baixe os artefatos necessários primeiro."
                "\nSe o documento estiver em revisão de identidade, a exclusão cancela "
                "a revisão e notifica todos os signatários.",
                query=True,
            ):
                self.busy("Excluindo documento", lambda: workflow.delete(document_id))
                d.control.endExecute()

        artifacts = (
            ("certificated", "PDF certificado"),
            ("original", "PDF original"),
            ("certificate-page", "Página de certificação"),
            ("pades", "PDF PAdES (ICP-Brasil)"),
            ("bundle", "Todos os artefatos (ZIP)"),
        )
        selection = d.add(
            "ListBox",
            "artifact",
            14,
            205,
            320,
            20,
            Dropdown=True,
            StringItemList=tuple(label for _, label in artifacts),
            SelectedItems=(3 if any(
                s.get("verification_method") == "DigitalCertificate" for s in signers
            ) else 0,),
        )
        d.button("download", "Baixar…", 344, 205, 130, fn=download)
        d.button("refresh", "Atualizar", 14, 237, fn=refresh)
        d.button("resume", "Retomar envio…", 114, 237, 120, fn=resume)
        d.button("resend", "Reenviar convite…", 244, 237, 130, fn=resend)
        d.button("delete", "Excluir…", 14, 269, fn=delete)
        d.button("notifications", "Notificações…", 114, 269, 120, fn=notifications)
        d.button("contract", "Contrato…", 244, 269, 130,
                 fn=lambda: self.contract_details(document_id))
        d.button("close", "Fechar", 384, 269, kind=2)
        d.label(
            "hint",
            "Certificado A1/A3 e biometria acontecem no fluxo da Assinafy. "
            "InReview/pending_identity_review: revise a identidade no painel Assinafy.",
            14,
            304,
            460,
            38,
        )
        refresh()
        d.execute()

    def contract_details(self, document_id):
        workflow = self.get_workflow()
        doc = self.busy("Consultando documento", lambda: workflow.document(document_id))
        current = None
        d = Dialog(self, "Assinafy — gestão do contrato", 520, 340)
        d.label("document", doc.get("name", document_id) + " · " + document_id, 14, 12, 492, 25)
        details = d.add(
            "Edit", "terms", 14, 42, 492, 190, MultiLine=True, ReadOnly=True, VScroll=True,
            HelpText="Termos do contrato",
        )

        def refresh():
            nonlocal current
            current = self.busy("Consultando contrato", lambda: workflow.contract(document_id))
            amendment = (current or {}).get("resource") == "document_contract_amendment"
            terms = (current.get("terms") or {}) if amendment else (current or {})
            lines = ["Situação: " + str((current or {}).get("status", "sem termos cadastrados"))]
            if amendment:
                lines += [
                    "Relação: " + current.get("relation_type", ""),
                    "Contrato principal: " + current.get("parent_document_id", ""),
                    "Documento: " + current.get("document_status", ""),
                    "Aplicação: " + (current.get("apply_error") or "sem impedimento informado"),
                    "Moeda dos valores: " + (current.get("currency") or "—"),
                    "Renovação automática: " + (
                        "sem alteração" if "auto_renew" not in terms
                        else "sim" if terms["auto_renew"] else "não"
                    ),
                ]
            lines += [f"{label}: {terms.get(key) if terms.get(key) is not None else '—'}"
                      for key, label, _ in TERM_FIELDS]
            if not amendment:
                lines += [
                    "Renovação automática: " + ("sim" if terms.get("auto_renew") else "não"),
                    "Período: " + str(terms.get("term", "—")),
                ]
            details.Text = "\n".join(lines)
            d.controls["edit_terms"].setEnable(
                not (amendment and current.get("status") == "applied")
            )
            d.controls["link"].setEnable(current is None)
            d.controls["unlink"].setEnable(amendment and current.get("status") == "pending")
            d.controls["family"].setEnable(current is not None)
            d.controls["renew"].setEnable(
                bool(current) and not amendment and current.get("status") != "terminated"
            )
            d.controls["terminate"].setEnable(
                bool(current) and not amendment and current.get("status") != "terminated"
            )

        def edit():
            self.contract_editor(document_id, current)
            refresh()

        def link():
            form = Dialog(self, "Vincular instrumento ao contrato", 430, 155)
            form.edit("parent", "ID do documento do contrato principal", "", 14, 12, 402)
            form.label("relation_label", "Tipo de instrumento", 14, 57, 240)
            form.add("ListBox", "relation", 14, 74, 240, 20, Dropdown=True,
                     StringItemList=tuple(label for _, label in RELATIONS), SelectedItems=(0,))
            form.button("continue", "Continuar…", 226, 116, kind=1)
            form.button("cancel", "Cancelar", 326, 116, kind=2)
            if form.execute() == 1:
                parent = form.values["parent"]["Text"].strip()
                if not parent or parent == document_id:
                    raise ValueError("Informe outro documento como contrato principal.")
                relation = RELATIONS[form.values["relation"]["SelectedItems"][0]][0]
                self.contract_editor(document_id, None, link={
                    "parent_document_id": parent, "relation_type": relation,
                })
                refresh()

        def unlink():
            if self.box("Desvincular este instrumento pendente do contrato principal?", query=True):
                payload = {"parent_document_id": None, "updated_at": current["updated_at"]}
                self.busy("Desvinculando instrumento",
                          lambda: workflow.update_contract(document_id, payload))
                refresh()

        def family():
            root = self.busy(
                "Consultando instrumentos", lambda: workflow.related_contract(document_id)
            )
            self.box("Contrato principal: " + root["document_id"] + "\n\n" + (
                "\n\n".join(
                    f"{a.get('document_name', a['document_id'])} · {a['document_id']}\n"
                    f"{a.get('relation_type', '')} · {a.get('status', '')} · "
                    f"{a.get('document_status', '')}\n{a.get('apply_error') or ''}"
                    for a in root.get("amendments", [])
                ) or "Nenhum instrumento vinculado."
            ))

        def renew():
            payload = {"term": current["term"], "updated_at": current["updated_at"]}
            if self.box(
                f"Renovar o período {current['term']} por "
                f"{current.get('renewal_term_months') or 12} meses?\n"
                f"Término atual: {current.get('end_date') or 'não definido'}\n"
                "O prazo avança até ficar no futuro. O valor permanece igual.", query=True,
            ):
                self.busy(
                    "Renovando contrato", lambda: workflow.renew_contract(document_id, payload)
                )
                refresh()

        def terminate():
            form = Dialog(self, "Encerrar contrato", 430, 170)
            form.edit("reason", "Motivo (opcional, até 500 caracteres)", "", 14, 12, 402,
                      60, MultiLine=True, MaxTextLen=500)
            form.label("hint", "Encerra o acompanhamento; o documento e as assinaturas permanecem.",
                       14, 96, 402, 28)
            form.button("continue", "Continuar…", 226, 136, kind=1)
            form.button("cancel", "Cancelar", 326, 136, kind=2)
            if form.execute() == 1:
                payload = {"reason": form.values["reason"]["Text"].strip(),
                           "updated_at": current["updated_at"]}
                if self.box("Encerrar este contrato?\n\nMotivo: " + payload["reason"], query=True):
                    self.busy("Encerrando contrato",
                              lambda: workflow.terminate_contract(document_id, payload))
                    refresh()

        d.button("edit_terms", "Editar termos…", 14, 246, 110, fn=edit)
        d.button("link", "Vincular…", 134, 246, fn=link)
        d.button("family", "Instrumentos…", 234, 246, 110, fn=family)
        d.button("unlink", "Desvincular…", 354, 246, 110, fn=unlink)
        d.button("renew", "Renovar…", 14, 278, 110, fn=renew)
        d.button("terminate", "Encerrar…", 134, 278, fn=terminate)
        d.button("refresh", "Atualizar", 234, 278, 110, fn=refresh)
        d.button("close", "Fechar", 416, 310, kind=2)
        d.label("hint", "Alterações exigem um plano com gestão de contratos.", 14, 313, 390)
        refresh()
        d.execute()

    def contract_editor(self, document_id, current, *, link=None):
        workflow = self.get_workflow()
        amendment = bool(link) or (current or {}).get("resource") == "document_contract_amendment"
        original = ((current or {}).get("terms") or {}) if amendment else (current or {})
        relation = (link or current or {}).get("relation_type")
        fields = TERM_FIELDS
        if amendment and relation in {"renewal", "termination"}:
            allowed = {"end_date", "value"} if relation == "renewal" else {"end_date"}
            fields = tuple(field for field in fields if field[0] in allowed)
        d = Dialog(self, "Assinafy — termos do instrumento" if amendment else "Assinafy — termos",
                   500, 335)
        for index, (key, label, _) in enumerate(fields):
            d.edit(key, label, str(original[key]) if original.get(key) is not None else "",
                   14 + index % 2 * 245, 12 + index // 2 * 45, 226)
        show_auto = not amendment or relation == "amendment"
        if show_auto:
            d.add("CheckBox", "auto_renew", 14, 198, 300, 18,
                  Label="Renovar automaticamente" + (
                      " (indefinido: sem alteração)" if amendment else ""
                  ),
                  TriState=amendment,
                  State=2 if amendment and "auto_renew" not in original
                  else int(bool(original.get("auto_renew"))))
        statuses = ("draft", "active", "expired", "terminated") if current is None else (
            "draft", "active",
        )
        show_status = not amendment and (current is None or original.get("status") == "draft")
        if show_status:
            d.label("status_label", "Situação", 14, 224, 160)
            d.add("ListBox", "status", 14, 239, 226, 20, Dropdown=True,
                  StringItemList=tuple({"draft": "Rascunho", "active": "Ativo",
                                        "expired": "Expirado", "terminated": "Encerrado"}[s]
                                       for s in statuses),
                  SelectedItems=(statuses.index(original.get("status", "draft")),))
        d.label("hint", "Deixar um campo vazio remove seu valor. No instrumento pendente, "
                "remove apenas essa alteração; os termos do contrato principal permanecem.",
                14, 266, 472, 28)
        d.button("save", "Revisar…", 296, 306, kind=1)
        d.button("cancel", "Cancelar", 396, 306, kind=2)
        if d.execute() != 1:
            return
        payload = dict(link or {})
        for key, _, kind in fields:
            value = parse_term(d.values[key]["Text"], kind)
            if value != original.get(key):
                payload[key] = value
        if show_auto:
            state = d.values["auto_renew"]["State"]
            value = None if state == 2 else bool(state)
            previous = original.get("auto_renew") if amendment else bool(original.get("auto_renew"))
            if value != previous:
                payload["auto_renew"] = value
        if show_status:
            status = statuses[d.values["status"]["SelectedItems"][0]]
            if status != original.get("status"):
                payload["status"] = status
        if not payload:
            return
        merged = {**original, **payload}
        if merged.get("start_date") and merged.get("end_date"):
            if merged["end_date"] < merged["start_date"]:
                raise ValueError("O término não pode ser anterior ao início.")
        labels = {key: label for key, label, _ in TERM_FIELDS} | {
            "auto_renew": "Renovação automática", "status": "Situação",
            "parent_document_id": "Contrato principal", "relation_type": "Instrumento",
        }
        summary = "\n".join(f"{labels[key]}: {value if value is not None else 'remover'}"
                            for key, value in payload.items())
        if current:
            payload["updated_at"] = current["updated_at"]
        if self.box("Salvar estas alterações?\n\n" + summary, query=True):
            self.busy("Salvando termos", lambda: workflow.update_contract(document_id, payload))

    def run(self, frame, command):
        if self.in_dialog:
            return
        self.frame = frame
        self.in_dialog = True
        try:
            handlers = {
                "Send": self.compose,
                "History": self.history,
                "Contracts": lambda: self.history(contracts=True),
                "Settings": self.settings,
            }
            if command not in handlers:
                raise ValueError("Comando desconhecido.")
            return handlers[command]()
        finally:
            self.in_dialog = False


_app = None


def application(ctx):
    global _app
    if _app is None:
        _app = App(ctx)
    return _app
