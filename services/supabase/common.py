"""Componentes compartilhados entre integrações do Supabase.

Perfis: utilitários e clientes usados por administradores, professores e
alunos (sempre por meio das camadas de serviço autorizadas).
"""

from __future__ import annotations

import threading
from dataclasses import dataclass
from datetime import datetime, timezone
import json
import uuid
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


def _ensure_dict(value: Any) -> Dict[str, Any]:
    if isinstance(value, dict):
        return dict(value)
    if isinstance(value, str):
        try:
            parsed = json.loads(value)
            if isinstance(parsed, dict):
                return dict(parsed)
        except ValueError:
            return {}
    return {}


def _normalize_comment_entries(raw_comments: Any) -> List[Dict[str, Any]]:
    comments: List[Dict[str, Any]] = []
    if not isinstance(raw_comments, list):
        return comments
    for entry in raw_comments:
        if not isinstance(entry, dict):
            continue
        text = entry.get("text") or entry.get("comment")
        if text is None:
            continue
        text_str = str(text).strip()
        if not text_str:
            continue
        comments.append(
            {
                "id": entry.get("id"),
                "author_id": entry.get("author_id"),
                "author_login": entry.get("author_login"),
                "author_name": entry.get("author_name"),
                "created_at": entry.get("created_at"),
                "text": text_str,
            }
        )
    comments.sort(key=lambda item: item.get("created_at") or "")
    return comments


def _normalize_chat_record(
    row: Dict[str, Any],
    *,
    classroom_map: Optional[Dict[str, Dict[str, Any]]] = None,
    user_map: Optional[Dict[str, Dict[str, Optional[str]]]] = None,
) -> Dict[str, Any]:
    content = _ensure_dict(row.get("content"))

    subjects: List[str] = []
    raw_subjects = content.get("subjects")
    if isinstance(raw_subjects, list):
        for item in raw_subjects:
            if isinstance(item, str) and item.strip():
                subjects.append(item.strip())

    summary_value = row.get("summary") or content.get("summary") or ""
    summary_text = str(summary_value).strip() if summary_value else ""
    preview = summary_text
    if not preview:
        topic = row.get("topic_source") or row.get("subject_free_text")
        if isinstance(topic, str):
            preview = topic.strip()
    if len(preview) > 240:
        preview = preview[:237].rstrip() + "…"

    bucket = content.get("bucket") or content.get("storage_bucket")
    path = content.get("path") or content.get("storage_path")
    storage_path_id = (
        content.get("storage_path_id")
        or content.get("storage_chat_id")
        or content.get("storage_id")
    )

    comments = _normalize_comment_entries(content.get("teacher_comments"))

    auto_eval = content.get("auto_evaluation")
    if isinstance(auto_eval, str):
        auto_eval = auto_eval.strip()
    elif auto_eval is None:
        auto_eval = ""
    else:
        auto_eval = str(auto_eval).strip()

    grade = content.get("grade")
    student_goal = content.get("student_goal")
    if isinstance(student_goal, str):
        student_goal = student_goal.strip()
    student_interest = content.get("student_interest")
    if isinstance(student_interest, str):
        student_interest = student_interest.strip()

    student_info = (user_map or {}).get(row.get("student_id"), {})
    classroom_info = (classroom_map or {}).get(row.get("classroom_id"), {})

    normalized = {
        "id": row.get("id"),
        "student_id": row.get("student_id"),
        "student_login": student_info.get("login"),
        "student_name": student_info.get("display_name")
        or student_info.get("login"),
        "classroom_id": row.get("classroom_id"),
        "classroom_name": classroom_info.get("name"),
        "classroom_theme": classroom_info.get("theme_name"),
        "subjects": subjects,
        "summary": summary_text,
        "summary_preview": preview,
        "grade": grade,
        "storage_bucket": bucket,
        "storage_path": path,
        "storage_path_id": storage_path_id,
        "teacher_comments": comments,
        "auto_evaluation": auto_eval,
        "auto_evaluation_updated_at": content.get("auto_evaluation_updated_at"),
        "started_at": row.get("started_at"),
        "ended_at": row.get("ended_at"),
        "subject_free_text": row.get("subject_free_text")
        or content.get("subject_free_text"),
        "topic_source": row.get("topic_source") or content.get("topic_source"),
        "student_goal": student_goal,
        "student_interest": student_interest,
        "raw_content": content,
        "has_attachment": bool(bucket and path),
    }
    return normalized


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


def _load_chat_content(client: Client, chat_id: str) -> Dict[str, Any]:
    try:
        response = (
            client.table("chats").select("content").eq("id", chat_id).limit(1).execute()
        )
    except APIError as err:
        raise _handle_api_error(err) from err
    except Exception as exc:
        raise SupabaseOperationError(str(exc)) from exc

    rows = response.data or []
    if not rows:
        raise SupabaseOperationError("Chat não encontrado no Supabase.")

    return _ensure_dict(rows[0].get("content"))


def add_chat_comment(
    url: str,
    key: str,
    *,
    chat_id: str,
    author_id: Optional[str],
    author_login: Optional[str],
    author_name: Optional[str],
    text: str,
) -> Dict[str, Any]:
    """Append a teacher/admin comment to the chat content JSON."""

    normalized_text = (text or "").strip()
    if not normalized_text:
        raise SupabaseOperationError("Informe um comentário válido.")

    client = _get_client(url, key)
    content = _load_chat_content(client, chat_id)

    comments = list(content.get("teacher_comments") or [])
    sanitized = _normalize_comment_entries(comments)

    entry = {
        "id": str(uuid.uuid4()),
        "author_id": author_id,
        "author_login": _normalize_login(author_login or ""),
        "author_name": author_name or author_login or author_id,
        "text": normalized_text,
        "created_at": datetime.now(timezone.utc).isoformat(),
    }
    sanitized.append(entry)
    content["teacher_comments"] = sanitized

    try:
        client.table("chats").update({"content": content}).eq("id", chat_id).execute()
    except APIError as err:
        raise _handle_api_error(err) from err
    except Exception as exc:
        raise SupabaseOperationError(str(exc)) from exc

    return entry


def set_chat_auto_evaluation(
    url: str,
    key: str,
    *,
    chat_id: str,
    evaluation_text: str,
) -> str:
    """Persist the generated evaluation text inside the chat content."""

    client = _get_client(url, key)
    content = _load_chat_content(client, chat_id)

    normalized_text = (evaluation_text or "").strip()
    if normalized_text:
        content["auto_evaluation"] = normalized_text
        content["auto_evaluation_updated_at"] = datetime.now(timezone.utc).isoformat()
    else:
        content.pop("auto_evaluation", None)
        content.pop("auto_evaluation_updated_at", None)

    try:
        client.table("chats").update({"content": content}).eq("id", chat_id).execute()
    except APIError as err:
        raise _handle_api_error(err) from err
    except Exception as exc:
        raise SupabaseOperationError(str(exc)) from exc

    return content.get("auto_evaluation") or ""


def reset_cached_client() -> None:
    """Clear the cached Supabase client (useful for tests)."""

    global _cached_client, _client_signature
    with _client_lock:
        _cached_client = None
        _client_signature = None


__all__ = [
    "SupabaseError",
    "SupabaseConfigurationError",
    "SupabaseUserExistsError",
    "SupabaseOperationError",
    "UserRecord",
    "_get_client",
    "_handle_api_error",
    "_normalize_login",
    "_normalize_timestamp",
    "_normalize_chat_record",
    "add_chat_comment",
    "set_chat_auto_evaluation",
    "fetch_user_record",
    "create_user_record",
    "fetch_users_by_role",
    "_fetch_users_map",
    "reset_cached_client",
]
