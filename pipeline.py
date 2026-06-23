"""
Pipeline — voert een financiële analyse uit end-to-end:
  1. Render SQL met Jinja2 (sql_defaults + sql_overrides)
  2. Haal data op via Notifica Data API
  3. Bouw LLM-prompt op
  4. Roep Claude aan
  5. Parseer JSON-output
  6. Sla op in FA app DB (audit trail)
"""
import json
import os
import re
import textwrap
from datetime import datetime

import anthropic
from jinja2 import Environment, StrictUndefined

import fa_db

# ---------------------------------------------------------------------------
# Systeem-prompt (overgenomen uit run_executor — SQL-first modus)
# ---------------------------------------------------------------------------

SYSTEM_PROMPT = textwrap.dedent("""\
    Je bent een financieel-bedrijfskundig analist voor installatietechnische bedrijven.
    Je analyseert data die als JSON aangeleverd wordt en schrijft een beknopte,
    directie-gerichte analyse in het Nederlands.

    Regels:
    - Schrijf altijd in het Nederlands.
    - Vermijd vakjargon dat installateurs niet kennen (geen ERP/DSO/aging/P&L).
    - Signaleer concreet: benoem het kerngetal, dan de betekenis.
    - De hoofdactie is max 25 woorden.
    - Toon geen berekeningen, alleen de conclusie.
    - Output ALLEEN geldige JSON die voldoet aan het opgegeven schema.
    - Geen tekst buiten de JSON.
""")


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _render_sql(query_sql: str, sql_defaults: dict | None, sql_overrides: dict | None) -> str:
    """Rendert het Jinja2 SQL-template met defaults en klant-overrides."""
    context = {**(sql_defaults or {}), **(sql_overrides or {})}
    env = Environment(undefined=StrictUndefined)
    try:
        return env.from_string(query_sql).render(**context)
    except Exception as exc:
        raise ValueError(f"SQL-template render fout: {exc}") from exc


def _snapshot_to_text(snapshot: dict) -> str:
    """Zet snapshot {columns, rows} om naar een leesbare tekst voor de LLM."""
    columns = snapshot.get("columns", [])
    rows = snapshot.get("rows", [])
    row_count = snapshot.get("row_count", len(rows))
    truncated = snapshot.get("truncated", False)

    lines = [f"Datapunten: {row_count} rijen{' (afgekapt)' if truncated else ''}"]
    lines.append("Kolommen: " + ", ".join(str(c) for c in columns))
    lines.append("")

    # Compact tabelweergave (max 200 rijen in prompt)
    display_rows = rows[:200]
    for row in display_rows:
        lines.append(json.dumps(dict(zip(columns, row)), ensure_ascii=False, default=str))

    return "\n".join(lines)


def _parse_llm_json(text: str) -> dict:
    """Haalt JSON uit LLM-response (verwijdert markdown code fences e.d.)."""
    # Verwijder code fences
    text = re.sub(r"^```(?:json)?\s*", "", text.strip(), flags=re.IGNORECASE)
    text = re.sub(r"\s*```$", "", text.strip())
    try:
        return json.loads(text)
    except json.JSONDecodeError as exc:
        # Zoek naar eerste { … } blok
        match = re.search(r"\{.*\}", text, re.DOTALL)
        if match:
            return json.loads(match.group())
        raise ValueError(f"Kon geen JSON parsen uit LLM-response: {exc}") from exc


# ---------------------------------------------------------------------------
# Notifica Data API wrapper
# ---------------------------------------------------------------------------

def _query_dwh(klantnummer: int, sql: str) -> dict:
    """
    Voert SQL uit via de Notifica Data API.
    Retourneert {columns, rows, row_count, sql_executed}.
    """
    import sys
    import importlib

    # Probeer de SDK te importeren (staat in notifica_app/_sdk bij lokale dev)
    sdk_path = os.environ.get(
        "NOTIFICA_SDK_PATH",
        r"c:\projects\notifica_app\apps\_sdk",
    )
    if sdk_path not in sys.path:
        sys.path.insert(0, sdk_path)

    try:
        notifica_sdk = importlib.import_module("notifica_sdk")
        NotificaClient = notifica_sdk.NotificaClient
    except ImportError as exc:
        raise ImportError(
            "Notifica SDK niet gevonden. Zet NOTIFICA_SDK_PATH in .env."
        ) from exc

    data_key = os.environ.get("NOTIFICA_DATA_KEY") or os.environ.get("NOTIFICA_DWH_KEY")
    if not data_key:
        raise ValueError("NOTIFICA_DATA_KEY ontbreekt in .env")

    client = NotificaClient(data_key=data_key)
    result = client.query(klantnummer, sql, max_rows=500)

    # SDK kan dict of list teruggeven — normaliseer
    if isinstance(result, list):
        # Oudere SDK-versie: list of dicts
        if not result:
            return {"columns": [], "rows": [], "row_count": 0, "sql_executed": sql}
        columns = list(result[0].keys())
        rows = [[row[c] for c in columns] for row in result]
        return {"columns": columns, "rows": rows, "row_count": len(rows), "sql_executed": sql}
    elif isinstance(result, dict):
        result.setdefault("sql_executed", sql)
        return result
    else:
        raise TypeError(f"Onverwacht type van Data API: {type(result)}")


# ---------------------------------------------------------------------------
# Klant-naam ophalen
# ---------------------------------------------------------------------------

def get_klant_naam(klantnummer: int) -> str:
    """Haalt de klantnaam op uit de Notifica Data API."""
    try:
        result = _query_dwh(
            klantnummer,
            'SELECT TRIM("Administratienaam") FROM notifica."SSM Administraties" '
            'WHERE "AdministratieKey" = 1 LIMIT 1',
        )
        rows = result.get("rows", [])
        if rows:
            return str(rows[0][0])
    except Exception:
        pass
    return f"Klant {klantnummer}"


# ---------------------------------------------------------------------------
# Hoofd-pipeline
# ---------------------------------------------------------------------------

def run_analyse(klantnummer: int, analyse_id: str, sla_op: bool = True) -> dict:
    """
    Voert de volledige pipeline uit voor één klant/analyse-combinatie.

    Returns:
        {
          run_id: str | None,
          output_json: dict,
          snapshot: dict,
          analyse_naam: str,
          klant_naam: str,
        }
    """
    # 1. Config laden
    config = fa_db.get_analyse_config(klantnummer, analyse_id)

    analyse_naam = config["analyse_naam"]
    klant_naam = get_klant_naam(klantnummer)

    # 2. SQL renderen
    rendered_sql = _render_sql(
        config["query_sql"],
        config.get("sql_defaults"),
        config.get("sql_overrides"),
    )

    # 3. Data ophalen
    snapshot = _query_dwh(klantnummer, rendered_sql)

    # 4. Output-schema bepalen (delivery template is leidend)
    output_schema = config.get("delivery_output_schema") or {}
    output_schema_str = json.dumps(output_schema, ensure_ascii=False, indent=2)

    # 5. Prompt opbouwen
    leesinstructie = (config.get("leesinstructie") or "").strip()
    toon_instructie = (config.get("toon_instructie") or "").strip()

    snapshot_text = _snapshot_to_text(snapshot)

    # Render de prompt-template (Jinja2) met snapshot-context
    prompt_template = config.get("prompt_template") or ""
    env = Environment()
    try:
        prompt_body = env.from_string(prompt_template).render(
            snapshot=snapshot_text,
            klantnummer=klantnummer,
            klant_naam=klant_naam,
            peildatum=datetime.now().strftime("%B %Y"),
        )
    except Exception:
        prompt_body = prompt_template  # gebruik ongerenderd als fallback

    user_message = "\n\n".join(filter(None, [
        leesinstructie,
        prompt_body,
        f"Data:\n{snapshot_text}",
        toon_instructie,
        f"Geef je output als JSON volgens dit schema:\n{output_schema_str}",
    ]))

    # 6. Claude aanroepen
    model = config.get("model") or "claude-sonnet-4-6"
    temperature = float(config.get("temperature") or 0.0)
    max_tokens = int(config.get("max_output_tokens") or 4096)

    client = anthropic.Anthropic(api_key=os.environ["ANTHROPIC_API_KEY"])
    response = client.messages.create(
        model=model,
        max_tokens=max_tokens,
        temperature=temperature,
        system=SYSTEM_PROMPT,
        messages=[{"role": "user", "content": user_message}],
    )

    llm_text = response.content[0].text if response.content else ""

    # 7. JSON parsen
    output_json = _parse_llm_json(llm_text)

    # 8. Opslaan in FA app DB (audit trail)
    run_id = None
    if sla_op:
        try:
            run_id = fa_db.save_run(
                klantnummer=klantnummer,
                analyse_id=analyse_id,
                config=config,
                snapshot_data=snapshot,
                llm_response=llm_text,
                output_json=output_json,
            )
        except Exception as exc:
            # Niet-fataal: audit trail mislukking stopt de verzending niet
            print(f"[WAARSCHUWING] Run opslaan mislukt: {exc}")

    return {
        "run_id": run_id,
        "output_json": output_json,
        "snapshot": snapshot,
        "analyse_naam": analyse_naam,
        "klant_naam": klant_naam,
        "config": config,
    }
