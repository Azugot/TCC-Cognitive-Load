"""Componentes compartilhados entre integrações do Supabase.

Perfis: utilitários e clientes usados por administradores, professores e
alunos (sempre por meio das camadas de serviço autorizadas).
"""

from __future__ import annotations

import threading
from dataclasses import dataclass
from datetime import datetime, timezone
import json
from typing import Any, Dict, List, Optional, Sequence, Set, Tuple

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
        normalized: Dict[str, Any] = {
            "id": entry.get("id"),
            "author_id": entry.get("author_id"),
            "author_login": entry.get("author_login"),
            "author_name": entry.get("author_name"),
            "created_at": entry.get("created_at"),
            "text": text_str,
        }
        score_value = entry.get("score")
        if score_value is None:
            score_value = entry.get("overall_score")
        try:
            if score_value is not None:
                normalized["score"] = float(score_value)
        except (TypeError, ValueError):
            pass
        comments.append(normalized)
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


def _normalize_evaluation_row(
    row: Dict[str, Any],
    *,
    user_map: Optional[Dict[str, Dict[str, Optional[str]]]] = None,
) -> Dict[str, Any]:
    evaluator_id = row.get("evaluator_id")
    info = (user_map or {}).get(evaluator_id, {})
    comment_text = (row.get("comments") or "").strip()
    try:
        score_value = float(row.get("overall_score"))
    except (TypeError, ValueError):
        score_value = None

    author_login = info.get("login") or _normalize_login(row.get("evaluator_login") or "")
    author_name = (
        info.get("display_name")
        or row.get("evaluator_name")
        or row.get("evaluator_login")
        or author_login
        or evaluator_id
    )

    entry: Dict[str, Any] = {
        "id": row.get("id"),
        "chat_id": row.get("chat_id"),
        "author_id": evaluator_id,
        "author_login": author_login,
        "author_name": author_name,
        "created_at": row.get("created_at"),
        "text": comment_text,
    }
    if score_value is not None:
        entry["score"] = score_value
    return entry


def fetch_chat_evaluations_for_ids(
    client: Client,
    chat_ids: Sequence[Optional[str]],
    *,
    users_table: str = "users",
) -> Dict[str, List[Dict[str, Any]]]:
    cleaned = [cid for cid in chat_ids if cid]
    if not cleaned:
        return {}

    try:
        response = (
            client.table("chat_evaluations")
            .select("id,chat_id,evaluator_id,overall_score,comments,created_at")
            .in_("chat_id", cleaned)
            .order("created_at")
            .execute()
        )
    except APIError as err:
        raise _handle_api_error(err) from err
    except Exception as exc:
        raise SupabaseOperationError(str(exc)) from exc

    rows = response.data or []
    user_ids: Set[Optional[str]] = {row.get("evaluator_id") for row in rows if row.get("evaluator_id")}
    user_map = _fetch_users_map(client, user_ids, users_table=users_table) if user_ids else {}

    evaluations: Dict[str, List[Dict[str, Any]]] = {}
    for row in rows:
        chat_id = row.get("chat_id")
        if not chat_id:
            continue
        entry = _normalize_evaluation_row(row, user_map=user_map)
        evaluations.setdefault(chat_id, []).append(entry)
    return evaluations


def fetch_latest_auto_evaluations(
    client: Client,
    chat_ids: Sequence[Optional[str]],
) -> Dict[str, Dict[str, Any]]:
    cleaned = [cid for cid in chat_ids if cid]
    if not cleaned:
        return {}

    try:
        response = (
            client.table("automated_chat_evaluations")
            .select("id,chat_id,bot_evaluation,created_at")
            .in_("chat_id", cleaned)
            .order("created_at", desc=True)
            .execute()
        )
    except APIError as err:
        raise _handle_api_error(err) from err
    except Exception as exc:
        raise SupabaseOperationError(str(exc)) from exc

    rows = response.data or []
    results: Dict[str, Dict[str, Any]] = {}
    for row in rows:
        chat_id = row.get("chat_id")
        if not chat_id or chat_id in results:
            continue
        payload = _ensure_dict(row.get("bot_evaluation"))
        text_value = payload.get("text") or payload.get("evaluation") or payload.get("content")
        if isinstance(text_value, dict):
            text_value = json.dumps(text_value, ensure_ascii=False)
        text_str = str(text_value).strip() if text_value is not None else ""
        score_value = payload.get("score") or payload.get("rating")
        try:
            score = float(score_value) if score_value is not None else None
        except (TypeError, ValueError):
            score = None
        results[chat_id] = {
            "id": row.get("id"),
            "text": text_str,
            "score": score,
            "created_at": row.get("created_at"),
            "raw": payload,
        }
    return results


def enrich_chats_with_evaluations(
    client: Client,
    chats: List[Dict[str, Any]],
    *,
    users_table: str = "users",
) -> List[Dict[str, Any]]:
    if not chats:
        return chats

    chat_ids = [chat.get("id") for chat in chats if chat.get("id")]
    manual_map = fetch_chat_evaluations_for_ids(
        client, chat_ids, users_table=users_table
    )
    auto_map = fetch_latest_auto_evaluations(client, chat_ids)

    for chat in chats:
        chat_id = chat.get("id")
        existing_comments = _normalize_comment_entries(chat.get("teacher_comments"))
        db_comments = manual_map.get(chat_id, [])
        combined: List[Dict[str, Any]] = []
        if db_comments:
            combined.extend(db_comments)
        if existing_comments:
            combined.extend(existing_comments)
        if combined:
            combined.sort(key=lambda item: item.get("created_at") or "")
            chat["teacher_comments"] = combined
        else:
            chat["teacher_comments"] = []

        auto_entry = auto_map.get(chat_id)
        if auto_entry:
            chat["auto_evaluation"] = auto_entry.get("text") or chat.get("auto_evaluation")
            chat["auto_evaluation_score"] = auto_entry.get("score")
            chat["auto_evaluation_updated_at"] = auto_entry.get("created_at")
    return chats


def add_chat_comment(
    url: str,
    key: str,
    *,
    chat_id: str,
    author_id: Optional[str],
    author_login: Optional[str],
    author_name: Optional[str],
    text: str,
    score: Any,
) -> Dict[str, Any]:
    """Persist a manual evaluation/comment in ``chat_evaluations``."""

    normalized_text = (text or "").strip()
    if not normalized_text:
        raise SupabaseOperationError("Informe um comentário válido.")
    if not chat_id:
        raise SupabaseOperationError("Identificador do chat ausente para registrar comentário.")
    if not author_id:
        raise SupabaseOperationError("Identificador do avaliador ausente para registrar comentário.")

    try:
        numeric_score = float(score)
    except (TypeError, ValueError):
        raise SupabaseOperationError("Informe uma nota numérica válida para o comentário.")

    client = _get_client(url, key)
    payload = {
        "chat_id": chat_id,
        "evaluator_id": author_id,
        "overall_score": numeric_score,
        "comments": normalized_text,
    }

    try:
        response = client.table("chat_evaluations").insert(payload).execute()
    except APIError as err:
        raise _handle_api_error(err) from err
    except Exception as exc:
        raise SupabaseOperationError(str(exc)) from exc

    row = response.data[0] if response.data else payload
    user_map = _fetch_users_map(client, {author_id}) if author_id else {}
    entry = _normalize_evaluation_row(row, user_map=user_map)
    if not entry.get("author_name"):
        entry["author_name"] = author_name or author_login or author_id
    if not entry.get("author_login"):
        entry["author_login"] = _normalize_login(author_login or "")
    if entry.get("text") is None:
        entry["text"] = normalized_text
    if entry.get("score") is None:
        entry["score"] = numeric_score
    return entry


def record_auto_chat_evaluation(
    url: str,
    key: str,
    *,
    chat_id: str,
    evaluation_text: str,
    evaluation_score: Optional[float] = None,
    extra_payload: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
    """Store an automated evaluation snapshot for a chat."""

    client = _get_client(url, key)
    payload = dict(extra_payload or {})
    payload.setdefault("text", (evaluation_text or "").strip())
    if evaluation_score is not None:
        try:
            payload["score"] = float(evaluation_score)
        except (TypeError, ValueError):
            pass

    record = {"chat_id": chat_id, "bot_evaluation": payload}

    try:
        response = client.table("automated_chat_evaluations").insert(record).execute()
    except APIError as err:
        raise _handle_api_error(err) from err
    except Exception as exc:
        raise SupabaseOperationError(str(exc)) from exc

    row = response.data[0] if response.data else record
    stored_payload = _ensure_dict(row.get("bot_evaluation")) or payload
    text_value = stored_payload.get("text") or ""
    try:
        score_value = float(stored_payload.get("score")) if stored_payload.get("score") is not None else None
    except (TypeError, ValueError):
        score_value = None

    return {
        "id": row.get("id"),
        "chat_id": chat_id,
        "text": str(text_value).strip(),
        "score": score_value,
        "created_at": row.get("created_at"),
        "raw": stored_payload,
    }


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
    "fetch_chat_evaluations_for_ids",
    "fetch_latest_auto_evaluations",
    "enrich_chats_with_evaluations",
    "record_auto_chat_evaluation",
    "fetch_user_record",
    "create_user_record",
    "fetch_users_by_role",
    "_fetch_users_map",
    "reset_cached_client",
]
