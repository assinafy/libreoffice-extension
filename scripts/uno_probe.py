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


def prop(name, value):
    p = uno.createUnoStruct("com.sun.star.beans.PropertyValue")
    p.Name, p.Value = name, value
    return p


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
    assert set(submenu.getElementNames()) == {"Send", "History", "Settings"}
    installed = next(Path(profile).rglob("assinafy_libreoffice/ui.py")).parent.parent
    sys.path.insert(0, str(installed))
    from assinafy_libreoffice.ui import App, Dialog, Recipient, Shown

    app = App(ctx)
    real_execute = Dialog.execute

    def check_history():
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
                buttons["next"]()
                buttons["search_button"]()
            return complete_dialog(dialog)

        app.workflow = SimpleNamespace(
            list_documents=lambda page, search: calls.append((page, search)) or {"data": []}
        )
        Dialog.edit, Dialog.button, Dialog.execute = edit, button, execute
        try:
            app.history()
            assert calls == [(1, "Test query"), (2, "Test query"), (1, "Test query")], calls
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
                app.box = lambda text, query=False, error=False, answers=confirmations: (
                    next(answers) if query else True
                )
                result = app.send_flow(
                    [recipient], doc=doc, document_id="test-document" if resume else None
                )
                assert result["status"] == expected and events == calls, (result, events)
        finally:
            app.workflow = None
            app.preview, app.box = real_preview, real_box

    def complete_dialog(dialog):
        if "name" in dialog.controls:
            dialog.controls["name"].Text = "Test Recipient"
            dialog.controls["email"].Text = "recipient" + "@" + "example.invalid"
        timer = threading.Timer(0.25, lambda: dialog.control.endDialog(1))
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
            for command in ("Send", "History", "Settings"):
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
                    recipient = app.recipient_dialog()
                    assert (
                        isinstance(recipient, Recipient) and recipient.full_name == "Test Recipient"
                    )
                    print("Checking settings", flush=True)
                    app.settings()
                    print("Checking search thread and pagination", flush=True)
                    check_history()
                    fake_doc = {
                        "id": "test-document",
                        "name": "Test PDF",
                        "status": "metadata_ready",
                        "assignment": None,
                    }
                    app.workflow = SimpleNamespace(document=lambda _, value=fake_doc: dict(value))
                    print("Checking document controls", flush=True)
                    app.document_details("test-document")
                    app.workflow = None
                    app.save_tokens({"access_token": "test-session-value"})
                    found = app.vault.findForName(app.token_key(), "oauth", app.interaction)
                    assert (
                        json.loads(found.UserList[0].Passwords[0])["access_token"]
                        == "test-session-value"
                    )
                    app.save_tokens({})
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
