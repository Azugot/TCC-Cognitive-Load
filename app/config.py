"""Application wide configuration constants."""

import os

SUPABASE_URL = "https://YOUR_SUPABASE_PROJECT.supabase.co"
SUPABASE_SERVICE_ROLE_KEY = "SUPABASE_SERVICE_ROLE_KEY"
SUPABASE_USERS_TABLE = "users"
SUPABASE_CHAT_BUCKET = os.getenv("SUPABASE_CHAT_BUCKET", "chat-logs")
SUPABASE_CHAT_STORAGE_PREFIX = os.getenv("SUPABASE_CHAT_STORAGE_PREFIX", "classrooms")

ROLE_PT_TO_DB = {
    "aluno": "student",
    "professor": "teacher",
    "admin": "admin",
}

ROLE_DB_TO_PT = {value: key for key, value in ROLE_PT_TO_DB.items()}
