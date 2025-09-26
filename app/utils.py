"""Utility helpers shared across UI pages."""

import time
import uuid
from typing import Any, Dict, Iterable, List, Optional


def _now_ts() -> int:
    """Return current epoch timestamp in seconds."""
    return int(time.time())


def _mk_id(prefix: str = "id") -> str:
    """Generate a short unique identifier with the provided prefix."""
    return f"{prefix}_{str(uuid.uuid4())[:8]}"


def _normalize_username(value: Optional[str]) -> str:
    """Normalize usernames to ease comparisons inside Supabase payloads."""
    return (value or "").strip().lower()


def _user_role(auth: Optional[Dict[str, Any]]) -> str:
    """Return normalized role string from the auth state."""
    return str((auth or {}).get("role", "")).strip().lower()


def _is_admin(auth: Optional[Dict[str, Any]]) -> bool:
    return _user_role(auth) == "admin"


def _is_teacher(auth: Optional[Dict[str, Any]]) -> bool:
    return _user_role(auth) == "professor"


def _auth_user_id(auth: Optional[Dict[str, Any]]) -> Optional[str]:
    return (auth or {}).get("user_id")


def _merge_notice(text: str, notice: str) -> str:
    if not notice:
        return text
    base = text or ""
    if not base:
        return notice
    return f"{notice}\n\n{base}"


def _teacher_username(auth: Optional[Dict[str, Any]]) -> str:
    return _normalize_username((auth or {}).get("username"))


def _student_username(auth: Optional[Dict[str, Any]]) -> str:
    return _normalize_username((auth or {}).get("username"))


def _get_class_by_id(classrooms: Iterable[Dict[str, Any]], cls_id: Optional[str]):
    return next((x for x in (classrooms or []) if x.get("id") == cls_id), None)


def _class_member_labels(
    classroom: Optional[Dict[str, Any]],
    group: str,
    *,
    include_usernames: bool = False,
    username_only: bool = False,
) -> List[str]:
    """Return formatted member labels for the requested classroom group."""

    members = (classroom or {}).get("members", {}) or {}
    member_ids = list(members.get(group, []) or [])
    labels_map = members.get(f"{group}_labels", {}) or {}
    usernames_map = members.get(f"{group}_usernames", {}) or {}

    seen = set()
    results = []
    for member in member_ids:
        identifier = str(member or "").strip()
        if not identifier or identifier in seen:
            continue
        seen.add(identifier)

        raw_label = (
            labels_map.get(identifier)
            or labels_map.get(member)
            or ""
        )
        base_label = raw_label.strip() if isinstance(raw_label, str) else ""

        username_value = (
            usernames_map.get(identifier)
            or usernames_map.get(member)
            or ""
        )
        username_label = username_value.strip() if isinstance(username_value, str) else ""
        normalized_username = _normalize_username(username_label) if username_label else ""
        display_username = username_label or normalized_username

        if username_only:
            formatted = display_username or identifier
        elif include_usernames:
            compare_label = base_label.lower() if base_label else ""
            compare_username = (display_username or "").lower()
            if base_label and display_username and compare_label != compare_username:
                formatted = f"{base_label} (u: {display_username})"
            elif display_username:
                formatted = display_username
            else:
                formatted = base_label or identifier
        else:
            formatted = base_label or display_username or identifier

        if formatted:
            sort_key = (base_label or display_username or identifier).lower()
            results.append((sort_key, formatted))

    results.sort(key=lambda item: item[0])
    return [label for _, label in results]
