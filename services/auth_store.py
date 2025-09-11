# services/auth_store.py
import os
import json
import hashlib

# Mesmo caminho do demo original (volátil e compartilhado no container)
HF_USERS_DB = "/tmp/users.json"

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
