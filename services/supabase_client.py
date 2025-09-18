"""Utilities for interacting with Supabase from the application."""

from __future__ import annotations

import os
import threading
from dataclasses import dataclass
from datetime import datetime, timezone
import mimetypes
from typing import Any, Dict, List, Optional, Set, Tuple

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


def _normalize_timestamp(value: Any) -> Optional[str]:
    """Convert assorted timestamp inputs to an ISO-8601 UTC string."""

    if value is None:
        return None
    if isinstance(value, str):
        text = value.strip()
        return text or None
    if isinstance(value, datetime):
        dt = value
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        else:
            dt = dt.astimezone(timezone.utc)
        return dt.isoformat()

    try:
        numeric = float(value)
    except (TypeError, ValueError):
        return None

    try:
        return datetime.fromtimestamp(numeric, tz=timezone.utc).isoformat()
    except (OverflowError, OSError, ValueError):
        return None


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
            .order("name")
            .execute()
        )
    except APIError as err:
        raise _handle_api_error(err) from err
    except Exception as exc:
        raise SupabaseOperationError(str(exc)) from exc

    users = response.data or []
    return [UserRecord.from_raw(item) for item in users]


def _fetch_users_map(
    client: Client, user_ids: Set[Optional[str]], users_table: str = "users"
) -> Dict[str, Dict[str, Optional[str]]]:
    """Return a mapping of user_id -> {login, display_name, email, name}."""

    cleaned = [uid for uid in user_ids if uid]
    if not cleaned:
        return {}

    try:
        response = (
            client.table(users_table)
            .select("id,email,name")
            .in_("id", cleaned)
            .execute()
        )
    except APIError as err:
        raise _handle_api_error(err) from err
    except Exception as exc:
        raise SupabaseOperationError(str(exc)) from exc

    mapping: Dict[str, Dict[str, Optional[str]]] = {}
    for row in response.data or []:
        uid = row.get("id")
        if not uid:
            continue
        login = _normalize_login(row.get("email") or row.get("name"))
        mapping[uid] = {
            "login": login,
            "display_name": row.get("name") or row.get("email"),
            "email": row.get("email"),
            "name": row.get("name"),
        }
    return mapping


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

    user_ids: Set[Optional[str]] = set()
    for row in teacher_rows:
        user_ids.add(row.get("teacher_id"))
    for row in student_rows:
        user_ids.add(row.get("student_id"))
    for row in classrooms_raw:
        user_ids.add(row.get("created_by"))

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

    classrooms: List[Dict[str, Any]] = []
    for raw in classrooms_raw:
        cid = raw.get("id")
        if not cid:
            continue
        teacher_entries = teachers_by_class.get(cid, [])
        student_entries = students_by_class.get(cid, [])
        owner_login = None
        owner_id = None
        for entry in teacher_entries:
            if (entry.get("role_label") or "").lower() == "owner":
                owner_id = entry.get("user_id")
                owner_login = entry.get("login")
                break
        if not owner_login and raw.get("created_by"):
            creator_info = user_map.get(raw.get("created_by"))
            if creator_info:
                owner_login = creator_info.get("login") or owner_login

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
                "teachers": teacher_entries,
                "students": student_entries,
            }
        )

    return classrooms, subjects_by_class


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


def reset_cached_client() -> None:
    """Clear the cached Supabase client (useful for tests)."""

    global _cached_client, _client_signature
    with _client_lock:
        _cached_client = None
        _client_signature = None


def upload_file_to_bucket(
    url: str,
    key: str,
    *,
    bucket: str,
    file_path: str,
    storage_path: str,
    content_type: Optional[str] = None,
    upsert: bool = True,
) -> str:
    """Upload a file to a Supabase Storage bucket.

    Args:
        url: Supabase project URL.
        key: Supabase service role key.
        bucket: Target storage bucket name.
        file_path: Absolute path to the file on disk.
        storage_path: Path (within the bucket) where the file should live.
        content_type: Optional MIME type (``None`` keeps the default).
        upsert: Whether to overwrite an existing file with the same path.

    Returns:
        The path of the stored object inside the bucket.

    Raises:
        SupabaseConfigurationError: When the Supabase client is not configured.
        SupabaseOperationError: When validation fails or the upload operation fails.
    """

    if not bucket or not bucket.strip():
        raise SupabaseOperationError("Bucket do Storage não informado para upload.")

    normalized_path = (storage_path or "").strip().lstrip("/")
    if not normalized_path:
        raise SupabaseOperationError("Caminho do arquivo no Storage não informado.")

    if not file_path or not os.path.isfile(file_path):
        raise SupabaseOperationError(f"Arquivo inexistente para upload: {file_path}")

    client: Client = _get_client(url, key)  # sua função existente

    # Defina o content-type corretamente (lowercase na chave)
    if not content_type:
        guessed, _ = mimetypes.guess_type(file_path)
        content_type = guessed or "application/octet-stream"

    # >>> O PONTO CRÍTICO: valores de headers como strings
    file_options: Dict[str, Any] = {
        "upsert": "true" if upsert else "false",   # NÃO bool
        "content-type": content_type,              # chave em lowercase
        # opcional: "cache-control": "3600"
    }

    # Use argumentos nomeados para evitar ordem errada
    try:
        with open(file_path, "rb") as fh:
            data = fh.read()

        resp = client.storage.from_(bucket).upload(
            path=normalized_path,
            file=data,
            file_options=file_options,
        )
    except APIError as err:
        raise _handle_api_error(err) from err
    except Exception as exc:
        raise SupabaseOperationError(f"Falha ao enviar arquivo ao bucket '{bucket}': {exc}") from exc

    # O SDK pode retornar dict ou objeto; normalize
    if isinstance(resp, dict):
        stored_path = resp.get("path") or resp.get("Key") or normalized_path
    else:
        stored_path = normalized_path

    return stored_path


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
    content_payload["student_interest"] = _normalize_optional_text(
        student_interest
    )

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

    return result
