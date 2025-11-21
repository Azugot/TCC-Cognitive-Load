"""Application wide configuration constants."""

import os

SUPABASE_URL = os.getenv("SUPABASE_URL", "https://zocktiqsmpekmltfkopy.supabase.co")
SUPABASE_SERVICE_ROLE_KEY = os.getenv("SUPABASE_SERVICE_ROLE_KEY", "eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9.eyJpc3MiOiJzdXBhYmFzZSIsInJlZiI6InpvY2t0aXFzbXBla21sdGZrb3B5Iiwicm9sZSI6InNlcnZpY2Vfcm9sZSIsImlhdCI6MTc1NzA3NDUxNywiZXhwIjoyMDcyNjUwNTE3fQ.vay8JKF1WtoagwvC5eNPsyCTPQmUqWcy2WNdYri6rxQ")
SUPABASE_USERS_TABLE = os.getenv("SUPABASE_USERS_TABLE", "users")
SUPABASE_CHAT_BUCKET = os.getenv("SUPABASE_CHAT_BUCKET", "ChatRecords")
SUPABASE_CHAT_STORAGE_PREFIX = os.getenv("SUPABASE_CHAT_STORAGE_PREFIX", "classrooms")
SUPABASE_CLASS_DOCS_BUCKET = os.getenv("SUPABASE_CLASS_DOCS_BUCKET", "ClassroomsDocs")
SUPABASE_CLASS_DOCS_PREFIX = os.getenv("SUPABASE_CLASS_DOCS_PREFIX", "classrooms")

ROLE_PT_TO_DB = {
    "aluno": "student",
    "professor": "teacher",
    "admin": "admin",
}

VERTEX_SERVICE_ACCOUNT = {
    "type": "service_account",
    "project_id": os.getenv("GCP_PROJECT_ID", "acquired-router-470921-a3"),
    "private_key_id": os.getenv("GCP_PRIVATE_KEY_ID", "a19e3f67c4166764b79daebf498c88328c5215bb"),
    "private_key": os.getenv("GCP_PRIVATE_KEY", "-----BEGIN PRIVATE KEY-----\nMIIEvQIBADANBgkqhkiG9w0BAQEFAASCBKcwggSjAgEAAoIBAQCklEIgFhSK8HWh\nB0bLrzxv2qZ8goYzXHHBJ2NM9zyI2tf5himi67JUcgavp0ylbcPe+T4xRC2pwvH3\nDXioMpmeziJ5QccJp8LIgIJMjv/Tgqj4vE1LQNBOlSj/lDE17ko6Q9zM6HqYqXkJ\nV1ZY9LjzWgoOJ9ZxDhSemJCa5z8pjN56qg1P6lF+1usBbRDOw+H8VrFkkY/hNLPY\nTkA6OLLCWaR/KUaUQR+jyNf8gEsUc6p+jnaGBMC3Ts6ucmVVtGFndrN7ySIA+5dV\nW8BkiFLB+gqeEFID/KFUJtUEX5rKOEvjq3Gwcf1KAfYS8pFKJ1KE4qnkv3qGovV6\njiQlwjBBAgMBAAECggEAArD72jfpN+bP8iFHDSH3YdQCT6g/5QFvqOkts5CqF0n2\noOGCvxvGDLFpCoSgpUaH1s7GL46gFWnzLHNOeifqbSNdYwSuUkohfldTZiiLHoKh\nJDDqXcsEcwoHd7M8+ScFhMmW/+zz8wjXslVgw6N0HjLmZeIM9LgmcjvgBr/2B34r\n/hsfmzz0dnj0uHOY7bVvlG5rIHphZ9whJVEmb9MrdpldBzrlGZ6S5vK4YScvNPXq\nKjCvQCfUg6OEdP9wbOu/1cXeoznK6jowriaA1rFTSWNzKRoBWnC9ECZJxm9lY4iA\nNaPvpgjHgj6j3rmdb2v5WQCCltSFGZwS9inpLTrfYQKBgQDUzyxu47/TXnz9L1hT\nIzqgJeow1HWiD+yCKbkCIxiGF5+2jSuNqDGU+0ZbTx34UKDgBrxIZeCiQjgPwQNe\n1c8pHGfo0Ng5q3mgIxGud1fFpo+Pc4145roftwFXjRazet+JkPC9ZCYf4qMcMwjL\nRt4dDxZB4L+1Nh4bZQSQr+FcYQKBgQDF+zcaE9wJO4Ey/NwPhXvi4bffrgSm8Wit\nyXl+3AwjQccHhOvPehJEvyDSzgToKFXt/0IYyPUHXJrPUPKA6na0U+/EgGTxE+us\nEMgPmHTlG+WbLz+24w70Yxl9cUWtFIlobC3HBNrB+vxR+cY36h83LsiwhUK84sbl\noJygaGhf4QKBgF79PqMcq7IoWfgVWwJ5FiEH63nyS7OUEgijoP4wNjEceGDesJMh\ngUgzxNra/NCrBLQarY5PUy56ClYV3HBHVZnPIR6NogZT4Q02uhy7DoWd7DSm6n4N\n1wRzBnlS89AXR5I7DQosmsveuNnMed9qeZhU9KVhMZEsX9HwjFIc/6XhAoGAFtpp\nwOPb+WDaCBWyHUSOSWE+xV4kAVVKfQ0NrjweVo+INvD7+2Ye57qcQlkvrdDCIofd\njFjeF+xznky8wW7PJv+tZKRhgoaHJMSHI224yJ2QwnoQw76wAjvSPG2v2kvNlLUw\nD5Ia4ltjdt77J4cp9Ue8OMwZKQ6QYP9KNSX4LmECgYEAvAt9oKfU2BBZnIfI0UWE\nftSeulNtK+HuvhnXUAFCWmHPJ9A2FCVjvlyVdNakAt+OSY9vsAQPmiEdy5h6ZTf2\nenbScMolkRUg/IU7H4H5D2FblvAUgWrq1gzUONhZV26EibgbKgL77xRKBebQ51os\nu22SjWlp4T82+ag6q8vhojg=\n-----END PRIVATE KEY-----\n").replace("\\n", "\n"),
    "client_email": os.getenv("GCP_CLIENT_EMAIL", "tcc-vertex-ai-augusto@acquired-router-470921-a3.iam.gserviceaccount.com"),    # ok hard-coded
    "client_id": os.getenv("GCP_CLIENT_ID", "101137983589899452195"),          # ok hard-coded
    "auth_uri": "https://accounts.google.com/o/oauth2/auth",
    "token_uri": "https://oauth2.googleapis.com/token",
    "auth_provider_x509_cert_url": "https://www.googleapis.com/oauth2/v1/certs",
    "client_x509_cert_url": os.getenv("GCP_CLIENT_X509_CERT_URL", "https://www.googleapis.com/robot/v1/metadata/x509/tcc-vertex-ai-augusto%40acquired-router-470921-a3.iam.gserviceaccount.com"),
    "universe_domain": "googleapis.com",
}


ROLE_DB_TO_PT = {value: key for key, value in ROLE_PT_TO_DB.items()}
