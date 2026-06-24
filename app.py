"""
Notifica — Financiële Analyse Sender
=====================================
Dunne UI waarmee Arthur zelfstandig analyses kan draaien én versturen.

Alle zware logica (DWH-snapshot, Claude-analyse, e-mail/Resend) zit al in
Mark's financiele-analyse backend in de notifica-app. Deze app roept die
endpoints aan met een Supabase-employee-JWT (zelfde auth als de FA-React-app).
"""
import os
import re

import streamlit as st
from dotenv import load_dotenv

import fa_client
from fa_client import FAClient, FAError

load_dotenv()

st.set_page_config(
    page_title="Analyse Sender — Notifica",
    page_icon="📊",
    layout="wide",
    initial_sidebar_state="collapsed",
)

NAVY = "#16136F"
TEST_ADRES = "info@notifica.nl"


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def ontvangers_tekst(ontvangers: list) -> str:
    if not ontvangers:
        return ""
    return ", ".join(o.get("email", "") for o in ontvangers if o.get("email"))


def parse_emails(raw: str) -> list[str]:
    parts = re.split(r"[\n,;]+", raw or "")
    return [p.strip() for p in parts if p.strip()]


def maak_ontvangers(emails: list[str]) -> list[dict]:
    return [{"rol": "to", "email": e} for e in emails]


def header():
    st.markdown(
        f"""
        <div style="display:flex;align-items:center;gap:12px;margin-bottom:8px">
          <div style="background:linear-gradient(135deg,{NAVY} 0%,#3636A2 100%);
                      width:40px;height:40px;border-radius:10px;
                      display:flex;align-items:center;justify-content:center">
            <svg fill="none" stroke="white" stroke-width="2" viewBox="0 0 24 24"
                 width="20" height="20">
              <polyline points="22 12 18 12 15 21 9 3 6 12 2 12"/>
            </svg>
          </div>
          <div>
            <h1 style="margin:0;font-size:1.4rem;color:{NAVY}">Notifica &mdash; Analyse Sender</h1>
            <p style="margin:0;color:#666;font-size:0.85rem">
              Draai een financiële analyse en verstuur 'm naar de klant
            </p>
          </div>
        </div>
        <hr style="margin:0 0 24px 0;border:none;border-top:1px solid #eee">
        """,
        unsafe_allow_html=True,
    )


# ---------------------------------------------------------------------------
# Login (Supabase — zelfde account als de financiele-analyse app)
# ---------------------------------------------------------------------------

def _ensure_session():
    """Authenticeer de app op de achtergrond met een vaste service-login uit de env.
    Geen login-scherm: toegang wordt afgeschermd door het Notifica-platform (de
    draft is alleen bereikbaar voor ingelogde medewerkers)."""
    tok = st.session_state.get("token")
    if tok and tok.get("access_token"):
        return

    # Primair: vast refresh-token (captcha-vrij; Supabase-wachtwoordlogin is door
    # Cloudflare Turnstile geblokkeerd voor server-side gebruik).
    rt = os.environ.get("FA_REFRESH_TOKEN", "").strip()
    if rt:
        try:
            token = fa_client.refresh(rt)
        except FAError as exc:
            header()
            st.error(f"Service-sessie verlopen — vernieuw FA_REFRESH_TOKEN: {exc}")
            st.stop()
        st.session_state.token = token
        st.session_state.user_email = (token.get("user") or {}).get("email", "")
        return

    # Fallback: wachtwoordlogin (werkt alleen als captcha uit staat).
    email = os.environ.get("FA_LOGIN_EMAIL", "").strip()
    pw = os.environ.get("FA_LOGIN_PASSWORD", "")
    if email and pw:
        try:
            token = fa_client.login(email, pw)
        except FAError as exc:
            header()
            st.error(f"Service-login mislukt: {exc}")
            st.stop()
        st.session_state.token = token
        st.session_state.user_email = (token.get("user") or {}).get("email", email)
        return

    header()
    st.error("App niet geconfigureerd: zet FA_REFRESH_TOKEN in de env.")
    st.stop()


# ---------------------------------------------------------------------------
# Data laden (gecached per sessie)
# ---------------------------------------------------------------------------

@st.cache_data(ttl=600, show_spinner="Klanten ophalen...")
def _laad_organisaties(_token_access: str) -> list[dict]:
    """Alle actieve organisaties (klantnummer + autoritatieve naam) uit Supabase."""
    return _client().list_organizations()


@st.cache_data(ttl=120, show_spinner="Analyses voor klant ophalen...")
def _laad_klant_config(_token_access: str, klantnummer: int) -> list[dict]:
    """Per actieve analyse: enabled, ontvangers, leesinstructie voor deze klant."""
    return _client().klant_config(klantnummer)


def _client() -> FAClient:
    return FAClient(st.session_state.token)


def klant_label(kn: int, naam: str) -> str:
    return f"{kn} — {naam}" if naam else str(kn)


# ---------------------------------------------------------------------------
# Uitvoering: (tijdelijk ontvangers zetten →) run → deliver → verzenden
# ---------------------------------------------------------------------------

def voer_uit(*, client: FAClient, klantnummer: int, analyse: dict, cfg_rij: dict,
             doel_emails: list[str] | None, leesinstructie: str | None,
             permanent: bool):
    """Draait de analyse en verstuurt.

    - doel_emails=None  → gebruik de bestaande config-ontvangers ongewijzigd.
    - doel_emails=[...]  → stel die ontvangers in (en enable de combo).
    - permanent=False    → config-wijziging na afloop terugdraaien (test / eenmalige override).
    - permanent=True     → config-wijziging behouden (activatie van een nieuwe combo).
    """
    analyse_id = analyse["id"]
    orig_ontv = cfg_rij.get("delivery_ontvangers") or []
    orig_enabled = bool(cfg_rij.get("enabled", False))
    orig_lees = cfg_rij.get("leesinstructie")
    moet_herstellen = False

    status = st.status("Bezig...", expanded=True)
    try:
        # Config aanraken als we ontvangers zetten en/of de combo nog niet aan staat
        if doel_emails is not None or not orig_enabled:
            actie = "activeren + ontvangers instellen" if not orig_enabled else "ontvangers instellen"
            status.write(f"✉️ {actie}: {', '.join(doel_emails) if doel_emails else '(bestaand)'}")
            client.set_klant_config(
                klantnummer=klantnummer, analyse_id=analyse_id,
                enabled=True,
                leesinstructie=leesinstructie if leesinstructie is not None else orig_lees,
                sql_overrides=cfg_rij.get("sql_overrides"),
                delivery_ontvangers=(maak_ontvangers(doel_emails) if doel_emails is not None else orig_ontv),
            )
            moet_herstellen = not permanent

        status.write("📥 Analyse draaien (DWH + Claude)... dit duurt ~1 min")
        run = client.run(klantnummer, analyse["code"])
        run_id = run.get("run_id") or run.get("id")
        if run.get("status") != "done" or not run_id:
            raise FAError(f"Run niet voltooid: {run}")
        status.write(f"✓ Run klaar ({run.get('tokens_out', '?')} tokens) — e-mail renderen")

        delivery = client.generate_delivery(run_id)
        delivery_id = delivery.get("id")
        payload = delivery.get("payload", {})
        werkelijke_to = ontvangers_tekst(payload.get("to", []))
        status.write(f"✓ E-mail gerenderd — verzenden naar: {werkelijke_to}")

        send = client.send_delivery(delivery_id)
        if not send.get("success"):
            raise FAError(f"Verzenden mislukt: {send.get('error_message') or send}")

        status.update(label="✅ Verzonden!", state="complete")
        return {"run_id": run_id, "delivery_id": delivery_id,
                "payload": payload, "to": werkelijke_to}
    except Exception:
        status.update(label="❌ Mislukt", state="error")
        raise
    finally:
        if moet_herstellen:
            try:
                client.set_klant_config(
                    klantnummer=klantnummer, analyse_id=analyse_id,
                    enabled=orig_enabled,
                    leesinstructie=orig_lees,
                    sql_overrides=cfg_rij.get("sql_overrides"),
                    delivery_ontvangers=orig_ontv,
                )
            except Exception as exc:  # noqa: BLE001
                st.warning(f"Let op: de klant-config terugzetten mislukte ({exc}). "
                           f"Controleer 'm in de FA-app.")


# ---------------------------------------------------------------------------
# Hoofdscherm
# ---------------------------------------------------------------------------

def hoofdscherm():
    header()
    client = _client()

    top_l, top_r = st.columns([4, 1])
    with top_r:
        st.caption(f"Service: {st.session_state.get('user_email','?')}")

    try:
        orgs = _laad_organisaties(client.access_token)
    except FAError as exc:
        st.error(f"Kan FA-gegevens niet laden: {exc}")
        if "403" in str(exc):
            st.info("Je account heeft mogelijk geen medewerker-rechten in de FA-app. "
                    "Vraag Mark om `is_notifica_employee`.")
        return

    if not orgs:
        st.warning("Geen organisaties gevonden.")
        return

    naam_map = {o["klantnummer"]: o["naam"] for o in orgs}
    kns = [o["klantnummer"] for o in orgs]

    st.subheader("1 — Klant en analyse")
    col_l, col_r = st.columns([1, 1], gap="large")

    with col_l:
        keuze = st.selectbox(
            "Klant", kns,
            format_func=lambda kn: klant_label(kn, naam_map.get(kn, "")),
            help="Alle organisaties — ook klanten waarvoor een analyse nog niet is geactiveerd.",
        )
        # Alle actieve analyses + of ze al aan staan voor deze klant
        cfg = _laad_klant_config(client.access_token, keuze)
        if not cfg:
            st.warning("Geen analyses beschikbaar.")
            return

        def _analyse_label(r):
            return f"{r['analyse_naam']}  ✓" if r.get("enabled") else r["analyse_naam"]

        cfg_rij = st.selectbox(
            "Analyse", cfg,
            format_func=_analyse_label,
            help="✓ = al geactiveerd voor deze klant. Anders wordt 'ie bij versturen aangemaakt.",
        )
        analyse = {"id": cfg_rij["analyse_id"], "code": cfg_rij["analyse_code"],
                   "naam": cfg_rij["analyse_naam"]}

    enabled = bool(cfg_rij.get("enabled"))
    standaard = cfg_rij.get("delivery_ontvangers") or []

    with col_r:
        if enabled:
            st.markdown("**Standaard ontvangers** (uit de FA-config)")
            if standaard:
                for o in standaard:
                    st.code(o.get("email", ""), language=None)
            else:
                st.caption("Geen standaard ontvangers ingesteld — vul hieronder een adres in.")
        else:
            st.info("Deze analyse is **nog niet geactiveerd** voor deze klant. "
                    "Bij 'Uitvoeren & Versturen' wordt 'ie aangemaakt (geactiveerd) "
                    "met de ontvangers die je hieronder invult.")

    # Leesinstructie alleen bij een nieuwe (nog niet geactiveerde) combo
    leesinstructie_nieuw = None
    if not enabled:
        leesinstructie_nieuw = st.text_input(
            "Organisatie-context voor de analyse (leesinstructie)",
            value=f"Organisatie: {naam_map.get(keuze,'')}.",
            help="Komt in de prompt; de organisatienaam wordt het onderwerp van de mail.",
        )

    st.divider()
    st.subheader("2 — Verzenden")

    st.markdown("**Ontvangers**" if not enabled else "**Vervangende ontvangers** (optioneel)")
    if enabled:
        st.caption(
            "Leeg laten → de **standaard ontvangers** hierboven. "
            "Ingevuld → wij sturen **alleen** naar deze adressen (de config wordt "
            "tijdelijk aangepast en daarna teruggezet)."
        )
    else:
        st.caption("Vul de ontvanger(s) in. Deze worden de standaard ontvangers van de "
                   "nieuwe klant-analyse.")
    override_raw = st.text_area(
        "E-mailadressen (één per regel of komma-gescheiden)",
        placeholder="contactpersoon@klant.nl",
        height=90,
        label_visibility="collapsed",
    )
    override = parse_emails(override_raw)

    definitief = override if override else [o.get("email", "") for o in standaard if o.get("email")]

    if definitief:
        st.success(f"Wordt verzonden naar: {', '.join(definitief)}")
    elif not enabled:
        st.error("Vul minimaal één ontvanger in (de combinatie wordt nieuw aangemaakt).")
    else:
        st.error("Geen ontvangers — vul minimaal één adres in.")

    col_test, col_go = st.columns(2)
    with col_test:
        test_klik = st.button(f"🧪 Testmail → {TEST_ADRES}", use_container_width=True)
    with col_go:
        go_label = "🚀 Activeren & Versturen" if not enabled else "🚀 Uitvoeren & Versturen"
        go_klik = st.button(go_label, type="primary",
                            use_container_width=True, disabled=not definitief)

    if test_klik or go_klik:
        if test_klik:
            doel, permanent = [TEST_ADRES], False
        elif not enabled:
            doel, permanent = override, True            # activatie: permanent
        else:
            doel, permanent = (override or None), False  # eenmalige override of standaard
        try:
            result = voer_uit(client=client, klantnummer=keuze, analyse=analyse,
                              cfg_rij=cfg_rij, doel_emails=doel,
                              leesinstructie=leesinstructie_nieuw, permanent=permanent)
        except Exception as exc:  # noqa: BLE001
            st.error(f"Fout: {exc}")
            return

        if go_klik and not enabled:
            _laad_klant_config.clear()  # combo is nu geactiveerd → cache verversen
        st.success(f"✅ Verzonden naar {result['to']}")
        with st.expander("E-mail preview", expanded=True):
            st.caption(f"Onderwerp: {result['payload'].get('subject','—')}")
            html = result["payload"].get("body_html", "")
            if html:
                st.components.v1.html(html, height=600, scrolling=True)


# ---------------------------------------------------------------------------
# Router
# ---------------------------------------------------------------------------

_ensure_session()
hoofdscherm()
