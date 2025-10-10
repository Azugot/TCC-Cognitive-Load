"""Application wide configuration constants."""

import os

SUPABASE_URL = os.getenv("SUPABASE_URL", "")
SUPABASE_SERVICE_ROLE_KEY = os.getenv("SUPABASE_SERVICE_ROLE_KEY", "")
SUPABASE_USERS_TABLE = os.getenv("SUPABASE_USERS_TABLE", "")
SUPABASE_CHAT_BUCKET = os.getenv("SUPABASE_CHAT_BUCKET", "")
SUPABASE_CHAT_STORAGE_PREFIX = os.getenv("SUPABASE_CHAT_STORAGE_PREFIX", "")
SUPABASE_CLASS_DOCS_BUCKET = os.getenv("SUPABASE_CLASS_DOCS_BUCKET", "")
SUPABASE_CLASS_DOCS_PREFIX = os.getenv("SUPABASE_CLASS_DOCS_PREFIX", "classrooms")

ROLE_PT_TO_DB = {
    "aluno": "student",
    "professor": "teacher",
    "admin": "admin",
}

VERTEX_SERVICE_ACCOUNT = {
    "type": "service_account",
    "project_id": os.getenv("GCP_PROJECT_ID", ""),
    "private_key_id": os.getenv("GCP_PRIVATE_KEY_ID", ""),
    "private_key": os.getenv("GCP_PRIVATE_KEY", "").replace("\\n", "\n"),
    "client_email": os.getenv("GCP_CLIENT_EMAIL", ""),    # ok hard-coded
    "client_id": os.getenv("GCP_CLIENT_ID", ""),          # ok hard-coded
    "auth_uri": "https://accounts.google.com/o/oauth2/auth",
    "token_uri": "https://oauth2.googleapis.com/token",
    "auth_provider_x509_cert_url": "https://www.googleapis.com/oauth2/v1/certs",
    "client_x509_cert_url": os.getenv("GCP_CLIENT_X509_CERT_URL", ""),
    "universe_domain": "googleapis.com",
}


ROLE_DB_TO_PT = {value: key for key, value in ROLE_PT_TO_DB.items()}
