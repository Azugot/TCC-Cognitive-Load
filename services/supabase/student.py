"""Rotinas voltadas para fluxos de alunos e chats.

Perfis: estudantes autenticados e assistentes autorizados a registrar sessões.
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

from postgrest.exceptions import APIError

from .common import (
    SupabaseOperationError,
    _fetch_users_map,
    _get_client,
    _handle_api_error,
    _normalize_chat_record,
    _normalize_timestamp,
)


def create_chat_record(
    url: str,
    key: str,
    *,
    student_id: str,
    classroom_id: str,
    started_at: Any,
    ended_at: Any,
    chat_history: Optional[List[Dict[str, Any]]] = None,
    storage_chat_id: Optional[str] = None,
    storage_path_id: Optional[str] = None,
    storage_bucket: Optional[str] = None,
    storage_path: Optional[str] = None,
    chat_title: Optional[str] = None,
    subject_id: Optional[str] = None,
    subject_free_text: Optional[str] = None,
    topic_source: Optional[str] = None,
    summary: Optional[str] = None,
    subject_titles: Optional[List[str]] = None,
    student_goal: Optional[str] = None,
    student_interest: Optional[str] = None,
    is_adhoc_chat: bool = False,
    store_messages: bool = False,
    chats_table: str = "chats",
    chat_messages_table: str = "chat_messages",
) -> Dict[str, Any]:
    """Persist a chat session and its history on Supabase."""

    if not student_id:
        raise SupabaseOperationError("Identificador do aluno ausente para registrar chat.")
    if not classroom_id:
        raise SupabaseOperationError("Identificador da sala ausente para registrar chat.")

    storage_identifier = storage_path_id or storage_chat_id

    content_payload: Dict[str, Any] = {}
    if storage_identifier:
        content_payload["storage_path_id"] = storage_identifier
    if storage_bucket:
        content_payload["bucket"] = storage_bucket
    if storage_path:
        content_payload["path"] = storage_path
    if subject_titles:
        filtered_subjects = [
            str(title).strip()
            for title in (subject_titles or [])
            if isinstance(title, str) and str(title).strip()
        ]
        if filtered_subjects:
            content_payload["subjects"] = filtered_subjects
    if is_adhoc_chat and chat_title:
        content_payload["title"] = chat_title

    def _normalize_optional_text(value: Optional[str]) -> str:
        if value is None:
            return "None"
        text = str(value).strip()
        return text if text else "None"

    content_payload["student_goal"] = _normalize_optional_text(student_goal)
    content_payload["student_interest"] = _normalize_optional_text(student_interest)

    started_iso = _normalize_timestamp(started_at)
    ended_iso = _normalize_timestamp(ended_at)
    if not started_iso:
        started_iso = datetime.now(timezone.utc).isoformat()

    payload: Dict[str, Any] = {
        "student_id": student_id,
        "classroom_id": classroom_id,
        "topic_source": (topic_source or "").strip(),
        "started_at": started_iso,
    }
    if ended_iso:
        payload["ended_at"] = ended_iso
    if subject_id:
        payload["subject_id"] = subject_id
    if subject_free_text is not None:
        payload["subject_free_text"] = str(subject_free_text).strip()
    if summary:
        payload["summary"] = summary
    if content_payload:
        payload["content"] = content_payload

    client = _get_client(url, key)
    try:
        response = client.table(chats_table).insert(payload).execute()
    except APIError as err:
        raise _handle_api_error(err) from err
    except Exception as exc:
        raise SupabaseOperationError(str(exc)) from exc

    chat_rows = response.data or []
    chat_record = chat_rows[0] if chat_rows else payload

    result: Dict[str, Any] = {"chat": chat_record}

    if store_messages and chat_history:
        messages_payload = []
        for message in chat_history:
            if not isinstance(message, dict):
                continue
            entry = {
                "chat_id": chat_record.get("id"),
                "role": message.get("role"),
                "content": message.get("content"),
                "ts": message.get("ts"),
            }
            messages_payload.append(entry)
        if messages_payload and chat_record.get("id"):
            try:
                client.table(chat_messages_table).insert(messages_payload).execute()
            except APIError as err:
                raise _handle_api_error(err) from err
            except Exception as exc:
                raise SupabaseOperationError(str(exc)) from exc

    return result


__all__ = ["create_chat_record"]


def list_student_chats(
    url: str,
    key: str,
    *,
    student_id: str,
    users_table: str = "users",
) -> List[Dict[str, Any]]:
    """Return chats registered by a given student."""

    if not student_id:
        raise SupabaseOperationError(
            "Identificador do aluno ausente para consultar históricos de chat."
        )

    client = _get_client(url, key)

    try:
        chats_resp = (
            client.table("chats")
            .select(
                "id,student_id,classroom_id,content,summary,started_at,ended_at,subject_free_text,topic_source"
            )
            .eq("student_id", student_id)
            .order("started_at", desc=True)
            .execute()
        )
    except APIError as err:
        raise _handle_api_error(err) from err
    except Exception as exc:
        raise SupabaseOperationError(str(exc)) from exc

    rows = chats_resp.data or []
    if not rows:
        return []

    class_ids = {row.get("classroom_id") for row in rows if row.get("classroom_id")}

    classroom_map: Dict[str, Dict[str, Any]] = {}
    if class_ids:
        try:
            classrooms_resp = (
                client.table("classrooms")
                .select("id,name,theme_name")
                .in_("id", list(class_ids))
                .execute()
            )
        except APIError as err:
            raise _handle_api_error(err) from err
        except Exception as exc:
            raise SupabaseOperationError(str(exc)) from exc

        for row in classrooms_resp.data or []:
            cid = row.get("id")
            if not cid:
                continue
            classroom_map[cid] = {
                "name": row.get("name"),
                "theme_name": row.get("theme_name"),
            }

    user_map = _fetch_users_map(client, {student_id}, users_table=users_table)

    normalized = [
        _normalize_chat_record(row, classroom_map=classroom_map, user_map=user_map)
        for row in rows
    ]
    return normalized


__all__.append("list_student_chats")
