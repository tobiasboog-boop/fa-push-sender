"""
fa_client — dunne client over Mark's financiele-analyse backend (notifica-app).

De zware pipeline (DWH-snapshot, Claude, e-mail/Resend) zit al in de
notifica-app op VPS4. Deze module roept die endpoints aan met een
Supabase-employee-JWT — dezelfde auth als de financiele-analyse React-app.

Auth: Supabase password-grant (Arthur logt in met zijn notifica-account).
Geen DWH-key, Claude-key of Resend-key meer nodig in deze app.
"""
from __future__ import annotations

import os
import time
import base64
import json

import requests

SUPABASE_URL = os.environ.get("SUPABASE_URL", "https://db.notifica.nl").rstrip("/")
SUPABASE_ANON_KEY = os.environ.get(
    "SUPABASE_ANON_KEY",
    # Publieke anon-key (zelfde als de React-apps gebruiken)
    "eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9.eyJpc3MiOiJzdXBhYmFzZSIsInJlZiI6InVzeHN0ZG1lbGppY2xtY2JqZ3Z1Iiwicm9sZSI6ImFub24iLCJpYXQiOjE3Njk0Mjg0NTYsImV4cCI6MjA4NTAwNDQ1Nn0.OhtOSrpsxqJaWNvhYTGFnZNVQ1JsmOMObbDdYEWR07A",
)
API_BASE = os.environ.get("NOTIFICA_API_URL", "https://app.notifica.nl").rstrip("/")


class FAError(Exception):
    """Fout bij een FA-API call (met leesbaar bericht uit de respons)."""


# ---------------------------------------------------------------------------
# Supabase auth
# ---------------------------------------------------------------------------

def login(email: str, password: str) -> dict:
    """Log in via Supabase password-grant. Geeft het token-object terug
    ({access_token, refresh_token, expires_at, user})."""
    r = requests.post(
        f"{SUPABASE_URL}/auth/v1/token?grant_type=password",
        headers={"apikey": SUPABASE_ANON_KEY, "Content-Type": "application/json"},
        json={"email": email, "password": password},
        timeout=20,
    )
    if not r.ok:
        try:
            msg = r.json().get("error_description") or r.json().get("msg") or r.text
        except Exception:
            msg = r.text
        raise FAError(f"Inloggen mislukt: {msg}")
    data = r.json()
    return _normalize_token(data)


def refresh(refresh_token: str) -> dict:
    r = requests.post(
        f"{SUPABASE_URL}/auth/v1/token?grant_type=refresh_token",
        headers={"apikey": SUPABASE_ANON_KEY, "Content-Type": "application/json"},
        json={"refresh_token": refresh_token},
        timeout=20,
    )
    if not r.ok:
        raise FAError("Sessie verlopen — log opnieuw in.")
    return _normalize_token(r.json())


def _normalize_token(data: dict) -> dict:
    access = data.get("access_token", "")
    return {
        "access_token": access,
        "refresh_token": data.get("refresh_token", ""),
        "expires_at": _jwt_exp(access),
        "user": data.get("user", {}) or {},
    }


def _jwt_exp(token: str) -> float:
    try:
        payload = token.split(".")[1]
        payload += "=" * (-len(payload) % 4)
        return float(json.loads(base64.b64decode(payload)).get("exp", 0))
    except Exception:
        return 0.0


# ---------------------------------------------------------------------------
# FA API client
# ---------------------------------------------------------------------------

class FAClient:
    """Roept de /api/fa/* endpoints aan met een (auto-refreshend) JWT-token."""

    def __init__(self, token: dict):
        self._token = token

    # -- token-beheer -------------------------------------------------------
    @property
    def access_token(self) -> str:
        if self._token.get("expires_at", 0) < time.time() + 60:
            if self._token.get("refresh_token"):
                self._token = refresh(self._token["refresh_token"])
        return self._token["access_token"]

    @property
    def token(self) -> dict:
        return self._token

    def _headers(self) -> dict:
        return {"Authorization": f"Bearer {self.access_token}",
                "Content-Type": "application/json"}

    def _req(self, method: str, path: str, **kwargs):
        url = f"{API_BASE}{path}"
        timeout = kwargs.pop("timeout", 60)
        r = requests.request(method, url, headers=self._headers(), timeout=timeout, **kwargs)
        if not r.ok:
            try:
                err = r.json()
                msg = err.get("error") or err.get("message") or r.text
            except Exception:
                msg = r.text
            raise FAError(f"{method} {path} → HTTP {r.status_code}: {msg}")
        if r.text:
            try:
                return r.json()
            except Exception:
                return {"raw": r.text}
        return {}

    # -- analyses & klanten -------------------------------------------------
    def list_all_analyses(self) -> list[dict]:
        """Alle analyses (admin) — id, code, naam, is_active."""
        return self._req("GET", "/api/fa/admin/analyses").get("analyses", [])

    def klanten_voor_analyse(self, analyse_id: str) -> list[dict]:
        """Klant_analyse_config rijen voor één analyse (klantnummer, enabled, leesinstructie)."""
        return self._req(
            "GET", "/api/fa/admin/analyse-klanten",
            params={"analyse_id": analyse_id},
        ).get("klanten", [])

    def klant_config(self, klantnummer: int) -> list[dict]:
        """Per actieve analyse: enabled, leesinstructie, delivery_ontvangers voor deze klant."""
        return self._req(
            "GET", "/api/fa/admin/klant-config",
            params={"klantnummer": klantnummer},
        ).get("config", [])

    def set_klant_config(self, *, klantnummer: int, analyse_id: str, enabled: bool,
                         leesinstructie, sql_overrides, delivery_ontvangers) -> dict:
        """Upsert klant_analyse_config — o.a. om delivery_ontvangers tijdelijk te zetten."""
        body = {
            "klantnummer": klantnummer,
            "analyse_id": analyse_id,
            "enabled": enabled,
            "leesinstructie": leesinstructie,
            "sql_overrides": sql_overrides if sql_overrides is not None else {},
            "delivery_ontvangers": delivery_ontvangers or [],
        }
        return self._req("PUT", "/api/fa/admin/klant-config", json=body).get("config", {})

    # -- run / deliver / verzenden -----------------------------------------
    def run(self, klantnummer: int, analyse_code: str) -> dict:
        """Draai een analyse (DWH-snapshot + Claude). Geeft o.a. run_id + status."""
        return self._req(
            "POST", "/api/fa/runs",
            json={"klantnummer": klantnummer, "analyse": analyse_code},
            timeout=240,
        )

    def generate_delivery(self, run_id: str) -> dict:
        """Render de e-mail voor een run. Bakt ontvangers in uit klant_analyse_config."""
        return self._req("POST", f"/api/fa/runs/{run_id}/deliveries", json={}, timeout=90)

    def get_delivery(self, delivery_id: str) -> dict:
        return self._req("GET", f"/api/fa/deliveries/{delivery_id}")

    def send_delivery(self, delivery_id: str) -> dict:
        """Verstuur de delivery via de geconfigureerde provider (Resend)."""
        return self._req("POST", f"/api/fa/deliveries/{delivery_id}/verzenden", json={}, timeout=90)
