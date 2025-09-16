"""Utilities for interacting with Supabase from the application."""

from __future__ import annotations

import threading
from dataclasses import dataclass
from typing import Any, Dict, List, Optional, Tuple

from postgrest.exceptions import APIError
from supabase import Client, create_client


class SupabaseError(RuntimeError):
    """Base exception for Supabase related failures."""


class SupabaseConfigurationError(SupabaseError):
    """Raised when the Supabase client is not properly configured."""


class SupabaseUserExistsError(SupabaseError):
    """Raised when attempting to create a user that already exists."""


class SupabaseOperationError(SupabaseError):
    """Raised when an operation against Supabase fails."""


_cached_client: Optional[Client] = None
_client_signature: Optional[Tuple[str, str]] = None
_client_lock = threading.Lock()


def _is_placeholder(value: str) -> bool:
    if not value:
        return True
    markers = (
        "YOUR_SUPABASE",
        "SUPABASE_SERVICE_ROLE_KEY",
        "CHANGE_ME",
        "REPLACE_ME",
        "YOUR_PROJECT",
    )
    upper_value = value.upper()
    return any(marker in upper_value for marker in markers)


def _normalize_login(login: str) -> str:
    return (login or "").strip().lower()


def _get_client(url: str, key: str) -> Client:
    """Return a cached Supabase client, creating it if necessary."""

    if _is_placeholder(url) or _is_placeholder(key):
        raise SupabaseConfigurationError(
            "Credenciais do Supabase ausentes. Atualize SUPABASE_URL e SUPABASE_SERVICE_ROLE_KEY."
        )

    global _cached_client, _client_signature
    with _client_lock:
        signature = (url, key)
        if _cached_client is None or _client_signature != signature:
            try:
                _cached_client = create_client(url, key)
                _client_signature = signature
            except Exception as exc:  # pragma: no cover - depende de rede/configuração real
                raise SupabaseOperationError(
                    f"Não foi possível inicializar o cliente Supabase: {exc}"
                ) from exc
    return _cached_client


@dataclass
class UserRecord:
    id: Optional[str]
    name: Optional[str]
    email: Optional[str]
    password_hash: Optional[str]
    role: Optional[str]

    @classmethod
    def from_raw(cls, data: Dict[str, Any]) -> "UserRecord":
        return cls(
            id=data.get("id"),
            name=data.get("name"),
            email=data.get("email"),
            password_hash=data.get("password_hash") or data.get("pw"),
            role=data.get("role") or data.get("role_type"),
        )


def _handle_api_error(error: APIError) -> SupabaseError:
    message = error.message or "Erro de Supabase"
    details = (error.details or "").lower()
    combined = f"{message} {details}".lower()
    if error.code == "23505" or "duplicate" in combined or "already exists" in combined:
        return SupabaseUserExistsError("Usuário já cadastrado no Supabase.")
    return SupabaseOperationError(message)


def fetch_user_record(url: str, key: str, table: str, login: str) -> Optional[UserRecord]:
    """Fetch a user record by login identifier (email)."""

    identifier = _normalize_login(login)
    if not identifier:
        return None

    client = _get_client(url, key)
    try:
        response = (
            client.table(table)
            .select("id,name,email,password_hash,role")
            .eq("email", identifier)
            .limit(1)
            .execute()
        )
    except APIError as err:
        raise _handle_api_error(err) from err
    except Exception as exc:
        raise SupabaseOperationError(str(exc)) from exc

    data = response.data or []
    if data:
        return UserRecord.from_raw(data[0])

    try:
        response = (
            client.table(table)
            .select("id,name,email,password_hash,role")
            .eq("name", identifier)
            .limit(1)
            .execute()
        )
    except APIError as err:
        raise _handle_api_error(err) from err
    except Exception as exc:
        raise SupabaseOperationError(str(exc)) from exc

    data = response.data or []
    if data:
        return UserRecord.from_raw(data[0])
    return None


def create_user_record(
    url: str,
    key: str,
    table: str,
    *,
    login: str,
    password_hash: str,
    role: str,
    display_name: Optional[str] = None,
) -> UserRecord:
    """Create a new user record in Supabase."""

    identifier = _normalize_login(login)
    if not identifier:
        raise SupabaseOperationError("Login inválido para criação de usuário.")

    payload: Dict[str, Any] = {
        "name": display_name or identifier,
        "email": identifier,
        "password_hash": password_hash,
        "role": role,
    }

    client = _get_client(url, key)
    try:
        response = client.table(table).insert(payload).execute()
    except APIError as err:
        raise _handle_api_error(err) from err
    except Exception as exc:
        raise SupabaseOperationError(str(exc)) from exc

    data = response.data or [payload]
    return UserRecord.from_raw(data[0])


def fetch_users_by_role(
    url: str,
    key: str,
    table: str,
    role: str,
) -> List[UserRecord]:
    """Return a list of users filtered by Supabase role identifier."""

    client = _get_client(url, key)
    try:
        response = (
            client.table(table)
            .select("id,name,email,password_hash,role")
            .eq("role", role)
            .order("name", ascending=True)
            .execute()
        )
    except APIError as err:
        raise _handle_api_error(err) from err
    except Exception as exc:
        raise SupabaseOperationError(str(exc)) from exc

    users = response.data or []
    return [UserRecord.from_raw(item) for item in users]


def reset_cached_client() -> None:
    """Clear the cached Supabase client (useful for tests)."""

    global _cached_client, _client_signature
    with _client_lock:
        _cached_client = None
        _client_signature = None
