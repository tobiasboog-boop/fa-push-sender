"""
Mailer — rendert de e-mail HTML via Jinja2 en verstuurt via Resend.
"""
import json
import os
import urllib.request
import urllib.error
from datetime import datetime

from jinja2 import Environment

import fa_db


# ---------------------------------------------------------------------------
# Jinja2-filters (identiek aan delivery.py in de FA app)
# ---------------------------------------------------------------------------

def _eur(value) -> str:
    try:
        return f"€ {float(value):_.0f}".replace("_", ".")
    except (TypeError, ValueError):
        return str(value)


def _round1(value) -> str:
    try:
        return f"{float(value):.1f}"
    except (TypeError, ValueError):
        return str(value)


def _pct(value) -> str:
    try:
        return f"{float(value):.0f}%"
    except (TypeError, ValueError):
        return str(value)


# ---------------------------------------------------------------------------
# Email rendering
# ---------------------------------------------------------------------------

def render_email(config: dict, output_json: dict, klant_naam: str) -> tuple[str, str]:
    """
    Rendert de e-mail HTML en subject via de delivery template uit de DB.

    Returns:
        (subject: str, html_body: str)
    """
    rendering_template = config.get("rendering_template")
    if not rendering_template:
        raise ValueError(
            f"Geen delivery template gevonden voor analyse '{config.get('analyse_naam')}'. "
            "Zet een actieve delivery template in de FA app (App Beheer → Delivery Templates)."
        )

    email_config = config.get("email_config") or {}
    analyse_naam = config.get("analyse_naam", "Financiële Analyse")
    peildatum = datetime.now().strftime("%B %Y")

    # Subject
    subject_template = email_config.get("subject", f"Financiële analyse: {analyse_naam}")
    env = Environment()
    env.filters["eur"] = _eur
    env.filters["round1"] = _round1
    env.filters["pct"] = _pct

    subject = env.from_string(subject_template).render(
        klant_naam=klant_naam,
        analyse_naam=analyse_naam,
        peildatum=peildatum,
        output=output_json,
    )

    # HTML body
    html_body = env.from_string(rendering_template).render(
        output=output_json,
        klant_naam=klant_naam,
        analyse_naam=analyse_naam,
        peildatum=peildatum,
        **output_json,  # spreidt kernboodschap, secties etc. als top-level vars
    )

    return subject, html_body


# ---------------------------------------------------------------------------
# Resend verzending
# ---------------------------------------------------------------------------

def send_email(
    to: list[str],
    subject: str,
    html_body: str,
    from_addr: str = "noreply@insights.notifica.nl",
    reply_to: str = "arthur@notifica.nl",
    cc: list[str] | None = None,
    api_key: str | None = None,
) -> dict:
    """
    Verstuurt de e-mail via Resend API.

    Returns:
        {"id": "<resend-message-id>"}
    """
    if api_key is None:
        api_key = fa_db.get_resend_api_key()

    payload = {
        "from": from_addr,
        "to": to,
        "subject": subject,
        "html": html_body,
        "reply_to": reply_to,
    }
    if cc:
        payload["cc"] = cc

    _resend_post(api_key, payload)
    return {"ok": True, "to": to}


def _resend_post(api_key: str, payload: dict) -> dict:
    """HTTP POST naar Resend, met fallback zonder tracking-veld."""
    url = "https://api.resend.com/emails"
    body = json.dumps(payload).encode("utf-8")
    req = urllib.request.Request(
        url,
        data=body,
        headers={
            "Authorization": f"Bearer {api_key}",
            "Content-Type": "application/json",
        },
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            return json.loads(resp.read())
    except urllib.error.HTTPError as exc:
        error_body = exc.read().decode("utf-8", errors="replace")
        raise RuntimeError(
            f"Resend HTTP {exc.code}: {error_body}"
        ) from exc


# ---------------------------------------------------------------------------
# Alles in één stap
# ---------------------------------------------------------------------------

def render_and_send(
    config: dict,
    output_json: dict,
    klant_naam: str,
    to: list[str],
    cc: list[str] | None = None,
    test_mode: bool = False,
) -> dict:
    """
    Combineert render + send. Bij test_mode wordt '[TEST] ' voor het subject gezet
    en wordt altijd naar info@notifica.nl gestuurd (ongeacht `to`).
    """
    subject, html_body = render_email(config, output_json, klant_naam)

    if test_mode:
        subject = f"[TEST] {subject}"
        to = ["info@notifica.nl"]
        cc = None

    result = send_email(
        to=to,
        subject=subject,
        html_body=html_body,
        cc=cc,
    )
    result["subject"] = subject
    result["html_body"] = html_body
    return result
