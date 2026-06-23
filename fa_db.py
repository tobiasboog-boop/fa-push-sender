"""
FA App DB — leest configuratie uit app_financiele_analyse (VPS4)
en schrijft run-resultaten terug voor het audit trail.
"""
import os
import json
import uuid
from contextlib import contextmanager

import psycopg2
import psycopg2.extras


def _conn_params():
    return dict(
        host=os.environ.get("APP_DB_HOST", "10.3.152.8"),
        port=int(os.environ.get("APP_DB_PORT", 5532)),
        dbname=os.environ.get("APP_DB_NAME", "notifica_app"),
        user=os.environ.get("APP_DB_USER", "notifica_app"),
        password=os.environ["APP_DB_PASSWORD"],
        options="-c search_path=app_financiele_analyse,public",
        connect_timeout=10,
    )


@contextmanager
def _db():
    con = psycopg2.connect(**_conn_params())
    try:
        yield con
        con.commit()
    except Exception:
        con.rollback()
        raise
    finally:
        con.close()


# ---------------------------------------------------------------------------
# Lezen
# ---------------------------------------------------------------------------

def get_actieve_configs():
    """
    Geeft alle actieve klant/analyse-combinaties terug inclusief
    de datum van de laatste succesvolle run.
    """
    sql = """
        SELECT
            kac.klantnummer,
            kac.analyse_id,
            kac.delivery_ontvangers,
            kac.sql_overrides,
            kac.leesinstructie,
            a.code  AS analyse_code,
            a.naam  AS analyse_naam,
            r.id    AS laatste_run_id,
            r.created_at AS laatste_run_datum
        FROM klant_analyse_config kac
        JOIN analyses a ON a.id = kac.analyse_id
        LEFT JOIN LATERAL (
            SELECT id, created_at
            FROM runs
            WHERE klantnummer = kac.klantnummer
              AND analyse_id  = kac.analyse_id
              AND status = 'done'
            ORDER BY created_at DESC
            LIMIT 1
        ) r ON true
        WHERE kac.enabled = true
        ORDER BY kac.klantnummer, a.naam
    """
    with _db() as con:
        with con.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
            cur.execute(sql)
            return [dict(r) for r in cur.fetchall()]


def get_alle_analyses() -> list[dict]:
    """Geeft alle actieve analyses terug (niet gefilterd op klant)."""
    sql = """
        SELECT
            a.id   AS analyse_id,
            a.code AS analyse_code,
            a.naam AS analyse_naam
        FROM analyses a
        JOIN dossier_definities dd ON dd.id = a.active_dossier_definitie_id
        JOIN prompt_versies pv     ON pv.id = a.active_prompt_id
        ORDER BY a.naam
    """
    with _db() as con:
        with con.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
            cur.execute(sql)
            return [dict(r) for r in cur.fetchall()]


def get_analyse_config(klantnummer: int, analyse_id: str) -> dict:
    """
    Haalt de volledige pipeline-configuratie op voor één klant/analyse.
    klant_analyse_config is optioneel: als er geen entry bestaat worden
    analyse-defaults gebruikt (lege sql_overrides, geen vaste ontvangers).
    """
    sql = """
        SELECT
            a.id                        AS analyse_id,
            a.code,
            a.naam                      AS analyse_naam,
            a.delivery_toon,
            -- Dossier / SQL
            ddv.id                      AS dossier_definitie_versie_id,
            ddv.query_sql,
            ddv.sql_defaults,
            -- Prompt
            pv.id                       AS prompt_versie_id,
            pv.template                 AS prompt_template,
            pv.model,
            pv.temperature,
            pv.max_output_tokens,
            -- Delivery template
            dtv.id                      AS delivery_template_versie_id,
            dtv.rendering_template,
            dtv.output_schema           AS delivery_output_schema,
            dtv.email_config,
            dtv.toon_instructie,
            -- Klantconfig (optioneel — NULL als geen config bestaat)
            kac.leesinstructie,
            kac.sql_overrides,
            kac.delivery_ontvangers,
            -- Resend key
            dm.provider_config          AS resend_provider_config
        FROM analyses a
        JOIN dossier_definities dd
          ON dd.id = a.active_dossier_definitie_id
        JOIN dossier_definitie_versies ddv
          ON ddv.id = dd.active_versie_id
        JOIN prompt_versies pv
          ON pv.id = a.active_prompt_id
        LEFT JOIN delivery_templates dt
          ON dt.id = a.active_delivery_template_id
        LEFT JOIN delivery_template_versies dtv
          ON dtv.id = dt.active_versie_id
        LEFT JOIN delivery_methoden dm
          ON dm.code = 'email-resend'
        LEFT JOIN klant_analyse_config kac
          ON kac.analyse_id = a.id AND kac.klantnummer = %s AND kac.enabled = true
        WHERE a.id = %s
    """
    with _db() as con:
        with con.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
            cur.execute(sql, (klantnummer, analyse_id))
            row = cur.fetchone()
            if not row:
                raise ValueError(
                    f"Analyse {analyse_id} niet gevonden of niet actief"
                )
            return dict(row)


def get_laatste_run_output(klantnummer: int, analyse_id: str) -> dict | None:
    """
    Geeft de output van de laatste succesvolle run terug
    (voor het 'hergebruik' pad zonder nieuwe LLM-call).
    """
    sql = """
        SELECT
            r.id        AS run_id,
            r.created_at,
            ao.output_json
        FROM runs r
        JOIN analyse_outputs ao ON ao.run_id = r.id
        WHERE r.klantnummer = %s
          AND r.analyse_id  = %s
          AND r.status = 'done'
        ORDER BY r.created_at DESC
        LIMIT 1
    """
    with _db() as con:
        with con.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
            cur.execute(sql, (klantnummer, analyse_id))
            row = cur.fetchone()
            return dict(row) if row else None


def get_resend_api_key() -> str:
    """Leest de Resend API-key uit de delivery_methoden tabel."""
    sql = """
        SELECT provider_config->>'api_key' AS api_key
        FROM delivery_methoden
        WHERE code = 'email-resend'
        LIMIT 1
    """
    with _db() as con:
        with con.cursor() as cur:
            cur.execute(sql)
            row = cur.fetchone()
            if row and row[0]:
                return row[0]
    # Fallback naar env var
    key = os.environ.get("RESEND_API_KEY", "")
    if not key:
        raise RuntimeError("Geen Resend API-key gevonden (DB of RESEND_API_KEY env)")
    return key


# ---------------------------------------------------------------------------
# Schrijven (audit trail)
# ---------------------------------------------------------------------------

def save_run(klantnummer: int, analyse_id: str, config: dict,
             snapshot_data: dict, llm_response: str, output_json: dict) -> str:
    """Slaat een volledige run op in het FA-app audit trail. Geeft run_id terug."""
    run_id = str(uuid.uuid4())

    with _db() as con:
        with con.cursor() as cur:
            # 1. Run-rij
            cur.execute(
                """
                INSERT INTO runs
                    (id, klantnummer, analyse_id, status, mode,
                     prompt_versie_id, dossier_definitie_versie_id)
                VALUES (%s, %s, %s, 'done', 'official', %s, %s)
                """,
                (
                    run_id,
                    klantnummer,
                    analyse_id,
                    config.get("prompt_versie_id"),
                    config.get("dossier_definitie_versie_id"),
                ),
            )

            # 2. Dossier (snapshot)
            cur.execute(
                """
                INSERT INTO dossiers (run_id, snapshot_data, dwh_query_executed)
                VALUES (%s, %s, %s)
                """,
                (
                    run_id,
                    json.dumps(snapshot_data),
                    snapshot_data.get("sql_executed", ""),
                ),
            )

            # 3. Analyse output
            cur.execute(
                """
                INSERT INTO analyse_outputs (run_id, output_json)
                VALUES (%s, %s)
                """,
                (run_id, json.dumps(output_json)),
            )

    return run_id
