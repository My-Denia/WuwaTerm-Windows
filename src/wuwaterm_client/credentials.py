"""Keyring wrapper for the single device credential this client stores.

The token is never written to disk directly by this application: this
module is the only place that touches it, and it delegates entirely to the
OS credential store via ``keyring`` (Windows Credential Manager on this
platform). config.py never sees the token.
"""

from __future__ import annotations

import inspect
from collections.abc import Callable
from typing import Any
from urllib.parse import urlsplit

import keyring
import keyring.errors

SERVICE_NAME = "WuwaTerm"
CREDENTIAL_USERNAME = "device-token"
_UNSET = object()


def credential_origin(base_url: str | None) -> str | None:
    """Normalize a server base URL to its origin (``scheme://host[:port]``).

    Returns ``None`` when ``base_url`` is empty, malformed, uses a scheme
    other than ``http``/``https``, or embeds userinfo. Default ports (80/443)
    are omitted and paths/queries/fragments are stripped so all URL forms for
    the same host/port share one keyring slot while different hosts or ports
    stay strictly isolated.
    """
    if not isinstance(base_url, str):
        return None
    cleaned = base_url.strip()
    if not cleaned:
        return None
    try:
        parts = urlsplit(cleaned)
        scheme = parts.scheme.lower()
        if scheme not in ("http", "https"):
            return None
        if parts.username is not None or parts.password is not None:
            return None
        host = parts.hostname
        if not host:
            return None
        port = parts.port
    except ValueError:
        return None
    host_text = f"[{host.lower()}]" if ":" in host else host.lower()
    is_default = (scheme == "http" and port == 80) or (scheme == "https" and port == 443)
    port_suffix = "" if port is None or is_default else f":{port}"
    return f"{scheme}://{host_text}{port_suffix}"


def credential_username(origin: Any = _UNSET) -> str | None:
    """Return the keyring account name for ``origin``.

    When ``origin`` is omitted (``_UNSET``), returns the legacy unscoped account
    name so direct unit helpers and one-time startup migration can address it.
    When an explicit ``origin`` argument is passed, it is bound strictly to
    ``device-token@{normalized_origin}`` and returns ``None`` if invalid or
    unconfigured (never falling back to the legacy unscoped slot).
    """
    if origin is _UNSET:
        return CREDENTIAL_USERNAME
    normalized = credential_origin(origin)
    if normalized is None:
        return None
    return f"{CREDENTIAL_USERNAME}@{normalized}"


def call_with_origin(func: Callable[..., Any], *args: Any, origin: str | None) -> Any:
    """Invoke ``func`` with ``origin=origin`` only if its signature accepts it.

    Test doubles frequently monkeypatch 0- or 1-argument lambdas onto UI views
    or ``ApiClient``; inspecting once keeps them working while production
    credential helpers always receive the active origin.
    """
    try:
        parameters = inspect.signature(func).parameters
    except (TypeError, ValueError):
        return func(*args)
    if "origin" in parameters or any(
        p.kind is inspect.Parameter.VAR_KEYWORD for p in parameters.values()
    ):
        return func(*args, origin=origin)
    return func(*args)


class CredentialStoreUnavailable(RuntimeError):
    """The OS credential store could not be used.

    The vault can be temporarily unavailable, and the backend then raises from
    wherever it was called: during start-up, before a window exists, or from
    inside a request, where it would bypass every view's error handling and
    leave the status line saying the work is still in progress. Callers get
    this instead, and decide what to show.
    """


# The backend is a native OS component, and it does not confine itself to
# keyring's own exception type: WinVaultKeyring re-raises `pywintypes.error`
# for a CredDelete that fails for any reason other than "not found", and that
# is not a KeyringError. This module is the only boundary to that component,
# and every failure of it means one thing to this application - the store
# could not be used - so every failure is normalized here rather than left to
# surprise a Qt callback.
def store_token(token: str, *, origin: Any = _UNSET) -> None:
    username = credential_username(origin)
    if username is None:
        raise ValueError("A valid server origin is required to store a device credential.")
    try:
        keyring.set_password(SERVICE_NAME, username, token)
    except Exception as exc:
        raise CredentialStoreUnavailable(str(exc)) from exc


def read_token(*, origin: Any = _UNSET) -> str | None:
    username = credential_username(origin)
    if username is None:
        return None
    try:
        return keyring.get_password(SERVICE_NAME, username)
    except Exception as exc:
        raise CredentialStoreUnavailable(str(exc)) from exc


def has_token(*, origin: Any = _UNSET) -> bool:
    """Whether a credential is stored. A store that cannot be read is not the
    same as an empty one, but for this question it has the same answer."""
    try:
        return read_token(origin=origin) is not None
    except CredentialStoreUnavailable:
        return False


def delete_token(*, origin: Any = _UNSET) -> None:
    username = credential_username(origin)
    if username is None:
        return
    try:
        keyring.delete_password(SERVICE_NAME, username)
    except keyring.errors.PasswordDeleteError:
        # There was nothing to delete. Forgetting a credential that is not
        # there is what the caller wanted anyway.
        pass
    except Exception as exc:
        # The vault itself is unavailable, which is a different thing: the
        # credential may still be there and the caller has to be told.
        raise CredentialStoreUnavailable(str(exc)) from exc


def migrate_legacy_token(base_url: str | None) -> bool:
    """Move a single legacy unscoped token to ``base_url``'s origin slot once.

    Runs at startup when a server address is configured and has no origin-scoped
    credential yet. Returns ``True`` if a legacy token was migrated and deleted.
    """
    normalized = credential_origin(base_url)
    if normalized is None:
        return False
    if read_token(origin=normalized) is not None:
        return False
    legacy = read_token()
    if legacy is None:
        return False
    store_token(legacy, origin=normalized)
    delete_token()
    return True


def active_backend_name() -> str:
    """Name of the keyring backend currently in effect, e.g.
    ``WinVaultKeyring`` on Windows, so the app can report where the
    credential lives."""
    return type(keyring.get_keyring()).__name__
