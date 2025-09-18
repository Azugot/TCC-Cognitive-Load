"""Funcionalidades de professores e owners de sala.

Perfis: professores responsáveis por ajustar a experiência das turmas.
"""

from __future__ import annotations

from typing import Any, Dict

from postgrest.exceptions import APIError

from .common import SupabaseOperationError, _get_client, _handle_api_error


def set_classroom_theme_config(
    url: str,
    key: str,
    *,
    classroom_id: str,
    theme_config: Dict[str, Any],
) -> None:
    """Persist the theme configuration for a classroom."""

    client = _get_client(url, key)
    try:
        client.table("classrooms").update({"theme_config": theme_config}).eq(
            "id", classroom_id
        ).execute()
    except APIError as err:
        raise _handle_api_error(err) from err
    except Exception as exc:
        raise SupabaseOperationError(str(exc)) from exc


__all__ = ["set_classroom_theme_config"]
