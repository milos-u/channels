import datetime
import logging
import sys

from django.apps import apps
from django.conf import settings
from django.contrib.staticfiles.handlers import ASGIStaticFilesHandler
from django.core.management import CommandError
from django.core.management.commands.runserver import Command as RunserverCommand

from channels import __version__
from channels.routing import get_default_application

logger = logging.getLogger("django.channels.server")


VALID_BACKENDS = ("daphne", "uvicorn")


class Command(RunserverCommand):
    protocol = "http"
    # server_cls je nastaveny pri runtime v inner_run podle WS_SERVER_BACKEND;
    # pro WSGI fallback (--noasgi) se vraci k RunserverCommand.server_cls.
    server_cls = None

    def add_arguments(self, parser):
        super().add_arguments(parser)
        parser.add_argument(
            "--noasgi",
            action="store_false",
            dest="use_asgi",
            default=True,
            help="Run the old WSGI-based runserver rather than the ASGI-based one",
        )
        # ---- Daphne-specific args (ignorovany pokud backend=uvicorn) ----
        parser.add_argument(
            "--http_timeout",
            action="store",
            dest="http_timeout",
            type=int,
            default=None,
            help=(
                "(daphne only) Specify the daphne http_timeout interval in "
                "seconds (default: no timeout)"
            ),
        )
        parser.add_argument(
            "--websocket_handshake_timeout",
            action="store",
            dest="websocket_handshake_timeout",
            type=int,
            default=5,
            help=(
                "(daphne only) Specify the daphne websocket_handshake_timeout "
                "interval in seconds (default: 5)"
            ),
        )
        parser.add_argument(
            "--application-close-timeout",
            action="store",
            dest="application_close_timeout",
            type=int,
            default=10,
            help=(
                "(daphne only) Specify the daphne application_close_timeout "
                "interval in seconds (default: 10)"
            ),
        )
        # ---- Uvicorn-specific args (ignorovany pokud backend=daphne) ----
        parser.add_argument(
            "--workers",
            action="store",
            dest="uvicorn_workers",
            type=int,
            default=1,
            help=(
                "(uvicorn only) worker process count. Default 1 — autoreload "
                "spravne funguje jen s 1 workerem."
            ),
        )
        parser.add_argument(
            "--timeout-graceful-shutdown",
            action="store",
            dest="uvicorn_timeout_graceful_shutdown",
            type=int,
            default=5,
            help=(
                "(uvicorn only) graceful shutdown timeout in seconds "
                "(default: 5 for dev runserver -- short so Ctrl+C is snappy "
                "even with open browser WS; production service factory uses "
                "30s for in-flight load cleanup)"
            ),
        )

    def handle(self, *args, **options):
        self.backend = getattr(settings, "WS_SERVER_BACKEND", "daphne")
        if self.backend not in VALID_BACKENDS:
            raise CommandError(
                "settings.WS_SERVER_BACKEND must be one of %r, got %r"
                % (VALID_BACKENDS, self.backend)
            )
        self.http_timeout = options.get("http_timeout", None)
        self.websocket_handshake_timeout = options.get(
            "websocket_handshake_timeout", 5
        )
        self.application_close_timeout = options.get("application_close_timeout", 10)
        self.uvicorn_workers = options.get("uvicorn_workers", 1)
        self.uvicorn_timeout_graceful_shutdown = options.get(
            "uvicorn_timeout_graceful_shutdown", 30
        )
        # Check Channels is installed right
        if options["use_asgi"] and not hasattr(settings, "ASGI_APPLICATION"):
            raise CommandError(
                "You have not set ASGI_APPLICATION, which is needed to run the server."
            )
        # Dispatch upward
        super().handle(*args, **options)

    def inner_run(self, *args, **options):
        # Maybe they want the wsgi one?
        if not options.get("use_asgi", True):
            if hasattr(RunserverCommand, "server_cls"):
                self.server_cls = RunserverCommand.server_cls
            return RunserverCommand.inner_run(self, *args, **options)
        # Run checks
        self.stdout.write("Performing system checks...\n\n")
        self.check(display_num_errors=True)
        self.check_migrations()
        # Print helpful text
        self._print_banner()

        if self.backend == "uvicorn":
            self._run_uvicorn(options)
        else:
            self._run_daphne(options)

    def _print_banner(self):
        quit_command = "CTRL-BREAK" if sys.platform == "win32" else "CONTROL-C"
        now = datetime.datetime.now().strftime("%B %d, %Y - %X")
        self.stdout.write(now)
        self.stdout.write(
            (
                "Django version %(version)s, using settings %(settings)r\n"
                "Starting ASGI/Channels version %(channels_version)s development server"
                " [%(backend)s] at %(protocol)s://%(addr)s:%(port)s/\n"
                "Quit the server with %(quit_command)s.\n"
            )
            % {
                "version": self.get_version(),
                "channels_version": __version__,
                "settings": settings.SETTINGS_MODULE,
                "backend": self.backend,
                "protocol": self.protocol,
                "addr": "[%s]" % self.addr if self._raw_ipv6 else self.addr,
                "port": self.port,
                "quit_command": quit_command,
            }
        )

    def _run_daphne(self, options):
        from daphne.endpoints import build_endpoint_description_strings
        from daphne.server import Server

        logger.debug("Daphne running, listening on %s:%s", self.addr, self.port)
        endpoints = build_endpoint_description_strings(host=self.addr, port=self.port)
        try:
            Server(
                application=self.get_application(options),
                endpoints=endpoints,
                signal_handlers=not options["use_reloader"],
                action_logger=self.log_action,
                http_timeout=self.http_timeout,
                root_path=getattr(settings, "FORCE_SCRIPT_NAME", "") or "",
                websocket_handshake_timeout=self.websocket_handshake_timeout,
                application_close_timeout=self.application_close_timeout,
            ).run()
            logger.debug("Daphne exited")
        except KeyboardInterrupt:
            shutdown_message = options.get("shutdown_message", "")
            if shutdown_message:
                self.stdout.write(shutdown_message)
            return

    def _run_uvicorn(self, options):
        import uvicorn

        logger.debug("Uvicorn running, listening on %s:%s", self.addr, self.port)
        config = uvicorn.Config(
            app=self.get_application(options),
            host=self.addr,
            port=int(self.port),
            workers=self.uvicorn_workers,
            loop="auto",
            ws="auto",
            log_level="info",
            timeout_graceful_shutdown=self.uvicorn_timeout_graceful_shutdown,
            # Channels nepouziva ASGI lifespan protocol — vypneme, jinak
            # Uvicorn loguje warning "ASGI 'lifespan' protocol appears unsupported".
            lifespan="off",
        )
        try:
            uvicorn.Server(config).run()
            logger.debug("Uvicorn exited")
        except KeyboardInterrupt:
            shutdown_message = options.get("shutdown_message", "")
            if shutdown_message:
                self.stdout.write(shutdown_message)
            return

    def get_application(self, options):
        """
        Returns the static files serving application wrapping the default application,
        if static files should be served. Otherwise just returns the default
        handler.
        """
        staticfiles_installed = apps.is_installed("django.contrib.staticfiles")
        use_static_handler = options.get("use_static_handler", staticfiles_installed)
        insecure_serving = options.get("insecure_serving", False)
        if use_static_handler and (settings.DEBUG or insecure_serving):
            return ASGIStaticFilesHandler(get_default_application())
        else:
            return get_default_application()

    def log_action(self, protocol, action, details):
        """
        Logs various different kinds of requests to the console.

        Volano jen z Daphne (action_logger=self.log_action). Uvicorn loguje
        vlastnim access logem (formatovani je jine, sjednoceni je open item).
        """
        # HTTP requests
        if protocol == "http" and action == "complete":
            msg = "HTTP %(method)s %(path)s %(status)s [%(time_taken).2f, %(client)s]"

            # Utilize terminal colors, if available
            if 200 <= details["status"] < 300:
                # Put 2XX first, since it should be the common case
                logger.info(self.style.HTTP_SUCCESS(msg), details)
            elif 100 <= details["status"] < 200:
                logger.info(self.style.HTTP_INFO(msg), details)
            elif details["status"] == 304:
                logger.info(self.style.HTTP_NOT_MODIFIED(msg), details)
            elif 300 <= details["status"] < 400:
                logger.info(self.style.HTTP_REDIRECT(msg), details)
            elif details["status"] == 404:
                logger.warn(self.style.HTTP_NOT_FOUND(msg), details)
            elif 400 <= details["status"] < 500:
                logger.warn(self.style.HTTP_BAD_REQUEST(msg), details)
            else:
                # Any 5XX, or any other response
                logger.error(self.style.HTTP_SERVER_ERROR(msg), details)

        # Websocket requests
        elif protocol == "websocket" and action == "connected":
            logger.info("WebSocket CONNECT %(path)s [%(client)s]", details)
        elif protocol == "websocket" and action == "disconnected":
            logger.info("WebSocket DISCONNECT %(path)s [%(client)s]", details)
        elif protocol == "websocket" and action == "connecting":
            logger.info("WebSocket HANDSHAKING %(path)s [%(client)s]", details)
        elif protocol == "websocket" and action == "rejected":
            logger.info("WebSocket REJECT %(path)s [%(client)s]", details)
