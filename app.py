"""
Notifica — Financiële Analyse Sender
Streamlit-app waarmee Arthur zelfstandig analyses kan uitvoeren en versturen.
"""
import json
import os
from datetime import datetime, timezone

import streamlit as st
from dotenv import load_dotenv

load_dotenv()

# ---------------------------------------------------------------------------
# Pagina-config (altijd als eerste Streamlit-call)
# ---------------------------------------------------------------------------

st.set_page_config(
    page_title="Analyse Sender — Notifica",
    page_icon="📊",
    layout="wide",
    initial_sidebar_state="expanded",
)

# ---------------------------------------------------------------------------
# Wachtwoordbeveiliging
# ---------------------------------------------------------------------------

def _check_password() -> bool:
    expected = os.environ.get("APP_PASSWORD", "")
    if not expected:
        return True  # geen wachtwoord ingesteld = open (dev-modus)

    if st.session_state.get("authenticated"):
        return True

    with st.container():
        st.markdown(
            """
            <div style="max-width:400px;margin:80px auto;text-align:center">
              <h2>🔒 Notifica Analyse Sender</h2>
              <p style="color:#666">Intern hulpmiddel — voer het wachtwoord in</p>
            </div>
            """,
            unsafe_allow_html=True,
        )
        col_l, col_m, col_r = st.columns([1, 2, 1])
        with col_m:
            pwd = st.text_input("Wachtwoord", type="password", label_visibility="collapsed",
                                placeholder="Wachtwoord")
            if st.button("Inloggen", use_container_width=True, type="primary"):
                if pwd == expected:
                    st.session_state.authenticated = True
                    st.rerun()
                else:
                    st.error("Onjuist wachtwoord")
    return False


if not _check_password():
    st.stop()


# ---------------------------------------------------------------------------
# Imports (pas na login, zodat DB-errors de login niet blokkeren)
# ---------------------------------------------------------------------------

import fa_db
import pipeline
import mailer


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _format_datum(dt) -> str:
    if dt is None:
        return "—"
    if hasattr(dt, "astimezone"):
        dt = dt.astimezone(timezone.utc).replace(tzinfo=None)
    delta = datetime.utcnow() - dt
    days = delta.days
    if days == 0:
        return "vandaag"
    if days == 1:
        return "gisteren"
    return f"{days} dagen geleden ({dt.strftime('%d-%m-%Y')})"


def _parse_ontvangers(config: dict) -> list[str]:
    raw = config.get("delivery_ontvangers")
    if not raw:
        return []
    if isinstance(raw, str):
        try:
            raw = json.loads(raw)
        except json.JSONDecodeError:
            return [e.strip() for e in raw.split(",") if e.strip()]
    if isinstance(raw, list):
        result = []
        for item in raw:
            if isinstance(item, dict):
                result.append(item.get("email", ""))
            else:
                result.append(str(item))
        return [e for e in result if e]
    if isinstance(raw, dict):
        return raw.get("to", [])
    return []


# ---------------------------------------------------------------------------
# Data laden (gecached per sessie)
# ---------------------------------------------------------------------------

@st.cache_data(ttl=120, show_spinner="Configuraties ophalen...")
def _load_configs():
    return fa_db.get_actieve_configs()


@st.cache_data(ttl=300, show_spinner="Analyses ophalen...")
def _load_alle_analyses():
    return fa_db.get_alle_analyses()


@st.cache_data(ttl=300, show_spinner="Klantnaam ophalen...")
def _load_klant_naam(klantnummer: int) -> str:
    try:
        return pipeline.get_klant_naam(klantnummer)
    except Exception:
        return f"Klant {klantnummer}"


# ---------------------------------------------------------------------------
# Navigatie / sessiestatus
# ---------------------------------------------------------------------------

def _init_state():
    defaults = {
        "stap": 1,          # 1=selectie, 2=ontvangers+bevestiging, 3=resultaat
        "geselecteerd": None,
        "run_result": None,
        "mail_result": None,
        "error": None,
        "test_verstuurd": False,
    }
    for k, v in defaults.items():
        if k not in st.session_state:
            st.session_state[k] = v


_init_state()


# ---------------------------------------------------------------------------
# Header
# ---------------------------------------------------------------------------

st.markdown(
    """
    <div style="display:flex;align-items:center;gap:12px;margin-bottom:8px">
      <div style="background:linear-gradient(135deg,#16136F 0%,#3636A2 100%);
                  width:40px;height:40px;border-radius:10px;
                  display:flex;align-items:center;justify-content:center">
        <svg fill="none" stroke="white" stroke-width="2" viewBox="0 0 24 24"
             width="20" height="20">
          <polyline points="22 12 18 12 15 21 9 3 6 12 2 12"/>
        </svg>
      </div>
      <div>
        <h1 style="margin:0;font-size:1.4rem;color:#16136F">
          Notifica &mdash; Analyse Sender
        </h1>
        <p style="margin:0;color:#666;font-size:0.85rem">
          Voer financiële analyses uit en verstuur ze naar klanten
        </p>
      </div>
    </div>
    <hr style="margin:0 0 24px 0;border:none;border-top:1px solid #eee">
    """,
    unsafe_allow_html=True,
)


# ---------------------------------------------------------------------------
# Stap 1 — Selectie
# ---------------------------------------------------------------------------

if st.session_state.stap == 1:
    try:
        configs = _load_configs()
        alle_analyses = _load_alle_analyses()
    except Exception as exc:
        st.error(f"Kan FA app DB niet bereiken: {exc}")
        st.stop()

    # Geconfigureerde klanten (hebben laatste-run info)
    klanten: dict[int, dict] = {}
    for cfg in configs:
        kn = cfg["klantnummer"]
        if kn not in klanten:
            klanten[kn] = {"klantnummer": kn, "analyses": []}
        klanten[kn]["analyses"].append(cfg)

    # Geconfigureerde klantnummers als dropdown-opties + "Andere klant"
    geconfigureerde_kns = sorted(klanten.keys())
    klant_opties = [str(kn) for kn in geconfigureerde_kns] + ["Andere klant..."]

    col_left, col_right = st.columns([1, 1], gap="large")

    with col_left:
        st.subheader("Stap 1 — Klant en analyse")

        gekozen_klant_label = st.selectbox(
            "Klant",
            klant_opties,
            key="klant_select",
        )

        if gekozen_klant_label == "Andere klant...":
            kn_input = st.number_input(
                "Klantnummer",
                min_value=1000, max_value=9999, step=1,
                value=1200,
                key="klant_custom",
                help="Voer een willekeurig klantnummer in",
            )
            gekozen_kn = int(kn_input)
            klant_info = None
            geconfigureerde_analyse_ids = set()
        else:
            gekozen_kn = int(gekozen_klant_label)
            klant_info = klanten.get(gekozen_kn)
            geconfigureerde_analyse_ids = {a["analyse_id"] for a in klant_info["analyses"]} if klant_info else set()

        # Analyses: alle analyses, met ★ voor geconfigureerde combos
        analyse_opties_labels = []
        analyse_opties_map = {}
        for a in alle_analyses:
            aid = str(a["analyse_id"])
            prefix = "★ " if aid in geconfigureerde_analyse_ids else ""
            label = f"{prefix}{a['analyse_naam']}"
            analyse_opties_labels.append(label)
            analyse_opties_map[label] = a

        gekozen_analyse_label = st.selectbox(
            "Analyse",
            analyse_opties_labels,
            key="analyse_select",
            help="★ = al geconfigureerd voor deze klant",
        )
        gekozen_analyse = analyse_opties_map[gekozen_analyse_label]

        # Status laatste run (alleen als geconfigureerde combo)
        laatste_run = None
        if klant_info:
            cfg_match = next(
                (a for a in klant_info["analyses"] if str(a["analyse_id"]) == str(gekozen_analyse["analyse_id"])),
                None,
            )
            if cfg_match:
                laatste_run = cfg_match.get("laatste_run_datum")

        if laatste_run:
            days = (datetime.utcnow() - laatste_run.replace(tzinfo=None)).days
            if days <= 3:
                kleur, icoon = "#d1fae5", "✓"
            elif days <= 14:
                kleur, icoon = "#fef9c3", "⚠"
            else:
                kleur, icoon = "#fee2e2", "⚠"
            st.markdown(
                f"""<div style="background:{kleur};padding:10px 14px;border-radius:8px;
                              font-size:0.85rem;margin-top:8px">
                  {icoon} Laatste run: <strong>{_format_datum(laatste_run)}</strong>
                </div>""",
                unsafe_allow_html=True,
            )
        else:
            label_msg = "Nog geen eerdere run voor deze combinatie" if klant_info else "Nieuwe combinatie — geen eerdere run"
            st.markdown(
                f"""<div style="background:#f3f4f6;padding:10px 14px;border-radius:8px;
                              font-size:0.85rem;margin-top:8px;color:#666">
                  {label_msg}
                </div>""",
                unsafe_allow_html=True,
            )

        st.markdown("<br>", unsafe_allow_html=True)
        if st.button("Volgende →", type="primary", use_container_width=True):
            # Haal config op (met fallback op analyse-defaults als geen klant_analyse_config)
            geconfigureerde_combo = cfg_match if (klant_info and cfg_match) else None
            st.session_state.geselecteerd = {
                "klantnummer": gekozen_kn,
                "klant_naam": f"Klant {gekozen_kn}",
                "analyse_id": str(gekozen_analyse["analyse_id"]),
                "analyse_naam": gekozen_analyse["analyse_naam"],
                "config": geconfigureerde_combo or {},
            }
            st.session_state.stap = 2
            st.session_state.error = None
            st.rerun()

    with col_right:
        st.subheader("Geconfigureerde klanten")
        for kn in sorted(klanten.keys()):
            k = klanten[kn]
            with st.expander(f"Klant {kn}", expanded=(kn == gekozen_kn if gekozen_klant_label != "Andere klant..." else False)):
                for a in k["analyses"]:
                    laatste = _format_datum(a.get("laatste_run_datum"))
                    st.markdown(
                        f"**{a['analyse_naam']}**  \n"
                        f"<span style='font-size:0.8rem;color:#666'>Laatste run: {laatste}</span>",
                        unsafe_allow_html=True,
                    )


# ---------------------------------------------------------------------------
# Stap 2 — Ontvangers + bevestiging
# ---------------------------------------------------------------------------

elif st.session_state.stap == 2:
    sel = st.session_state.geselecteerd

    # Terug-knop
    if st.button("← Terug"):
        st.session_state.stap = 1
        st.rerun()

    st.subheader(f"Stap 2 — Ontvangers: {sel['klant_naam']} / {sel['analyse_naam']}")

    standaard_ontvangers = _parse_ontvangers(sel["config"])

    col_l, col_r = st.columns([1, 1], gap="large")

    with col_l:
        st.markdown("**Standaard ontvangers** (uit klantconfiguratie in FA app)")
        if standaard_ontvangers:
            for email in standaard_ontvangers:
                st.code(email, language=None)
        else:
            st.caption("Geen standaard ontvangers ingesteld.")

        st.markdown("---")
        st.markdown("**Vervangende of extra ontvangers**")
        st.caption(
            "Leeg laten = gebruik standaard ontvangers. "
            "Ingevuld = gebruik ALLEEN deze adressen."
        )
        extra_input = st.text_area(
            "E-mailadressen (één per regel)",
            placeholder="arthur@notifica.nl\ncontact@klant.nl",
            height=110,
            key="extra_emails",
            label_visibility="collapsed",
        )
        extra_emails = [e.strip() for e in extra_input.splitlines() if e.strip()]

        uiteindelijk = extra_emails if extra_emails else standaard_ontvangers

        if uiteindelijk:
            st.success(f"Wordt verzonden naar: {', '.join(uiteindelijk)}")
        else:
            st.error("Geen ontvangers ingesteld — vul minimaal één e-mailadres in.")

    with col_r:
        st.markdown("**Opties**")

        run_mode = st.radio(
            "Uitvoeringsmodus",
            ["Nieuwe analyse uitvoeren (huidige data)", "Laatste run hergebruiken (snel)"],
            index=0,
            key="run_mode",
        )
        fresh_run = run_mode.startswith("Nieuwe")

        if not fresh_run and not sel["config"].get("laatste_run_id"):
            st.warning("Geen eerdere run beschikbaar — nieuwe run is vereist.")
            fresh_run = True

        st.markdown("---")

        col_test, col_go = st.columns(2)
        with col_test:
            test_btn = st.button(
                "🧪 Testmail → info@",
                disabled=not uiteindelijk,
                use_container_width=True,
                help="Verzendt naar info@notifica.nl als test",
            )
        with col_go:
            go_btn = st.button(
                "🚀 Uitvoeren & Versturen",
                type="primary",
                disabled=not uiteindelijk,
                use_container_width=True,
            )

    # ---------------------------------------------------------------------------
    # Uitvoering
    # ---------------------------------------------------------------------------

    def _do_run(klantnummer, analyse_id, to_list, test_mode=False, fresh=True):
        """Voert de pipeline uit en verstuurt de e-mail. Geeft result-dict terug."""
        sel_info = st.session_state.geselecteerd

        if fresh:
            with st.status("Analyse uitvoeren...", expanded=True) as status:
                st.write("📥 Data ophalen uit DWH via Notifica API...")
                try:
                    run_result = pipeline.run_analyse(klantnummer, analyse_id, sla_op=True)
                except Exception as exc:
                    status.update(label="❌ Fout bij analyse", state="error")
                    raise

                st.write(f"✓ {run_result['snapshot'].get('row_count', '?')} rijen verwerkt")
                st.write("🤖 Analyse genereren via Claude...")
                # (al gedaan in run_analyse)

                st.write("✉️ E-mail renderen...")
                config_full = fa_db.get_analyse_config(klantnummer, analyse_id)

                try:
                    mail_result = mailer.render_and_send(
                        config=config_full,
                        output_json=run_result["output_json"],
                        klant_naam=run_result["klant_naam"],
                        to=["info@notifica.nl"] if test_mode else to_list,
                        test_mode=test_mode,
                    )
                except Exception as exc:
                    status.update(label="❌ Fout bij verzenden", state="error")
                    raise

                status.update(label="✅ Klaar!", state="complete")
                return {**run_result, "mail": mail_result, "test_mode": test_mode}

        else:
            # Hergebruik laatste run
            with st.status("Laatste run ophalen en verzenden...", expanded=True) as status:
                st.write("📂 Laatste run ophalen...")
                last = fa_db.get_laatste_run_output(klantnummer, analyse_id)
                if not last:
                    status.update(label="❌ Geen run gevonden", state="error")
                    raise ValueError("Geen eerdere run beschikbaar")

                output_json = last.get("output_json") or {}
                if isinstance(output_json, str):
                    output_json = json.loads(output_json)

                st.write("✉️ E-mail renderen...")
                config_full = fa_db.get_analyse_config(klantnummer, analyse_id)
                klant_naam = _load_klant_naam(klantnummer)

                mail_result = mailer.render_and_send(
                    config=config_full,
                    output_json=output_json,
                    klant_naam=klant_naam,
                    to=["info@notifica.nl"] if test_mode else to_list,
                    test_mode=test_mode,
                )
                status.update(label="✅ Klaar!", state="complete")
                return {
                    "run_id": str(last["run_id"]),
                    "output_json": output_json,
                    "klant_naam": klant_naam,
                    "mail": mail_result,
                    "test_mode": test_mode,
                }

    if test_btn:
        try:
            result = _do_run(
                sel["klantnummer"], sel["analyse_id"],
                to_list=uiteindelijk,
                test_mode=True,
                fresh=fresh_run,
            )
            st.success("Testmail verzonden naar info@notifica.nl")
            with st.expander("E-mail preview"):
                st.components.v1.html(result["mail"]["html_body"], height=600, scrolling=True)
        except Exception as exc:
            st.error(f"Fout: {exc}")

    if go_btn:
        try:
            result = _do_run(
                sel["klantnummer"], sel["analyse_id"],
                to_list=uiteindelijk,
                test_mode=False,
                fresh=fresh_run,
            )
            st.session_state.run_result = result
            st.session_state.stap = 3
            st.rerun()
        except Exception as exc:
            st.error(f"Fout: {exc}")


# ---------------------------------------------------------------------------
# Stap 3 — Resultaat
# ---------------------------------------------------------------------------

elif st.session_state.stap == 3:
    result = st.session_state.run_result
    sel = st.session_state.geselecteerd

    st.success(
        f"✅ Analyse verstuurd naar {', '.join(result['mail']['to'])}",
        icon="📬",
    )

    col_a, col_b = st.columns([1, 1], gap="large")

    with col_a:
        st.subheader("Samenvatting")
        st.markdown(f"**Klant:** {result.get('klant_naam', sel['klant_naam'])}")
        st.markdown(f"**Analyse:** {sel['analyse_naam']}")
        st.markdown(f"**Ontvanger(s):** {', '.join(result['mail']['to'])}")
        st.markdown(f"**Onderwerp:** {result['mail'].get('subject', '—')}")
        if result.get("run_id"):
            st.caption(f"Run ID: `{result['run_id']}`")

        st.markdown("---")
        col_nieuw, col_home = st.columns(2)
        with col_nieuw:
            if st.button("Nog een analyse", use_container_width=True):
                st.session_state.stap = 2
                st.session_state.run_result = None
                st.rerun()
        with col_home:
            if st.button("Andere klant / analyse", use_container_width=True, type="primary"):
                st.session_state.stap = 1
                st.session_state.geselecteerd = None
                st.session_state.run_result = None
                st.rerun()

    with col_b:
        st.subheader("E-mail preview")
        html_body = result["mail"].get("html_body", "")
        if html_body:
            st.components.v1.html(html_body, height=600, scrolling=True)
        else:
            st.caption("Geen preview beschikbaar")
