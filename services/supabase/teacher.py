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


def create_classroom_document_record(
    url: str,
    key: str,
    *,
    classroom_id: str,
    name: str,
    storage_path: str,
    uploaded_by: Optional[str] = None,
    description: Optional[str] = None,
) -> Dict[str, Any]:
    """Register a classroom document entry in Supabase."""

    if not classroom_id:
        raise SupabaseOperationError("Sala não informada para registrar o documento.")
    if not storage_path:
        raise SupabaseOperationError("Caminho do documento não informado.")
    if not uploaded_by:
        raise SupabaseOperationError(
            "Usuário responsável pelo upload não informado."
        )

    client = _get_client(url, key)

    payload: Dict[str, Any] = {
        "classroom_id": classroom_id,
        "file_name": (name or "Documento").strip() or "Documento",
        "storage_path": storage_path,
        "uploaded_by": uploaded_by,
    }
    if description is not None:
        payload["description"] = (description or "").strip() or None

    try:
        resp = (
            client.table("classroom_documents")
            .insert(payload)
            .execute()
        )
    except APIError as err:
        raise _handle_api_error(err) from err
    except Exception as exc:
        raise SupabaseOperationError(str(exc)) from exc

    data = getattr(resp, "data", None) if not isinstance(resp, dict) else resp.get("data")
    document: Dict[str, Any] = {}
    if isinstance(data, list) and data:
        document = data[0]
    elif isinstance(resp, dict):
        document = resp

    if document and "name" not in document:
        document["name"] = document.get("file_name")

    return document


__all__.append("create_classroom_document_record")


def update_classroom_document_record(
    url: str,
    key: str,
    *,
    document_id: str,
    name: Optional[str] = None,
    description: Optional[str] = None,
) -> Dict[str, Any]:
    """Update classroom document metadata."""

    if not document_id:
        raise SupabaseOperationError("Documento não informado para atualização.")

    updates: Dict[str, Any] = {}
    if name is not None:
        updates["file_name"] = (name or "Documento").strip() or "Documento"
    if description is not None:
        updates["description"] = (description or "").strip() or None

    if not updates:
        return {}

    client = _get_client(url, key)

    try:
        resp = (
            client.table("classroom_documents")
            .update(updates)
            .eq("id", document_id)
            .execute()
        )
    except APIError as err:
        raise _handle_api_error(err) from err
    except Exception as exc:
        raise SupabaseOperationError(str(exc)) from exc

    data = getattr(resp, "data", None) if not isinstance(resp, dict) else resp.get("data")
    document: Dict[str, Any] = {}
    if isinstance(data, list) and data:
        document = data[0]
    elif isinstance(resp, dict):
        document = resp

    if document and "name" not in document:
        document["name"] = document.get("file_name")

    return document


__all__.append("update_classroom_document_record")


def delete_classroom_document_record(
    url: str,
    key: str,
    *,
    document_id: str,
) -> None:
    """Remove a classroom document metadata entry."""

    if not document_id:
        raise SupabaseOperationError("Documento não informado para exclusão.")

    client = _get_client(url, key)

    try:
        client.table("classroom_documents").delete().eq("id", document_id).execute()
    except APIError as err:
        raise _handle_api_error(err) from err
    except Exception as exc:
        raise SupabaseOperationError(str(exc)) from exc


__all__.append("delete_classroom_document_record")
