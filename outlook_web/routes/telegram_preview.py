from __future__ import annotations

from flask import Blueprint, make_response, render_template

from outlook_web.services.telegram_preview import load_telegram_preview


def create_blueprint() -> Blueprint:
    bp = Blueprint("telegram_preview", __name__)

    @bp.get("/telegram/preview/<token>")
    def preview(token: str):
        email = load_telegram_preview(token)
        # No login/session dependency: possession of this scoped token grants access.
        response = make_response(render_template("telegram_preview.html", email=email), 200 if email else 404)
        response.headers["Cache-Control"] = "no-store, max-age=0"
        response.headers["Pragma"] = "no-cache"
        response.headers["Referrer-Policy"] = "no-referrer"
        response.headers["X-Content-Type-Options"] = "nosniff"
        # Leave frame-ancestors unset so Telegram can embed the public preview.
        # The inner srcdoc also inherits this policy; scripts, forms and
        # non-image remote resources remain blocked even with malicious markup.
        response.headers["Content-Security-Policy"] = (
            "default-src 'none'; script-src 'none'; style-src 'unsafe-inline'; "
            "img-src https: data:; frame-src 'self'; base-uri 'none'; form-action 'none'; object-src 'none'"
        )
        return response

    return bp
