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

def klant_naam(leesinstructie: str | None) -> str:
    """Leid de organisatienaam af uit de leesinstructie."""
    tekst = (leesinstructie or "").strip()
    if not tekst:
        return ""
    eerste = tekst.split("\n")[0].strip()
    m = re.match(r"Organisatie:\s*(.+)", eerste)
    if m:
        # Strip precies één afsluitende punt (behoudt 'B.V.')
        return re.sub(r"\.\s*$", "", m.group(1)).strip()
    m = re.match(r"(.+?)\s+is\s+", eerste)
    if m:
        return m.group(1).strip()
    return ""


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

@st.cache_data(ttl=300, show_spinner="Klanten en analyses ophalen...")
def _laad_klant_overzicht(_token_access: str):
    """Bouw {klantnummer: {naam, analyses:[{id,code,naam}]}} uit de FA-config.
    `_token_access` zit in de cache-key zodat een nieuwe login vers laadt."""
    client = _client()
    analyses = [a for a in client.list_all_analyses() if a.get("is_active")]
    klanten: dict[int, dict] = {}
    for a in analyses:
        for r in client.klanten_voor_analyse(a["id"]):
            if not r.get("enabled"):
                continue
            kn = r["klantnummer"]
            entry = klanten.setdefault(kn, {"klantnummer": kn, "naam": "", "analyses": []})
            entry["analyses"].append({"id": a["id"], "code": a["code"], "naam": a["naam"]})
            if not entry["naam"]:
                naam = klant_naam(r.get("leesinstructie"))
                if naam:
                    entry["naam"] = naam
    for entry in klanten.values():
        entry["analyses"].sort(key=lambda x: x["naam"])
    return klanten


def _client() -> FAClient:
    return FAClient(st.session_state.token)


def klant_label(kn: int, naam: str) -> str:
    return f"{kn} — {naam}" if naam else str(kn)


# ---------------------------------------------------------------------------
# Uitvoering: (tijdelijk ontvangers zetten →) run → deliver → verzenden
# ---------------------------------------------------------------------------

def voer_uit(*, client: FAClient, klantnummer: int, analyse: dict,
             cfg_rij: dict, doel_emails: list[str] | None):
    """Draait de analyse en verstuurt. doel_emails=None → standaard ontvangers.
    Bij een override zetten we delivery_ontvangers tijdelijk en herstellen daarna."""
    analyse_id = analyse["id"]
    origineel = cfg_rij.get("delivery_ontvangers") or []
    moet_herstellen = False

    status = st.status("Bezig...", expanded=True)
    try:
        if doel_emails is not None:
            status.write(f"✉️ Ontvangers tijdelijk instellen: {', '.join(doel_emails)}")
            client.set_klant_config(
                klantnummer=klantnummer, analyse_id=analyse_id,
                enabled=bool(cfg_rij.get("enabled", True)),
                leesinstructie=cfg_rij.get("leesinstructie"),
                sql_overrides=cfg_rij.get("sql_overrides"),
                delivery_ontvangers=maak_ontvangers(doel_emails),
            )
            moet_herstellen = True

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
                    enabled=bool(cfg_rij.get("enabled", True)),
                    leesinstructie=cfg_rij.get("leesinstructie"),
                    sql_overrides=cfg_rij.get("sql_overrides"),
                    delivery_ontvangers=origineel,
                )
            except Exception as exc:  # noqa: BLE001
                st.warning(f"Let op: standaard ontvangers terugzetten mislukte ({exc}). "
                           f"Controleer de klant-config in de FA-app.")


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
        klanten = _laad_klant_overzicht(client.access_token)
    except FAError as exc:
        st.error(f"Kan FA-gegevens niet laden: {exc}")
        if "403" in str(exc):
            st.info("Je account heeft mogelijk geen medewerker-rechten in de FA-app. "
                    "Vraag Mark om `is_notifica_employee`.")
        return

    if not klanten:
        st.warning("Geen geconfigureerde klanten gevonden.")
        return

    st.subheader("1 — Klant en analyse")
    col_l, col_r = st.columns([1, 1], gap="large")

    with col_l:
        kns = sorted(klanten.keys())
        keuze = st.selectbox(
            "Klant",
            kns,
            format_func=lambda kn: klant_label(kn, klanten[kn]["naam"]),
        )
        klant = klanten[keuze]
        analyse_opties = klant["analyses"]
        analyse_naam = st.selectbox(
            "Analyse",
            [a["naam"] for a in analyse_opties],
        )
        analyse = next(a for a in analyse_opties if a["naam"] == analyse_naam)

    # Config-rij (ontvangers + leesinstructie + sql_overrides) voor deze combo
    cfg = client.klant_config(keuze)
    cfg_rij = next((r for r in cfg if str(r.get("analyse_id")) == str(analyse["id"])), {})
    standaard = cfg_rij.get("delivery_ontvangers") or []

    with col_r:
        st.markdown("**Standaard ontvangers** (uit de FA-config)")
        if standaard:
            for o in standaard:
                st.code(o.get("email", ""), language=None)
        else:
            st.caption("Geen standaard ontvangers ingesteld — vul hieronder een adres in.")

    st.divider()
    st.subheader("2 — Verzenden")

    st.markdown("**Vervangende ontvangers** (optioneel)")
    st.caption(
        "Leeg laten → de **standaard ontvangers** hierboven. "
        "Ingevuld → wij sturen **alleen** naar deze adressen (de config wordt "
        "tijdelijk aangepast en daarna teruggezet)."
    )
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
    else:
        st.error("Geen ontvangers — vul minimaal één adres in.")

    col_test, col_go = st.columns(2)
    with col_test:
        test_klik = st.button(f"🧪 Testmail → {TEST_ADRES}", use_container_width=True)
    with col_go:
        go_klik = st.button("🚀 Uitvoeren & Versturen", type="primary",
                            use_container_width=True, disabled=not definitief)

    if test_klik or go_klik:
        if test_klik:
            doel = [TEST_ADRES]
        else:
            # Override → die adressen; anders standaard (doel=None laat backend de config gebruiken)
            doel = override if override else None
        try:
            result = voer_uit(client=client, klantnummer=keuze, analyse=analyse,
                              cfg_rij=cfg_rij, doel_emails=doel)
        except Exception as exc:  # noqa: BLE001
            st.error(f"Fout: {exc}")
            return

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
