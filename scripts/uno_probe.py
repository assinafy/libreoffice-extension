"""Invoked by smoke_office with a Python interpreter that can import PyUNO."""

import json
import sys
import threading
import time
import traceback
from pathlib import Path
from types import SimpleNamespace

import uno
import unohelper
from com.sun.star.awt import XCallback
from com.sun.star.task import XInteractionHandler


def prop(name, value):
    p = uno.createUnoStruct("com.sun.star.beans.PropertyValue")
    p.Name, p.Value = name, value
    return p


class MasterPassword(unohelper.Base, XInteractionHandler):
    def handle(self, request):
        # The container takes the 32-hex-digit digest the office dialog derives from the text.
        choice = next(c for c in request.getContinuations() if hasattr(c, "setPassword"))
        choice.setPassword("0123456789abcdef" * 2)
        choice.select()


def check_native(ctx, profile, output):
    smgr = ctx.ServiceManager
    desktop = smgr.createInstanceWithContext("com.sun.star.frame.Desktop", ctx)
    desktop.loadComponentFromURL("private:factory/swriter", "_blank", 0, (prop("Hidden", True),))
    provider = smgr.createInstanceWithContext(
        "com.sun.star.configuration.ConfigurationProvider", ctx
    )
    menus = provider.createInstanceWithArguments(
        "com.sun.star.configuration.ConfigurationAccess",
        (prop("nodepath", "/org.openoffice.Office.Addons/AddonUI/OfficeMenuBar"),),
    )
    assert menus.hasByName("br.com.assinafy.libreoffice.menu")
    submenu = menus.getByName("br.com.assinafy.libreoffice.menu").getByName("Submenu")
    assert set(submenu.getElementNames()) == {"Send", "History", "Contracts", "Settings"}
    installed = next(Path(profile).rglob("assinafy_libreoffice/ui.py")).parent.parent
    sys.path.insert(0, str(installed))
    from assinafy_libreoffice.ui import App, Dialog, Recipient, Shown

    app = App(ctx)
    real_execute = Dialog.execute
    recipient_choice = {"method": 0, "notification": 0}

    def check_history(contracts=False):
        calls, buttons = [], {}
        real_edit, real_button = Dialog.edit, Dialog.button

        def edit(dialog, *args, **kwargs):
            control = real_edit(dialog, *args, **kwargs)
            if args[0] != "search":
                return control
            control.Text = "Test query"

            class Search:
                @property
                def Text(self):
                    assert threading.get_ident() == app.main_thread
                    return control.Text

            return Search()

        def button(dialog, name, *args, **kwargs):
            if name in {"next", "search_button"}:
                buttons[name] = kwargs["fn"]
            return real_button(dialog, name, *args, **kwargs)

        def execute(dialog):
            if "documents" in dialog.controls:
                if contracts:
                    dialog.controls["filter"].getModel().SelectedItems = (2,)
                buttons["next"]()
                buttons["search_button"]()
            return complete_dialog(dialog)

        app.workflow = SimpleNamespace(
            list_documents=lambda page, search: calls.append((page, search)) or {"data": []},
            list_contracts=lambda page, search, status: calls.append((page, search, status))
            or {"data": []},
        )
        Dialog.edit, Dialog.button, Dialog.execute = edit, button, execute
        try:
            app.history(contracts=contracts)
            expected = [(1, "Test query"), (2, "Test query"), (1, "Test query")]
            if contracts:
                expected = [(*entry, status) for entry, status in zip(
                    expected, ("", "active", "active"), strict=True
                )]
            assert calls == expected, calls
        finally:
            app.workflow = None
            Dialog.edit, Dialog.button, Dialog.execute = real_edit, real_button, complete_dialog

    def check_confirmations(doc, original):
        events, reviewed = [], []
        real_preview, real_box = app.preview, app.box
        account = {"id": "test-workspace", "name": "Test workspace"}
        estimate = {"has_sufficient_resources": True, "total_credits": 0}

        def prepare(pdf, *args):
            assert pdf == reviewed[-1], "Upload must use exactly the reviewed PDF"
            events.append("upload")
            return {"document_id": "test-document", "estimate": estimate}

        def send(*args):
            events.append("send")
            return {"id": "test-assignment"}

        app.workflow = SimpleNamespace(
            account_info=lambda: account,
            document=lambda _: {"name": "Test.pdf", "assignment": None},
            download=lambda *_: original,
            estimate=lambda *_: estimate,
            prepare=prepare,
            send=send,
        )
        recipient = Recipient("Test Recipient", "recipient" + "@" + "example.invalid")
        try:
            for review, choices, resume, expected, calls in (
                (False, (), False, "cancelled", []),
                (True, (False,), False, "cancelled", []),
                (True, (True, False), False, "prepared", ["upload"]),
                (True, (True, True), False, "sent", ["upload", "send"]),
                (True, (True,), True, "sent", ["send"]),
            ):
                print("Checking confirmation:", expected, resume, flush=True)
                events.clear()
                confirmations = iter(choices)

                def preview(path, accepted=review):
                    reviewed.append(path.read_bytes())
                    return accepted

                app.preview = preview
                def box(text, query=False, error=False, answers=confirmations):
                    if query and "canais indicados" in text:
                        assert "Notificação:" in text and "Email" in text
                    return next(answers) if query else True

                app.box = box
                result = app.send_flow(
                    [recipient], doc=doc, document_id="test-document" if resume else None
                )
                assert result["status"] == expected and events == calls, (result, events)
        finally:
            app.workflow = None
            app.preview, app.box = real_preview, real_box

    def check_delivery_controls():
        events, messages, buttons = [], [], {}
        real_button, real_box = Dialog.button, app.box
        signer = {
            "id": "test-signer", "full_name": "Test Recipient",
            "email": "recipient" + "@" + "example.invalid",
            "notification_methods": ["Whatsapp"], "whatsapp_phone_number": "+15555550100",
            "identity_verification_status": "InReview",
            "notification_history": [{
                "event": "signature_request", "status": "failed",
                "error_code": "test-delivery-error", "failed_at": "2026-10-01T12:00:00Z",
                "error_message": "Test delivery explanation",
            }],
        }
        doc = {
            "id": "test-document", "name": "Test PDF", "status": "pending_signature",
            "assignment": {"id": "test-assignment", "signers": [signer, {
                "id": "certificate-signer", "verification_method": "DigitalCertificate",
            }]},
        }

        def resend(*args, estimate_only=False, confirmed=None):
            if not estimate_only:
                assert confirmed == signer
            events.append("estimate" if estimate_only else "resend")
            return {"has_sufficient_resources": True, "total_credits": 0.45}

        app.workflow = SimpleNamespace(document=lambda _: doc, resend=resend,
                                       delete=lambda _: events.append("delete"))
        answers = iter((False, True, False, True))

        def box(text, query=False, error=False):
            messages.append(text)
            return next(answers) if query else True

        def button(dialog, name, *args, **kwargs):
            if name in {"notifications", "resend", "delete"}:
                buttons[name] = kwargs["fn"]
            return real_button(dialog, name, *args, **kwargs)

        def execute(dialog):
            if "signers" in dialog.controls:
                def actions():
                    assert dialog.controls["artifact"].getSelectedItemPos() == 3
                    listing = dialog.controls["signers"]
                    assert "InReview" in listing.getModel().StringItemList[0]
                    listing.selectItemPos(0, True)
                    buttons["notifications"]()
                    buttons["resend"]()
                    assert events == ["estimate"], "Declined confirmation must not resend"
                    buttons["resend"]()
                    buttons["delete"]()
                    assert "delete" not in events
                    buttons["delete"]()

                listener = Shown(actions)
                dialog.control.addWindowListener(listener)
                dialog.listeners.append(listener)
            return complete_dialog(dialog)

        Dialog.button, Dialog.execute, app.box = button, execute, box
        try:
            app.document_details("test-document")
            assert events == ["estimate", "estimate", "resend", "delete"]
            assert "failed" in messages[0] and "test-delivery-error" in messages[0]
            assert "Test delivery explanation" in messages[0]
            assert all("Whatsapp" in text for text in messages[:3])
            assert all("+15555550100" in text for text in messages[1:3])
            assert all("notifica todos os signatários" in text for text in messages[3:])
        finally:
            app.workflow = None
            Dialog.button, Dialog.execute, app.box = real_button, complete_dialog, real_box

    def check_contract_forms():
        updates = []
        real_box = app.box
        stamp = "2026-10-23T12:00:00Z"
        root = {
            "resource": "document_contract", "status": "active", "term": 1,
            "value": 100, "currency": "BRL", "auto_renew": True, "updated_at": stamp,
            "metadata": {"preserved": "yes"},
            "external_references": [{"system": "test", "id": "1"}],
        }
        pending = {"resource": "document_contract_amendment", "relation_type": "amendment",
                   "status": "pending", "terms": {"value": 100}, "updated_at": stamp}
        app.workflow = SimpleNamespace(
            update_contract=lambda document, payload: updates.append((document, payload))
        )
        cases = [
            (root, None, {}, {}, True, 1, None),
            (root, None, {"value": ""}, {}, True, 1, {"value": None, "updated_at": stamp}),
            (root, None, {"value": "200"}, {}, False, 1, None),
            (root, None, {"value": "200"}, {}, True, 2, None),
            (None, None, {"counterparty_name": "Example"}, {}, True, 1,
             {"counterparty_name": "Example", "status": "draft"}),
            (pending, None, {}, {"auto_renew": 0}, True, 1,
             {"auto_renew": False, "updated_at": stamp}),
            (pending | {"terms": {"value": 100, "auto_renew": True}}, None, {},
             {"auto_renew": 2}, True, 1, {"auto_renew": None, "updated_at": stamp}),
            (pending, None, {}, {}, True, 1, None),
        ]
        for relation in ("amendment", "renewal", "termination"):
            link = {"parent_document_id": "root-document", "relation_type": relation}
            edits = {"end_date": "2030-12-31"}
            cases.append((None, link, edits, {}, True, 1, link | edits))
        try:
            for current, link, edits, states, confirm, result, expected in cases:
                updates.clear()
                app.box = lambda *_args, accepted=confirm, **_kwargs: accepted

                def execute(dialog, edits=edits, states=states, current=current,
                            link=link, result=result):
                    if "waiting" in dialog.controls:
                        return real_execute(dialog)
                    for name, value in edits.items():
                        dialog.controls[name].Text = value
                    for name, value in states.items():
                        dialog.controls[name].getModel().State = value
                    if current == root:
                        assert "status" not in dialog.controls
                    if link and link["relation_type"] == "termination":
                        assert "value" not in dialog.controls
                        assert "auto_renew" not in dialog.controls
                    return complete_dialog(dialog, result=result)

                Dialog.execute = execute
                app.contract_editor("test-document", current, link=link)
                expected_updates = [] if expected is None else [("test-document", expected)]
                assert updates == expected_updates, updates
        finally:
            Dialog.execute, app.box, app.workflow = complete_dialog, real_box, None

    def check_contract_actions():
        stamp = "2026-10-23T12:00:00Z"
        root = {"resource": "document_contract", "document_id": "test-document", "status": "active",
                "term": 2, "renewal_term_months": 12, "end_date": "2030-12-31", "updated_at": stamp}
        real_button, real_box = Dialog.button, app.box
        try:
            for record in (root, {"resource": "document_contract_amendment", "status": "pending",
                                 "terms": {"value": 100, "auto_renew": False}, "currency": "BRL",
                                 "updated_at": stamp},
                           {"resource": "document_contract_amendment", "status": "applied",
                            "terms": {}, "updated_at": stamp}):
                events, buttons, messages = [], {}, []
                app.workflow = SimpleNamespace(
                    document=lambda _: {"name": "Test contract"},
                    contract=lambda _, record=record: record,
                    related_contract=lambda _: root | {"amendments": []},
                    renew_contract=lambda doc, data, events=events: events.append(("renew", data)),
                    terminate_contract=lambda doc, data, events=events:
                    events.append(("terminate", data)),
                    update_contract=lambda doc, data, events=events:
                    events.append(("update", data)),
                )
                answers = iter((False, True, False, True))

                def box(text, query=False, messages=messages, answers=answers, **_):
                    messages.append(text)
                    return next(answers) if query else True

                app.box = box

                def button(dialog, name, *args, buttons=buttons, **kwargs):
                    if name in {"renew", "terminate", "unlink", "family"}:
                        buttons[name] = kwargs["fn"]
                    return real_button(dialog, name, *args, **kwargs)

                def execute(dialog, record=record, events=events, buttons=buttons):
                    if "reason" in dialog.controls:
                        dialog.controls["reason"].Text = "Test termination"
                    if "terms" in dialog.controls:
                        def actions():
                            amendment = record["resource"] == "document_contract_amendment"
                            if record["status"] == "applied":
                                assert not dialog.controls["edit_terms"].isEnabled()
                                assert not dialog.controls["unlink"].isEnabled()
                            elif amendment:
                                assert "Moeda dos valores: BRL" in dialog.controls["terms"].Text
                                assert "Renovação automática: não" in dialog.controls["terms"].Text
                                buttons["unlink"]()
                                assert not events
                                buttons["unlink"]()
                            else:
                                buttons["family"]()
                                buttons["renew"]()
                                assert not events
                                buttons["renew"]()
                                buttons["terminate"]()
                                assert len(events) == 1
                                buttons["terminate"]()

                        listener = Shown(actions)
                        dialog.control.addWindowListener(listener)
                        dialog.listeners.append(listener)
                    return complete_dialog(dialog)

                Dialog.button, Dialog.execute = button, execute
                app.contract_details("test-document")
                if record is root:
                    assert events == [
                        ("renew", {"term": 2, "updated_at": stamp}),
                        ("terminate", {"reason": "Test termination", "updated_at": stamp}),
                    ], events
                elif record["status"] == "pending":
                    assert events == [("update", {"parent_document_id": None, "updated_at": stamp})]
                else:
                    assert not events
        finally:
            Dialog.button, Dialog.execute = real_button, complete_dialog
            app.box, app.workflow = real_box, None

    def complete_dialog(dialog, result=1):
        if "waiting" in dialog.controls:
            return real_execute(dialog)
        if "name" in dialog.controls:
            dialog.controls["name"].Text = "Test Recipient"
            dialog.controls["email"].Text = "recipient" + "@" + "example.invalid"
            dialog.controls["phone"].Text = "+15555550100"
            dialog.controls["government"].Text = "10000098744"
            dialog.controls["method"].getModel().SelectedItems = (recipient_choice["method"],)
            dialog.controls["notification"].getModel().SelectedItems = (
                recipient_choice["notification"],
            )
        timer = threading.Timer(0.25, lambda: dialog.control.endDialog(result))
        listener = Shown(timer.start)
        dialog.control.addWindowListener(listener)
        dialog.listeners.append(listener)
        try:
            return real_execute(dialog)
        finally:
            timer.cancel()
            if timer.ident:
                timer.join()

    for module, pdf_filter in (
        ("swriter", "writer_pdf_Export"),
        ("scalc", "calc_pdf_Export"),
        ("simpress", "impress_pdf_Export"),
        ("sdraw", "draw_pdf_Export"),
    ):
        doc = desktop.loadComponentFromURL("private:factory/" + module, "_blank", 0, ())
        try:
            if module == "swriter":
                doc.Text.String = (
                    "Assinafy — Documento de teste\nAssinatura eletrônica: ação e revisão."
                )
            elif module == "scalc":
                doc.Sheets.getByIndex(0).getCellRangeByName(
                    "A1"
                ).String = "Assinafy — Planilha de teste"
            else:
                shape = doc.createInstance("com.sun.star.drawing.TextShape")
                size = uno.createUnoStruct("com.sun.star.awt.Size")
                size.Width, size.Height = 16000, 2000
                shape.setSize(size)
                doc.DrawPages.getByIndex(0).add(shape)
                shape.String = "Assinafy — Documento de teste"
            frame = doc.getCurrentController().getFrame()
            for command in ("Send", "History", "Contracts", "Settings"):
                url = uno.createUnoStruct("com.sun.star.util.URL")
                url.Complete = "br.com.assinafy.libreoffice:" + command
                transformer = smgr.createInstanceWithContext(
                    "com.sun.star.util.URLTransformer", ctx
                )
                _, parsed = transformer.parseStrict(url)
                handler = frame.queryDispatch(parsed, "_self", 0)
                assert handler is not None, (module, command)
                if command == "Settings":
                    settings_handler, settings_url = handler, parsed
            path = Path(output) / (module + ".pdf")
            doc.storeToURL(path.as_uri(), (prop("FilterName", pdf_filter),))
            assert path.read_bytes().startswith(b"%PDF-")
            assert not doc.getURL(), "PDF export must not change the source document location"
            app.frame = frame
            print("Checking native callbacks:", module, flush=True)
            assert app.busy("Assinafy — teste", lambda: 42) == 42
            Dialog.execute = complete_dialog
            try:
                print("Checking settings command:", module, flush=True)
                settings_handler.dispatch(settings_url, ())
                print("Checking preview:", module, flush=True)
                assert app.preview(path)
                png = path.with_name("preview-0.png")
                assert png.read_bytes().startswith(b"\x89PNG") and png.stat().st_size > 1000
                png.unlink()
                if module == "swriter":
                    print("Checking recipient controls", flush=True)
                    for method, notification, code, channel in (
                        (0, 0, "Email", "Email"),
                        (1, 0, "Whatsapp", "Whatsapp"),
                        (2, 1, "DigitalCertificate", "Email"),
                        (2, 2, "DigitalCertificate", "Whatsapp"),
                        (3, 1, "IdDocumentFaceMatchBasic", "Email"),
                        (3, 2, "IdDocumentFaceMatchBasic", "Whatsapp"),
                    ):
                        recipient_choice.update(method=method, notification=notification)
                        recipient = app.recipient_dialog()
                        assert isinstance(recipient, Recipient)
                        assert recipient.full_name == "Test Recipient"
                        assert recipient.verification_method == code
                        assert recipient.pricing()["notification_methods"] == [channel]
                        assert recipient.whatsapp_phone_number == "+15555550100"
                    print("Checking settings", flush=True)
                    app.settings()
                    print("Checking search thread and pagination", flush=True)
                    check_history()
                    check_history(contracts=True)
                    print("Checking delivery history, biometric status and resend", flush=True)
                    check_delivery_controls()
                    print("Checking contract forms and lifecycle confirmations", flush=True)
                    check_contract_forms()
                    check_contract_actions()
                    app.save_tokens({"access_token": "test-session-value"})
                    assert app.load_tokens() == {"access_token": "test-session-value"}
                    # A master password set mid-session makes the next save persistent: the
                    # newest tokens must replace the session copy, not sit behind it.
                    app.vault.allowPersistentStoring(True)
                    assert app.vault.changeMasterPassword(MasterPassword())
                    app.save_tokens({"access_token": "test-rotated-value"})
                    assert app.load_tokens() == {"access_token": "test-rotated-value"}
                    app.save_tokens({})
                    assert app.load_tokens() == {}
                    check_confirmations(doc, path.read_bytes())
            finally:
                Dialog.execute = real_execute
            print("PASS:", module, "export, dispatch, PDF preview and native dialogs")
        finally:
            doc.setModified(False)
            doc.close(True)
    return "native checks passed"


def run(profile, output):
    ctx = XSCRIPTCONTEXT.getComponentContext()  # noqa: F821

    class Runner(unohelper.Base, XCallback):
        def notify(self, data):
            result = {}
            try:
                result["result"] = check_native(ctx, profile, output)
            except Exception:
                result["error"] = traceback.format_exc()
            Path(output, "native-result.json").write_text(json.dumps(result), encoding="utf-8")

    callback = ctx.ServiceManager.createInstanceWithContext("com.sun.star.awt.AsyncCallback", ctx)
    callback.addCallback(Runner(), None)
    return "started"


if __name__ == "__main__":
    pipe, profile, output = sys.argv[1:]
    local = uno.getComponentContext()
    resolver = local.ServiceManager.createInstanceWithContext(
        "com.sun.star.bridge.UnoUrlResolver", local
    )
    deadline = time.monotonic() + 45
    while True:
        try:
            ctx = resolver.resolve("uno:pipe,name=" + pipe + ";urp;StarOffice.ComponentContext")
            break
        except Exception:
            if time.monotonic() > deadline:
                raise
            time.sleep(0.2)
    script_path = Path(profile) / "user/Scripts/python/assinafy_native_test.py"
    script_path.parent.mkdir(parents=True, exist_ok=True)
    script_path.write_text(Path(__file__).read_text(), encoding="utf-8")
    factory = ctx.ServiceManager.createInstanceWithContext(
        "com.sun.star.script.provider.MasterScriptProviderFactory", ctx
    )
    provider = factory.createScriptProvider("")
    script = provider.getScript(
        "vnd.sun.star.script:assinafy_native_test.py$run?language=Python&location=user"
    )
    result = script.invoke((profile, output), (), ())
    assert result[0] == "started", result
    report = Path(output, "native-result.json")
    deadline = time.monotonic() + 100
    while not report.exists():
        if time.monotonic() > deadline:
            raise TimeoutError("Native UI checks did not finish")
        time.sleep(0.1)
    result = json.loads(report.read_text())
    assert "error" not in result, result.get("error")
    ctx.ServiceManager.createInstanceWithContext("com.sun.star.frame.Desktop", ctx).terminate()
    print(result["result"])
