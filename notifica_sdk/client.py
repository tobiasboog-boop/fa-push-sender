"""
Notifica SDK — Client
=====================
Dunne HTTP wrapper rond de Notifica Data API.

Gebruik:
    from notifica_sdk import NotificaClient

    client = NotificaClient()  # leest uit .env
    df = client.query(1210, "SELECT * FROM ods.werkbonnen LIMIT 10")
"""

import os
import io
import pandas as pd
import requests

from .exceptions import (
    NotificaError, AuthError, PermissionError, ValidationError,
    TimeoutError, RateLimitError, ServerError,
)

# Probeer python-dotenv te laden (optioneel)
try:
    from dotenv import load_dotenv
    load_dotenv()
except ImportError:
    pass


def _key_to_header(key: str) -> dict:
    """Bepaal de juiste HTTP header op basis van het key prefix."""
    if key.startswith('nk_dwh_') or key.startswith('nk_csv_'):
        return {'X-Data-Key': key}
    return {'X-App-Key': key}


class NotificaClient:
    """Client voor de Notifica Data API.

    Configuratie via environment variabelen:
        NOTIFICA_API_URL   - Base URL (default: https://app.notifica.nl)
        NOTIFICA_DWH_KEY   - Customer Data Key voor DWH queries (nk_dwh_...)
        NOTIFICA_CSV_KEY   - Customer Data Key voor CSV bestanden (nk_csv_...) — optioneel
        NOTIFICA_DATA_KEY  - Legacy alias voor NOTIFICA_DWH_KEY
        NOTIFICA_APP_KEY   - App API Key (legacy/development) — fallback
    """

    def __init__(self, api_url: str = None, dwh_key: str = None, csv_key: str = None,
                 data_key: str = None):
        self.api_url = (api_url or os.getenv('NOTIFICA_API_URL', 'https://app.notifica.nl')).rstrip('/')

        # DWH key: expliciete arg > data_key alias > env vars
        self.dwh_key = (
            dwh_key
            or data_key
            or os.getenv('NOTIFICA_DWH_KEY', '')
            or os.getenv('NOTIFICA_DATA_KEY', '')
            or os.getenv('NOTIFICA_APP_KEY', '')
        )
        if not self.dwh_key:
            raise AuthError(
                "Geen API key gevonden. Zet NOTIFICA_DWH_KEY in je .env "
                "(of NOTIFICA_DATA_KEY / NOTIFICA_APP_KEY voor backward compatibility)."
            )

        # CSV key: optioneel
        self.csv_key = csv_key or os.getenv('NOTIFICA_CSV_KEY', '')

        # Detecteer header op basis van key prefix
        self._data_header = _key_to_header(self.dwh_key)
        self._csv_header = _key_to_header(self.csv_key) if self.csv_key else None

        self._session = requests.Session()
        self._session.headers.update({'Content-Type': 'application/json'})

    def _auth_headers(self, use_csv_key: bool = False) -> dict:
        if use_csv_key:
            if not self._csv_header:
                raise AuthError(
                    "Geen CSV key geconfigureerd. Zet NOTIFICA_CSV_KEY (nk_csv_...) in je .env."
                )
            return self._csv_header
        return self._data_header

    def _request(self, method: str, path: str, use_csv_key: bool = False, **kwargs) -> dict:
        url = f"{self.api_url}{path}"
        kwargs.setdefault('headers', {}).update(self._auth_headers(use_csv_key))
        try:
            resp = self._session.request(method, url, timeout=120, **kwargs)
        except requests.ConnectionError:
            raise ServerError(f"Kan niet verbinden met {self.api_url}. Is de API bereikbaar?")
        except requests.Timeout:
            raise TimeoutError("Request timeout bij verbinden met de API.")

        if resp.status_code == 200:
            content_type = resp.headers.get('content-type', '')
            if 'application/json' in content_type:
                return resp.json()
            return {'raw': resp.text}

        try:
            error_data = resp.json()
            error_msg = error_data.get('error', resp.text)
        except Exception:
            error_msg = resp.text

        if resp.status_code == 401:
            raise AuthError("Ongeldige API key.", status_code=401, detail=error_msg)
        elif resp.status_code == 403:
            raise PermissionError(f"Geen toegang: {error_msg}", status_code=403, detail=error_msg)
        elif resp.status_code == 400:
            raise ValidationError(f"Ongeldige request: {error_msg}", status_code=400, detail=error_msg)
        elif resp.status_code == 408:
            raise TimeoutError(f"Query timeout: {error_msg}", status_code=408, detail=error_msg)
        elif resp.status_code == 429:
            raise RateLimitError("Te veel requests.", status_code=429, detail=error_msg)
        else:
            raise ServerError(f"API fout ({resp.status_code}): {error_msg}", status_code=resp.status_code, detail=error_msg)

    def _raw_request(self, method: str, path: str, use_csv_key: bool = False, **kwargs) -> requests.Response:
        url = f"{self.api_url}{path}"
        kwargs.setdefault('headers', {}).update(self._auth_headers(use_csv_key))
        return self._session.request(method, url, timeout=120, **kwargs)

    def info(self) -> dict:
        return self._request('POST', '/api/data/info')

    def query(self, klantnummer: int, sql: str, max_rows: int = None) -> pd.DataFrame:
        """Voer een vrije SQL query uit. Retourneert een pandas DataFrame."""
        body = {'klantnummer': klantnummer, 'sql': sql}
        if max_rows:
            body['max_rows'] = max_rows
        result = self._request('POST', '/api/data/query', json=body)
        return pd.DataFrame(result.get('rows', []), columns=result.get('columns', []))

    def query_template(self, klantnummer: int, template_name: str, parameters: dict = None) -> pd.DataFrame:
        body = {'klantnummer': klantnummer}
        if parameters:
            body['parameters'] = parameters
        result = self._request('POST', f'/api/data/query/{template_name}', json=body)
        return pd.DataFrame(result.get('rows', []), columns=result.get('columns', []))

    def schema(self, klantnummer: int) -> dict:
        return self._request('GET', f'/api/data/schema/{klantnummer}')

    def write(self, klantnummer: int, sql: str) -> dict:
        return self._request('POST', '/api/data/write', json={'klantnummer': klantnummer, 'sql': sql})

    def write_template(self, klantnummer: int, template_name: str, parameters: dict = None) -> dict:
        body = {'klantnummer': klantnummer}
        if parameters:
            body['parameters'] = parameters
        return self._request('POST', f'/api/data/write/{template_name}', json=body)

    def csv_batches(self, klantnummer: int, days: int = None) -> list:
        params = {}
        if days:
            params['days'] = days
        use_csv = bool(self.csv_key)
        result = self._request('GET', f'/api/data/csv/{klantnummer}/batches', use_csv_key=use_csv, params=params)
        return result.get('batches', result) if isinstance(result, dict) else result

    def csv_files(self, klantnummer: int, date: str, folder: str) -> list:
        use_csv = bool(self.csv_key)
        result = self._request('GET', f'/api/data/csv/{klantnummer}/{date}/{folder}/files', use_csv_key=use_csv)
        return result.get('files', result) if isinstance(result, dict) else result

    def csv_download(self, klantnummer: int, date: str, folder: str, filename: str) -> pd.DataFrame:
        use_csv = bool(self.csv_key)
        resp = self._raw_request('GET', f'/api/data/csv/{klantnummer}/{date}/{folder}/{filename}', use_csv_key=use_csv)
        if resp.status_code != 200:
            try:
                error_msg = resp.json().get('error', resp.text)
            except Exception:
                error_msg = resp.text
            raise ServerError(f"CSV download mislukt: {error_msg}", status_code=resp.status_code)
        return pd.read_csv(io.StringIO(resp.text))

    def templates(self) -> list:
        result = self._request('GET', '/api/data/templates')
        return result.get('templates', result) if isinstance(result, dict) else result

    def register_template(self, name: str, sql: str, parameters: list = None,
                          description: str = None, target_type: str = 'dwh') -> dict:
        body = {'name': name, 'sql_template': sql, 'target_type': target_type}
        if parameters:
            body['parameters'] = parameters
        if description:
            body['description'] = description
        return self._request('POST', '/api/data/templates/register', json=body)
