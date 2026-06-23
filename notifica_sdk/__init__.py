"""
Notifica SDK
============
Python client voor de Notifica Data API.

Gebruik:
    from notifica_sdk import NotificaClient

    client = NotificaClient()  # leest NOTIFICA_API_URL + NOTIFICA_DWH_KEY uit .env
    df = client.query(1210, "SELECT * FROM ods.werkbonnen LIMIT 10")
"""

from .client import NotificaClient
from .exceptions import (
    NotificaError,
    AuthError,
    PermissionError,
    ValidationError,
    TimeoutError,
    RateLimitError,
    ServerError,
)

__version__ = '0.1.0'
__all__ = [
    'NotificaClient',
    'NotificaError',
    'AuthError',
    'PermissionError',
    'ValidationError',
    'TimeoutError',
    'RateLimitError',
    'ServerError',
]
