"""Operações administrativas sobre salas e membros.

Perfis: administradores da plataforma e responsáveis com acesso completo.
"""

from __future__ import annotations

from typing import Any, Dict, List, Optional, Set, Tuple

from postgrest.exceptions import APIError

from .common import (
    SupabaseOperationError,
    _fetch_users_map,
    _get_client,
    _handle_api_error,
    _normalize_chat_record,
    enrich_chats_with_evaluations,
)


def fetch_classroom_domain(
    url: str,
    key: str,
    *,
    users_table: str = "users",
) -> Tuple[List[Dict[str, Any]], Dict[str, List[Dict[str, Any]]]]:
    """Fetch classrooms along with teachers, students and subjects."""

    client = _get_client(url, key)
    try:
        classrooms_resp = (
            client.table("classrooms")
            .select(
                "id,name,description,theme_name,theme_config,theme_locked,is_archived,created_by"
            )
            .order("name")
            .execute()
        )
    except APIError as err:
        raise _handle_api_error(err) from err
    except Exception as exc:
        raise SupabaseOperationError(str(exc)) from exc

    classrooms_raw = classrooms_resp.data or []
    if not classrooms_raw:
        return [], {}

    classroom_ids = [row.get("id") for row in classrooms_raw if row.get("id")]
    if not classroom_ids:
        return [], {}

    try:
        teachers_resp = (
            client.table("classroom_teachers")
            .select("classroom_id,teacher_id,role_label")
            .in_("classroom_id", classroom_ids)
            .execute()
        )
    except APIError as err:
        raise _handle_api_error(err) from err
    except Exception as exc:
        raise SupabaseOperationError(str(exc)) from exc
    teacher_rows = teachers_resp.data or []

    try:
        students_resp = (
            client.table("classroom_students")
            .select("classroom_id,student_id,status")
            .in_("classroom_id", classroom_ids)
            .execute()
        )
    except APIError as err:
        raise _handle_api_error(err) from err
    except Exception as exc:
        raise SupabaseOperationError(str(exc)) from exc
    student_rows = students_resp.data or []

    try:
        subjects_resp = (
            client.table("classroom_subjects")
            .select("id,classroom_id,name,is_active")
            .in_("classroom_id", classroom_ids)
            .execute()
        )
    except APIError as err:
        raise _handle_api_error(err) from err
    except Exception as exc:
        raise SupabaseOperationError(str(exc)) from exc
    subject_rows = subjects_resp.data or []

    try:
        documents_resp = (
            client.table("classroom_documents")
            .select(
                "id,classroom_id,name,storage_bucket,storage_path,content_type,file_size,uploaded_by,created_at,updated_at"
            )
            .in_("classroom_id", classroom_ids)
            .execute()
        )
    except APIError as err:
        raise _handle_api_error(err) from err
    except Exception as exc:
        raise SupabaseOperationError(str(exc)) from exc
    document_rows = documents_resp.data or []

    user_ids: Set[Optional[str]] = set()
    for row in teacher_rows:
        user_ids.add(row.get("teacher_id"))
    for row in student_rows:
        user_ids.add(row.get("student_id"))
    for row in classrooms_raw:
        user_ids.add(row.get("created_by"))
    for row in document_rows:
        user_ids.add(row.get("uploaded_by"))

    user_map = _fetch_users_map(client, user_ids, users_table=users_table)

    teachers_by_class: Dict[str, List[Dict[str, Any]]] = {}
    for row in teacher_rows:
        cid = row.get("classroom_id")
        tid = row.get("teacher_id")
        if not cid or not tid:
            continue
        info = user_map.get(tid, {})
        entry = {
            "user_id": tid,
            "login": info.get("login"),
            "username": info.get("username"),
            "display_name": info.get("display_name"),
            "role_label": row.get("role_label"),
        }
        teachers_by_class.setdefault(cid, []).append(entry)

    students_by_class: Dict[str, List[Dict[str, Any]]] = {}
    for row in student_rows:
        cid = row.get("classroom_id")
        sid = row.get("student_id")
        if not cid or not sid:
            continue
        info = user_map.get(sid, {})
        entry = {
            "user_id": sid,
            "login": info.get("login"),
            "username": info.get("username"),
            "display_name": info.get("display_name"),
            "status": row.get("status"),
        }
        students_by_class.setdefault(cid, []).append(entry)

    subjects_by_class: Dict[str, List[Dict[str, Any]]] = {}
    for row in subject_rows:
        cid = row.get("classroom_id")
        sid = row.get("id")
        if not cid or not sid:
            continue
        subjects_by_class.setdefault(cid, []).append(
            {
                "id": sid,
                "classroom_id": cid,
                "name": row.get("name"),
                "is_active": bool(row.get("is_active", True)),
            }
        )

    for entries in teachers_by_class.values():
        entries.sort(key=lambda item: item.get("login") or "")
    for entries in students_by_class.values():
        entries.sort(key=lambda item: item.get("login") or "")
    for entries in subjects_by_class.values():
        entries.sort(key=lambda item: (item.get("name") or "").lower())

    documents_by_class: Dict[str, List[Dict[str, Any]]] = {}
    for row in document_rows:
        cid = row.get("classroom_id")
        did = row.get("id")
        if not cid or not did:
            continue
        uploader = user_map.get(row.get("uploaded_by"), {})
        entry = {
            "id": did,
            "classroom_id": cid,
            "name": row.get("name"),
            "storage_bucket": row.get("storage_bucket"),
            "storage_path": row.get("storage_path"),
            "content_type": row.get("content_type"),
            "file_size": row.get("file_size"),
            "uploaded_by": row.get("uploaded_by"),
            "uploaded_by_login": uploader.get("login"),
            "uploaded_by_username": uploader.get("username"),
            "uploaded_by_name": uploader.get("display_name") or uploader.get("username") or uploader.get("login"),
            "created_at": row.get("created_at"),
            "updated_at": row.get("updated_at"),
        }
        documents_by_class.setdefault(cid, []).append(entry)

    for entries in documents_by_class.values():
        entries.sort(key=lambda item: (item.get("name") or "").lower())

    classrooms: List[Dict[str, Any]] = []
    for raw in classrooms_raw:
        cid = raw.get("id")
        if not cid:
            continue
        teacher_entries = teachers_by_class.get(cid, [])
        student_entries = students_by_class.get(cid, [])
        owner_login = None
        owner_id = None
        owner_username = None
        for entry in teacher_entries:
            if (entry.get("role_label") or "").lower() == "owner":
                owner_id = entry.get("user_id")
                owner_login = entry.get("login")
                owner_username = entry.get("username")
                break
        if not owner_login and raw.get("created_by"):
            creator_info = user_map.get(raw.get("created_by"))
            if creator_info:
                owner_login = creator_info.get("login") or owner_login
                owner_username = (
                    creator_info.get("username")
                    or creator_info.get("login")
                    or owner_username
                )

        classrooms.append(
            {
                "id": cid,
                "name": raw.get("name"),
                "description": raw.get("description"),
                "theme_name": raw.get("theme_name"),
                "theme_config": raw.get("theme_config") or {},
                "theme_locked": bool(raw.get("theme_locked")),
                "is_archived": bool(raw.get("is_archived")),
                "created_by": raw.get("created_by"),
                "owner_id": owner_id,
                "owner_login": owner_login,
                "owner_username": owner_username,
                "teachers": teacher_entries,
                "students": student_entries,
                "documents": documents_by_class.get(cid, []),
            }
        )

    return classrooms, subjects_by_class


def list_all_chats(
    url: str,
    key: str,
    *,
    limit: Optional[int] = 200,
    users_table: str = "users",
) -> List[Dict[str, Any]]:
    """Return chats across all classrooms (admin scope)."""

    client = _get_client(url, key)

    query = (
        client.table("chats")
        .select(
            "id,student_id,classroom_id,content,summary,started_at,ended_at,subject_free_text,topic_source"
        )
        .order("started_at", desc=True)
    )
    if isinstance(limit, int) and limit > 0:
        query = query.limit(limit)

    try:
        chats_resp = query.execute()
    except APIError as err:
        raise _handle_api_error(err) from err
    except Exception as exc:
        raise SupabaseOperationError(str(exc)) from exc

    rows = chats_resp.data or []
    if not rows:
        return []

    classroom_ids: Set[Optional[str]] = {
        row.get("classroom_id") for row in rows if row.get("classroom_id")
    }
    student_ids: Set[Optional[str]] = {
        row.get("student_id") for row in rows if row.get("student_id")
    }

    classroom_map: Dict[str, Dict[str, Optional[str]]] = {}
    if classroom_ids:
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

        for row in classrooms_resp.data or []:
            cid = row.get("id")
            if not cid:
                continue
            classroom_map[cid] = {
                "name": row.get("name"),
                "theme_name": row.get("theme_name"),
            }

    user_map = _fetch_users_map(client, student_ids, users_table=users_table)

    normalized = [
        _normalize_chat_record(row, classroom_map=classroom_map, user_map=user_map)
        for row in rows
    ]
    return enrich_chats_with_evaluations(
        client, normalized, users_table=users_table
    )


def create_classroom_record(
    url: str,
    key: str,
    *,
    name: str,
    description: Optional[str],
    theme_name: Optional[str],
    theme_locked: bool,
    created_by: str,
    theme_config: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
    """Create a classroom entry."""

    payload: Dict[str, Any] = {
        "name": name,
        "description": description,
        "theme_name": theme_name or name,
        "theme_locked": bool(theme_locked),
        "created_by": created_by,
        "theme_config": theme_config,
    }

    client = _get_client(url, key)
    try:
        response = client.table("classrooms").insert(payload).execute()
    except APIError as err:
        raise _handle_api_error(err) from err
    except Exception as exc:
        raise SupabaseOperationError(str(exc)) from exc

    data = response.data or [payload]
    return data[0]


def update_classroom_record(
    url: str,
    key: str,
    classroom_id: str,
    **fields: Any,
) -> Dict[str, Any]:
    """Update classroom fields."""

    if not classroom_id:
        raise SupabaseOperationError("Identificador da sala ausente para atualização.")

    payload: Dict[str, Any] = {}
    for field, value in fields.items():
        if value is None and field not in {"description", "theme_config"}:
            continue
        payload[field] = value

    if not payload:
        return {}

    client = _get_client(url, key)
    try:
        response = (
            client.table("classrooms")
            .update(payload)
            .eq("id", classroom_id)
            .execute()
        )
    except APIError as err:
        raise _handle_api_error(err) from err
    except Exception as exc:
        raise SupabaseOperationError(str(exc)) from exc

    data = response.data or []
    return data[0] if data else payload


def delete_classroom_record(url: str, key: str, classroom_id: str) -> None:
    """Delete a classroom and cascading data."""

    client = _get_client(url, key)
    try:
        client.table("classrooms").delete().eq("id", classroom_id).execute()
    except APIError as err:
        raise _handle_api_error(err) from err
    except Exception as exc:
        raise SupabaseOperationError(str(exc)) from exc


def upsert_classroom_teacher(
    url: str,
    key: str,
    *,
    classroom_id: str,
    teacher_id: str,
    role_label: Optional[str] = None,
) -> None:
    """Add or update a teacher membership for a classroom."""

    payload: Dict[str, Any] = {
        "classroom_id": classroom_id,
        "teacher_id": teacher_id,
    }
    if role_label is not None:
        payload["role_label"] = role_label

    client = _get_client(url, key)
    try:
        client.table("classroom_teachers").upsert(
            payload, on_conflict="classroom_id,teacher_id"
        ).execute()
    except APIError as err:
        raise _handle_api_error(err) from err
    except Exception as exc:
        raise SupabaseOperationError(str(exc)) from exc


def remove_classroom_teacher(
    url: str,
    key: str,
    *,
    classroom_id: str,
    teacher_id: str,
) -> None:
    """Remove a teacher from a classroom."""

    client = _get_client(url, key)
    try:
        client.table("classroom_teachers").delete().eq("classroom_id", classroom_id).eq(
            "teacher_id", teacher_id
        ).execute()
    except APIError as err:
        raise _handle_api_error(err) from err
    except Exception as exc:
        raise SupabaseOperationError(str(exc)) from exc


def upsert_classroom_student(
    url: str,
    key: str,
    *,
    classroom_id: str,
    student_id: str,
    status: str = "active",
) -> None:
    """Add or update a student membership for a classroom."""

    payload = {
        "classroom_id": classroom_id,
        "student_id": student_id,
        "status": status or "active",
    }

    client = _get_client(url, key)
    try:
        client.table("classroom_students").upsert(
            payload, on_conflict="classroom_id,student_id"
        ).execute()
    except APIError as err:
        raise _handle_api_error(err) from err
    except Exception as exc:
        raise SupabaseOperationError(str(exc)) from exc


def remove_classroom_student(
    url: str,
    key: str,
    *,
    classroom_id: str,
    student_id: str,
    hard: bool = False,
) -> None:
    """Remove a student from a classroom (soft delete by default)."""

    client = _get_client(url, key)
    try:
        if hard:
            client.table("classroom_students").delete().eq(
                "classroom_id", classroom_id
            ).eq("student_id", student_id).execute()
        else:
            client.table("classroom_students").update({"status": "removed"}).eq(
                "classroom_id", classroom_id
            ).eq("student_id", student_id).execute()
    except APIError as err:
        raise _handle_api_error(err) from err
    except Exception as exc:
        raise SupabaseOperationError(str(exc)) from exc


def create_subject_record(
    url: str,
    key: str,
    *,
    classroom_id: str,
    name: str,
    created_by: str,
) -> Dict[str, Any]:
    """Create a subject associated with a classroom."""

    payload = {
        "classroom_id": classroom_id,
        "name": name,
        "created_by": created_by,
    }

    client = _get_client(url, key)
    try:
        response = client.table("classroom_subjects").insert(payload).execute()
    except APIError as err:
        raise _handle_api_error(err) from err
    except Exception as exc:
        raise SupabaseOperationError(str(exc)) from exc

    data = response.data or [payload]
    return data[0]


def update_subject_active(
    url: str,
    key: str,
    *,
    subject_id: str,
    is_active: bool,
) -> None:
    """Toggle the active flag of a subject."""

    client = _get_client(url, key)
    try:
        client.table("classroom_subjects").update({"is_active": bool(is_active)}).eq(
            "id", subject_id
        ).execute()
    except APIError as err:
        raise _handle_api_error(err) from err
    except Exception as exc:
        raise SupabaseOperationError(str(exc)) from exc


__all__ = [
    "fetch_classroom_domain",
    "list_all_chats",
    "create_classroom_record",
    "update_classroom_record",
    "delete_classroom_record",
    "upsert_classroom_teacher",
    "remove_classroom_teacher",
    "upsert_classroom_student",
    "remove_classroom_student",
    "create_subject_record",
    "update_subject_active",
]
