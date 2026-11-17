"""HTTP blueprints for the v2 API.

Every module in this package that exposes a Flask Blueprint named ``bp`` is registered
automatically by ``register_blueprints(app)``. Modules may also expose ``init(core)``,
called once after registration with the live app module (for registering status
extensions or background workers).

Conventions for everything under ``/api/v2``:
- success: HTTP 200 with the payload object;
- failure: HTTP 4xx/5xx with ``{"error": {"code": "snake_case", "message": "Human sentence."}}``.
- Blueprint code reaches app state lazily via ``import app as core`` *inside* handlers.
"""

import importlib
import logging
import pkgutil

log = logging.getLogger(__name__)


def register_blueprints(app):
    """Import every src.web.* module and register its ``bp``. Returns the registered names."""
    registered = []
    for mod_info in sorted(pkgutil.iter_modules(__path__), key=lambda m: m.name):
        if mod_info.name.startswith("_"):
            continue
        try:
            module = importlib.import_module(f"{__name__}.{mod_info.name}")
        except Exception:  # a broken blueprint must not take the whole app down
            log.exception("Failed to import blueprint module %s", mod_info.name)
            continue
        bp = getattr(module, "bp", None)
        if bp is not None:
            app.register_blueprint(bp)
            registered.append(mod_info.name)
        init = getattr(module, "init", None)
        if callable(init):
            try:
                import sys
                init(sys.modules.get("app"))
            except Exception:
                log.exception("init() failed for blueprint module %s", mod_info.name)
    return registered


def api_error(code, message, status=400, **extra):
    """Build the standard v2 error response tuple."""
    from flask import jsonify

    body = {"error": {"code": code, "message": message}}
    if extra:
        body["error"].update(extra)
    return jsonify(body), status
