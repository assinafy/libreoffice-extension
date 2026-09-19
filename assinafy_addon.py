"""UNO protocol registration for the Assinafy extension."""

import unohelper
from com.sun.star.frame import XDispatch, XDispatchProvider
from com.sun.star.lang import XInitialization, XServiceInfo

PROTOCOL = "br.com.assinafy.libreoffice:"
IMPLEMENTATION = "br.com.assinafy.libreoffice.ProtocolHandler"
SERVICE = "com.sun.star.frame.ProtocolHandler"
COMMANDS = {"Send", "History", "Settings"}


def command_of(url):
    complete = getattr(url, "Complete", "") or getattr(url, "Main", "")
    if complete.startswith(PROTOCOL):
        return complete[len(PROTOCOL) :]
    if getattr(url, "Protocol", "") == PROTOCOL:
        return url.Path
    return ""


class ProtocolHandler(unohelper.Base, XDispatchProvider, XDispatch, XInitialization, XServiceInfo):
    def __init__(self, ctx):
        self.ctx = ctx
        self.frame = None

    def initialize(self, args):
        if args:
            self.frame = args[0]

    def queryDispatch(self, url, target_frame_name, search_flags):
        return self if command_of(url) in COMMANDS else None

    def queryDispatches(self, requests):
        return tuple(self.queryDispatch(r.FeatureURL, r.FrameName, r.SearchFlags) for r in requests)

    def dispatch(self, url, args):
        try:
            from assinafy_libreoffice.ui import application

            application(self.ctx).run(self.frame, command_of(url))
        except Exception as exc:
            try:
                from assinafy_libreoffice.ui import application

                application(self.ctx).error(exc)
            except Exception:
                import sys

                print(
                    "Assinafy: extension could not start; run the installation check.",
                    file=sys.stderr,
                )

    def addStatusListener(self, listener, url):
        import uno

        event = uno.createUnoStruct("com.sun.star.frame.FeatureStateEvent")
        event.Source = self
        event.FeatureURL = url
        event.IsEnabled = command_of(url) in COMMANDS
        listener.statusChanged(event)

    def removeStatusListener(self, listener, url):
        pass

    def getImplementationName(self):
        return IMPLEMENTATION

    def supportsService(self, service_name):
        return service_name == SERVICE

    def getSupportedServiceNames(self):
        return (SERVICE,)


g_ImplementationHelper = unohelper.ImplementationHelper()
g_ImplementationHelper.addImplementation(ProtocolHandler, IMPLEMENTATION, (SERVICE,))
