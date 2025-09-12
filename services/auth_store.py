# services/auth_store.py
import json
import hashlib

HF_USERS_DB = "./tmp/users.json"  # demo only


def _log(msg):
    print(f"[AUTH] {msg}")


def _normalize_db_keys(db: dict) -> dict:
    fixed = {}
    for k, v in (db or {}).items():
        uname = (k or "").strip().lower()
        if isinstance(v, dict):
            pw = v.get("pw") or v.get("password") or ""
            role = (v.get("role") or "aluno").lower()
            fixed[uname] = {"pw": pw, "role": role}
        elif isinstance(v, str):
            fixed[uname] = {"pw": v, "role": "aluno"}
    return fixed


def _loadUsers():
    try:
        with open(HF_USERS_DB, "r", encoding="utf-8") as f:
            raw = json.load(f)
        fixed = _normalize_db_keys(raw if isinstance(raw, dict) else {})
        if fixed != raw:
            _log(
                f"migrando chaves para minúsculas e salvando de volta ({len(fixed)} usuários)")
            _saveUsers(fixed)
        _log(f"carregado OK: {len(fixed)} usuário(s)")
        return fixed
    except Exception as e:
        _log(f"arquivo inexistente ou inválido: {e}; iniciando vazio")
        return {}


def _saveUsers(db):
    try:
        with open(HF_USERS_DB, "w", encoding="utf-8") as f:
            json.dump(db or {}, f)
        _log(f"salvo OK: {len(db or {})} usuário(s)")
    except Exception as e:
        _log(f"falha ao salvar: {e}")


def _hashPw(pw: str) -> str:
    return hashlib.sha256((pw or "").encode("utf-8")).hexdigest()


def _getUserEntry(db, username):
    uname = (username or "").strip().lower()
    entry = (db or {}).get(uname)
    if isinstance(entry, dict):
        pw = entry.get("pw") or entry.get("password") or ""
        role = (entry.get("role") or "aluno").lower()
        _log(f"getUserEntry('{uname}') -> encontrado (role={role})")
        return {"pw": pw, "role": role}
    if isinstance(entry, str):
        _log(f"getUserEntry('{uname}') -> legado(str) (assumindo role=aluno)")
        return {"pw": entry, "role": "aluno"}
    _log(f"getUserEntry('{uname}') -> NÃO encontrado")
    return None


def _setUserEntry(db, username, pw_hash, role):
    uname = (username or "").strip().lower()
    db[uname] = {"pw": pw_hash, "role": (role or "aluno").lower()}
    _log(f"setUserEntry('{uname}', role={role})")
    return db
