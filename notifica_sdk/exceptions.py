"""
Notifica SDK — Exceptions
=========================
Duidelijke foutmeldingen voor veelvoorkomende API fouten.
"""


class NotificaError(Exception):
    """Basis exception voor alle Notifica SDK fouten."""

    def __init__(self, message: str, status_code: int = None, detail: str = None):
        self.status_code = status_code
        self.detail = detail
        super().__init__(message)


class AuthError(NotificaError):
    """Ongeldige of ontbrekende API key."""
    pass


class PermissionError(NotificaError):
    """Geen toegang tot deze klant (check app_permissions)."""
    pass


class ValidationError(NotificaError):
    """Ongeldige query of parameters."""
    pass


class TimeoutError(NotificaError):
    """Query timeout — vereenvoudig de query of verhoog timeout in App Beheer."""
    pass


class RateLimitError(NotificaError):
    """Te veel requests — max 60 per minuut."""
    pass


class ServerError(NotificaError):
    """Server-side fout bij de Notifica API."""
    pass
