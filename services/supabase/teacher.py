"""Funcionalidades de professores e owners de sala.

Perfis: professores responsáveis por ajustar a experiência das turmas.
"""

from __future__ import annotations

from typing import Any, Dict, List, Optional, Set

from postgrest.exceptions import APIError

from .common import (
    SupabaseOperationError,
    _fetch_users_map,
    _get_client,
    _handle_api_error,
    _normalize_chat_record,
    enrich_chats_with_evaluations,
)


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


def list_teacher_classroom_chats(
    url: str,
    key: str,
    *,
    teacher_id: str,
    users_table: str = "users",
) -> List[Dict[str, Any]]:
    """Return chats for classrooms where the teacher is a member."""

    if not teacher_id:
        raise SupabaseOperationError(
            "Identificador do professor ausente para consultar históricos de chat."
        )

    client = _get_client(url, key)

    try:
        memberships = (
            client.table("classroom_teachers")
            .select("classroom_id")
            .eq("teacher_id", teacher_id)
            .execute()
        )
    except APIError as err:
        raise _handle_api_error(err) from err
    except Exception as exc:
        raise SupabaseOperationError(str(exc)) from exc

    classroom_ids: Set[str] = {
        row.get("classroom_id")
        for row in (memberships.data or [])
        if row.get("classroom_id")
    }
    if not classroom_ids:
        return []

    try:
        chats_resp = (
            client.table("chats")
            .select(
                "id,student_id,classroom_id,content,summary,started_at,ended_at,subject_free_text,topic_source"
            )
            .in_("classroom_id", list(classroom_ids))
            .order("started_at", desc=True)
            .execute()
        )
    except APIError as err:
        raise _handle_api_error(err) from err
    except Exception as exc:
        raise SupabaseOperationError(str(exc)) from exc

    chat_rows = chats_resp.data or []
    if not chat_rows:
        return []

    student_ids: Set[Optional[str]] = {
        row.get("student_id") for row in chat_rows if row.get("student_id")
    }
    user_map = _fetch_users_map(client, student_ids, users_table=users_table)

    try:
        classrooms_resp = (
            client.table("classrooms")
            .select("id,name,theme_name")
            .in_("id", list(classroom_ids))
            .execute()
        )
    except APIError as err:
        raise _handle_api_error(err) from err
    except Exception as exc:
        raise SupabaseOperationError(str(exc)) from exc

    classroom_map: Dict[str, Dict[str, Optional[str]]] = {}
    for row in classrooms_resp.data or []:
        cid = row.get("id")
        if not cid:
            continue
        classroom_map[cid] = {
            "name": row.get("name"),
            "theme_name": row.get("theme_name"),
        }

    normalized = [
        _normalize_chat_record(
            row, classroom_map=classroom_map, user_map=user_map)
        for row in chat_rows
    ]

    return enrich_chats_with_evaluations(
        client, normalized, users_table=users_table
    )


__all__.append("list_teacher_classroom_chats")
