import os

# We import daphne.server here to install Twisted's AsyncioSelectorReactor
# very early, before anything else accidentally imports twisted.internet.reactor
# (e.g. raven does this) with a different (incompatible) reactor.
#
# TLP patch: when running under uvicorn we skip this import to avoid keeping
# the Twisted reactor (~20-30 MB) and AsyncioSelectorReactor wiring in memory
# unnecessarily. Uvicorn does not use Twisted and chooses its own event loop
# (ProactorEventLoop for single-worker on Windows), so this import is dead
# weight in uvicorn deployments.
#
# Opt-in via env var USE_UVICORN=1, set by uvicorn launchers
# (tlp/management/win32_svc.py::uvicorn_service_factory, start_uvicorn.bat).
# Daphne deployments do not set the var, so the original early-import behavior
# is preserved exactly as upstream channels intends.
if not os.environ.get("USE_UVICORN"):
    import daphne.server
    assert daphne.server

from django.apps import AppConfig  # pyflakes doesn't support ignores


class ChannelsConfig(AppConfig):

    name = "channels"
    verbose_name = "Channels"

    def ready(self):
        # Do django monkeypatches
        from .hacks import monkeypatch_django

        monkeypatch_django()
