# services/auth_store.py
import json
import hashlib

HF_USERS_DB = "/tmp/users.json"  # demo only


def _loadUsers():
    try:
        with open(HF_USERS_DB, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return {}


def _saveUsers(db):
    try:
        with open(HF_USERS_DB, "w", encoding="utf-8") as f:
            json.dump(db, f)
    except Exception:
        pass


def _hashPw(pw: str) -> str:
    return hashlib.sha256(pw.encode("utf-8")).hexdigest()


def _getUserEntry(db, username):
    """Normaliza entrada para {pw:str, role:str}. Se legado (str), assume role 'aluno'."""
    entry = (db or {}).get(username)
    if isinstance(entry, dict):
        pw = entry.get("pw") or entry.get("password") or ""
        role = (entry.get("role") or "aluno").lower()
        return {"pw": pw, "role": role}
    if isinstance(entry, str):
        return {"pw": entry, "role": "aluno"}
    return None


def _setUserEntry(db, username, pw_hash, role):
    db[username] = {"pw": pw_hash, "role": (role or "aluno").lower()}
    return db
