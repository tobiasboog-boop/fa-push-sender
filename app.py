"""
Notifica — Financiële Analyse Sender
=====================================
Dunne UI waarmee Arthur zelfstandig analyses kan draaien én versturen.

Alle zware logica (DWH-snapshot, Claude-analyse, e-mail/Resend) zit al in
Mark's financiele-analyse backend in de notifica-app. Deze app roept die
endpoints aan met een Supabase-employee-JWT (zelfde auth als de FA-React-app).
"""
import base64
import json
import re

import streamlit as st
import streamlit.components.v1 as components
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

DRAFT_DEFAULT_URL = "https://app.notifica.nl/apps/drafts/push-analyse-sender/"


def _capture_ms_auth() -> bool:
    """Vang een token op dat via ?ms_auth=... in de URL is gezet (Microsoft-sessie
    of OAuth-redirect). Geeft True terug als er ingelogd is."""
    tok = st.query_params.get("ms_auth")
    if not tok:
        return False
    try:
        pad = "=" * (-len(tok) % 4)
        data = json.loads(base64.urlsafe_b64decode(tok + pad))
        token = fa_client.token_from_pair(
            data.get("access_token", ""), data.get("refresh_token", ""))
        if token["access_token"]:
            st.session_state.token = token
            st.session_state.user_email = token["user"].get("email", "")
    except Exception:
        pass
    st.query_params.clear()
    st.rerun()
    return True


def _ms_login_component():
    """JS: hergebruik bestaande Notifica-sessie (localStorage) óf start een verse
    Microsoft-login. Geeft het token door aan Streamlit via ?ms_auth=..."""
    html = """
    <div style="font-family:Inter,sans-serif;">
      <button id="reuse" style="display:none;width:100%;padding:11px;margin-bottom:8px;
        background:#16136F;color:#fff;border:none;border-radius:8px;font-size:14px;
        font-weight:600;cursor:pointer;">Doorgaan als <span id="em"></span></button>
      <button id="mslogin" style="width:100%;padding:11px;background:#fff;color:#16136F;
        border:1px solid #16136F;border-radius:8px;font-size:14px;font-weight:600;
        cursor:pointer;display:flex;align-items:center;justify-content:center;gap:8px;">
        <svg width="16" height="16" viewBox="0 0 23 23"><path fill="#f25022" d="M1 1h10v10H1z"/>
        <path fill="#7fba00" d="M12 1h10v10H12z"/><path fill="#00a4ef" d="M1 12h10v10H1z"/>
        <path fill="#ffb900" d="M12 12h10v10H12z"/></svg>
        Inloggen met Microsoft</button>
      <p id="hint" style="margin:8px 0 0;font-size:12px;color:#888;"></p>
    </div>
    <script>
    (function(){
      var SUPABASE="%%SUPABASE%%", FALLBACK="%%REDIRECT%%";
      function b64(o){var s=btoa(JSON.stringify(o));return s.replace(/\\+/g,'-').replace(/\\//g,'_').replace(/=+$/,'');}
      // Same-origin frames waarin de Notifica-sessie kan staan
      function stores(){ var a=[]; try{a.push(window.localStorage);}catch(e){} try{if(window.parent&&window.parent!==window)a.push(window.parent.localStorage);}catch(e){} try{if(window.top&&window.top!==window)a.push(window.top.localStorage);}catch(e){} return a; }
      function findSession(){
        var ss=stores();
        for(var i=0;i<ss.length;i++){
          try{ var raw=ss[i].getItem('notifica-auth'); if(!raw) continue;
            var s=JSON.parse(raw);
            var at=s.access_token||(s.currentSession&&s.currentSession.access_token);
            var rt=s.refresh_token||(s.currentSession&&s.currentSession.refresh_token)||'';
            var em=(s.user&&s.user.email)||(s.currentSession&&s.currentSession.user&&s.currentSession.user.email)||'';
            if(at) return {at:at,rt:rt,em:em};
          }catch(e){}
        }
        return null;
      }
      function reloadApp(token){
        // Streamlit-app (parent) of top herladen met ?ms_auth — same-origin, dus toegestaan
        var targets=[]; try{if(window.parent&&window.parent!==window)targets.push(window.parent);}catch(e){} try{if(window.top&&window.top!==window)targets.push(window.top);}catch(e){} targets.push(window);
        for(var i=0;i<targets.length;i++){
          try{ var base=targets[i].location.href.split('#')[0].split('?')[0]; targets[i].location.replace(base+'?ms_auth='+token); return true; }catch(e){}
        }
        return false;
      }
      function useSession(sess){ return reloadApp(b64({access_token:sess.at,refresh_token:sess.rt})); }

      // 1) Microsoft OAuth-redirect kwam terug in DIT tabblad (tokens in de hash)
      try{
        var hw=(window.parent&&window.parent!==window)?window.parent:window;
        var h=''; try{h=hw.location.hash||'';}catch(e){h=window.location.hash||'';}
        if(h.indexOf('access_token=')>=0){
          var p=new URLSearchParams(h.substring(1));
          var at=p.get('access_token'), rt=p.get('refresh_token')||'';
          if(at){ reloadApp(b64({access_token:at,refresh_token:rt})); return; }
        }
      }catch(e){}

      // 2) Bestaande Notifica-sessie (Microsoft) hergebruiken
      var sess=findSession();
      if(sess){
        var b=document.getElementById('reuse');
        document.getElementById('em').textContent=sess.em||'je Microsoft-account';
        b.style.display='block';
        b.onclick=function(){ if(!useSession(findSession()||sess)) document.getElementById('hint').textContent='Kon de sessie niet doorgeven — gebruik e-mail/wachtwoord.'; };
      }

      // 3) Verse Microsoft-login: opent in NIEUW TABBLAD (Microsoft kan niet in een iframe)
      document.getElementById('mslogin').onclick=function(){
        var rt; try{ rt=window.top.location.href.split('#')[0].split('?')[0]; }catch(e){ rt=FALLBACK; }
        if(!rt) rt=FALLBACK;
        var url=SUPABASE+'/auth/v1/authorize?provider=azure&redirect_to='+encodeURIComponent(rt);
        window.open(url,'_blank');
        document.getElementById('hint').textContent='Microsoft opent in een nieuw tabblad. Log daar in; hier verschijnt dan automatisch een knop om door te gaan.';
        // Poll op de nieuw verkregen sessie (max ~2 min)
        var tries=0, iv=setInterval(function(){
          tries++; var s=findSession();
          if(s){ clearInterval(iv); useSession(s); }
          else if(tries>80){ clearInterval(iv); }
        },1500);
      };
    })();
    </script>
    """
    html = html.replace("%%SUPABASE%%", fa_client.SUPABASE_URL).replace("%%REDIRECT%%", DRAFT_DEFAULT_URL)
    components.html(html, height=130)


def login_scherm():
    header()
    col_l, col_m, col_r = st.columns([1, 1.4, 1])
    with col_m:
        st.markdown(f"<h3 style='color:{NAVY}'>🔒 Inloggen</h3>", unsafe_allow_html=True)
        st.caption("Log in met je Notifica-account (zelfde als app.notifica.nl).")

        _ms_login_component()

        st.markdown("<p style='text-align:center;color:#aaa;font-size:13px;margin:6px 0'>of met e-mail</p>",
                    unsafe_allow_html=True)
        email = st.text_input("E-mailadres", placeholder="arthur@notifica.nl")
        wachtwoord = st.text_input("Wachtwoord", type="password")
        if st.button("Inloggen", type="primary", use_container_width=True):
            if not email or not wachtwoord:
                st.error("Vul e-mailadres en wachtwoord in.")
                return
            try:
                token = fa_client.login(email.strip(), wachtwoord)
            except FAError as exc:
                st.error(str(exc))
                return
            st.session_state.token = token
            st.session_state.user_email = (token.get("user") or {}).get("email", email.strip())
            st.rerun()


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
        st.caption(f"Ingelogd: {st.session_state.get('user_email','?')}")
        if st.button("Uitloggen", use_container_width=True):
            for k in ("token", "user_email"):
                st.session_state.pop(k, None)
            st.cache_data.clear()
            st.rerun()

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

if "token" not in st.session_state:
    _capture_ms_auth()  # vangt ?ms_auth=... op (Microsoft-sessie / OAuth-redirect)
    login_scherm()
else:
    hoofdscherm()
