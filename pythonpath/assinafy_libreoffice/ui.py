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
                    for key in ("Text", "StringItemList", "SelectedItems")
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
        d = Dialog(self, "Adicionar signatário", 370, 260)
        d.edit("name", "~Nome completo", "", 14, 12, 340)
        d.edit("email", "~E-mail", "", 14, 55, 340)
        d.edit("government", "CPF/CNPJ (obrigatório para certificado)", "", 14, 98, 340)
        d.label("method_label", "~Verificação", 14, 141, 245)
        d.add(
            "ListBox",
            "method",
            14,
            157,
            245,
            20,
            Dropdown=True,
            StringItemList=("Email", "DigitalCertificate"),
            SelectedItems=(0,),
        )
        d.edit("step", "~Etapa", "1", 273, 141, 81)
        d.label("hint", "Mesma etapa: paralelo. Etapas diferentes: sequencial.", 14, 186, 340)
        d.button("add", "Adicionar", 164, 224, kind=1)
        d.button("cancel", "Cancelar", 264, 224, kind=2)
        if d.execute() != 1:
            return None
        values = d.values
        recipient = Recipient(
            values["name"]["Text"].strip(),
            values["email"]["Text"].strip(),
            values["government"]["Text"].strip(),
            values["method"]["StringItemList"][values["method"]["SelectedItems"][0]],
            int(values["step"]["Text"]),
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
                f"{r.step}. {r.full_name} <{r.email}> — {r.verification_method}" for r in recipients
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
                f"  {r.verification_method} {r.government_id}"
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
            f"Enviar convites por e-mail agora?\n\n{summary}\n\n"
            f"Mensagem: {message}\nExpiração: {expires_at or 'sem expiração'}\n"
            f"Créditos estimados: {cost}\nDocumento: {document_id}",
            query=True,
        ):
            return {"status": "prepared", "document_id": document_id}
        assignment = self.busy(
            "Enviando convites", lambda: workflow.send(document_id, recipients, message, expires_at)
        )
        self.box("Convites enviados.\nDocumento: " + document_id)
        return {"status": "sent", "document_id": document_id, "assignment_id": assignment["id"]}

    def history(self):
        workflow = self.get_workflow()
        d = Dialog(self, "Assinafy — documentos", 510, 345)
        search = d.edit("search", "Buscar documentos", "", 14, 12, 370)
        d.label("documents_label", "Documentos do workspace conectado", 14, 59, 480)
        listing = d.add("ListBox", "documents", 14, 78, 482, 175, Dropdown=False)
        rows, page = [], [1]

        def refresh(delta=0, *, reset=False):
            page[0] = 1 if reset else max(1, page[0] + delta)
            query = search.Text
            current_page = page[0]
            result = self.busy(
                "Consultando documentos", lambda: workflow.list_documents(current_page, query)
            )
            rows[:] = result["data"]
            listing.getModel().StringItemList = tuple(
                f"{r.get('name', '')} · {r.get('status', '')} · {r['id']}" for r in rows
            )

        def details():
            selected = listing.getSelectedItemPos()
            if selected < 0:
                raise ValueError("Selecione um documento.")
            self.document_details(rows[selected]["id"])
            refresh()

        d.button("search_button", "Buscar", 394, 27, 102, fn=lambda: refresh(reset=True))
        d.button("previous", "Anterior", 14, 271, fn=lambda: refresh(-1))
        d.button("next", "Próxima", 114, 271, fn=lambda: refresh(1))
        d.button("details", "Acompanhar…", 304, 271, fn=details)
        d.button("close", "Fechar", 404, 271, kind=2)
        d.label(
            "hint",
            "Atualize o status antes de repetir um envio que falhou ou expirou.",
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
            StringItemList=tuple(
                f"{s.get('step', 1)}. {s.get('full_name', '')} "
                f"<{s.get('email', '')}> · " + ("assinado" if s.get("completed") else "pendente")
                for s in signers
            ),
        )

        def refresh():
            nonlocal doc
            doc = self.busy("Atualizando status", lambda: workflow.document(document_id))
            status.getModel().Label = "Status: " + doc.get("status", "") + " · " + document_id
            signers[:] = (doc.get("assignment") or {}).get("signers", [])
            listing.getModel().StringItemList = tuple(
                f"{s.get('step', 1)}. {s.get('full_name', '')} <{s.get('email', '')}> · "
                + ("assinado" if s.get("completed") else "pendente")
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
            estimate = self.busy(
                "Consultando custo",
                lambda: workflow.resend(document_id, signer["id"], estimate_only=True),
            )
            if estimate.get("has_sufficient_resources") is not True:
                raise ValueError("O workspace não tem recursos suficientes para reenviar.")
            if self.box(
                "Reenviar convite para " + signer.get("email", signer["id"]) + "?\n"
                "Créditos estimados: " + str(estimate.get("total_credits", "—")),
                query=True,
            ):
                self.busy("Reenviando convite", lambda: workflow.resend(document_id, signer["id"]))
                refresh()

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
                "Baixe os artefatos necessários primeiro.",
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
            SelectedItems=(0,),
        )
        d.button("download", "Baixar…", 344, 205, 130, fn=download)
        d.button("refresh", "Atualizar", 14, 237, fn=refresh)
        d.button("resume", "Retomar envio…", 114, 237, 120, fn=resume)
        d.button("resend", "Reenviar convite…", 244, 237, 130, fn=resend)
        d.button("delete", "Excluir…", 14, 269, fn=delete)
        d.button("close", "Fechar", 384, 269, kind=2)
        d.label(
            "hint",
            "A assinatura e o uso do certificado A1/A3 acontecem no fluxo seguro "
            "da Assinafy, aberto pelo próprio signatário.",
            14,
            304,
            460,
            38,
        )
        refresh()
        d.execute()

    def run(self, frame, command):
        if self.in_dialog:
            return
        self.frame = frame
        self.in_dialog = True
        try:
            handlers = {
                "Send": self.compose,
                "History": self.history,
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
