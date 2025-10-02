"""Utility helpers shared across UI pages."""

import time
import uuid
from typing import Any, Dict, Iterable, List, Optional


LEGACY_OWNER_SENTINEL_PREFIX = "legacy-owner::"


def _now_ts() -> int:
    """Return current epoch timestamp in seconds."""
    return int(time.time())


def _mk_id(prefix: str = "id") -> str:
    """Generate a short unique identifier with the provided prefix."""
    return f"{prefix}_{str(uuid.uuid4())[:8]}"


def _normalize_username(value: Optional[str]) -> str:
    """Normalize usernames to ease comparisons inside Supabase payloads."""
    return (value or "").strip().lower()


def _legacy_owner_sentinel(login: Optional[str]) -> Optional[str]:
    """Return deterministic sentinel identifier for legacy owner logins."""

    normalized = _normalize_username(login)
    if not normalized:
        return None
    return f"{LEGACY_OWNER_SENTINEL_PREFIX}{normalized}"


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


def _auth_login(auth: Optional[Dict[str, Any]]) -> str:
    """Return normalized login/username for the current auth payload."""

    if not isinstance(auth, dict):
        return ""
    return _normalize_username(auth.get("username") or auth.get("login"))


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

    def _map_for(suffix: str) -> Dict[str, str]:
        candidates = [f"{group}_{suffix}"]
        if group.endswith("s") and len(group) > 1:
            singular = group[:-1]
            candidates.append(f"{singular}_{suffix}")
        for key in candidates:
            value = members.get(key)
            if isinstance(value, dict) and value:
                return value
        # fall back to empty dict if no mapping available
        return {}

    labels_map = _map_for("labels")
    usernames_map = _map_for("usernames")

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

        is_legacy_owner = identifier.startswith(LEGACY_OWNER_SENTINEL_PREFIX)
        legacy_hint = ""
        if is_legacy_owner:
            legacy_hint = identifier[len(LEGACY_OWNER_SENTINEL_PREFIX) :]
            if not base_label:
                base_label = display_username or legacy_hint or identifier

        if username_only:
            formatted = display_username or base_label or identifier
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

        if is_legacy_owner and formatted:
            formatted = f"{formatted} (responsável)"

        if formatted:
            sort_key = (base_label or display_username or identifier).lower()
            results.append((sort_key, formatted))

    results.sort(key=lambda item: item[0])
    return [label for _, label in results]


def _auth_matches_classroom_teacher(
    auth: Optional[Dict[str, Any]], classroom: Optional[Dict[str, Any]]
) -> bool:
    """Return True if auth matches one of the classroom teachers."""

    if not isinstance(classroom, dict):
        return False

    members = (classroom.get("members") or {})
    teacher_ids = [str(uid) for uid in members.get("teachers", []) if uid]
    if not teacher_ids:
        teacher_ids = []

    user_id = _auth_user_id(auth)
    if user_id:
        user_id = str(user_id)
    if user_id and user_id in teacher_ids:
        return True

    login = _auth_login(auth)
    if not login:
        return False

    teacher_usernames = members.get("teacher_usernames") or {}
    for identifier in teacher_ids:
        username = teacher_usernames.get(identifier)
        if username and _normalize_username(username) == login:
            return True
    return False


def _auth_matches_classroom_owner(
    auth: Optional[Dict[str, Any]], classroom: Optional[Dict[str, Any]]
) -> bool:
    """Return True if auth represents the classroom owner."""

    if not isinstance(classroom, dict):
        return False

    owner_id = classroom.get("owner_id")
    user_id = _auth_user_id(auth)
    if user_id:
        user_id = str(user_id)
    if owner_id:
        return bool(user_id) and str(owner_id) == user_id

    login = _auth_login(auth)
    if not login:
        return False

    owner_login = _normalize_username(
        classroom.get("owner_login") or classroom.get("owner_username")
    )
    if owner_login and login == owner_login:
        return True

    sentinel = _legacy_owner_sentinel(owner_login or login)
    if sentinel:
        members = (classroom.get("members") or {})
        teacher_usernames = members.get("teacher_usernames") or {}
        if _normalize_username(teacher_usernames.get(sentinel)) == login:
            return True

    return False
