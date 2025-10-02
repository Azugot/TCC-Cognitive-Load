"""Upload e manipulação de arquivos no Storage do Supabase.

Perfis: professores, alunos e rotinas automáticas autorizadas a enviar arquivos.
"""

from __future__ import annotations

import mimetypes
import os
from typing import Any, Dict, Optional

from postgrest.exceptions import APIError

from .common import SupabaseOperationError, _get_client, _handle_api_error


def upload_file_to_bucket(
    url: str,
    key: str,
    *,
    bucket: str,
    file_path: str,
    storage_path: str,
    content_type: Optional[str] = None,
    upsert: bool = True,
) -> str:
    """Upload a file to a Supabase Storage bucket."""

    if not bucket or not bucket.strip():
        raise SupabaseOperationError("Bucket do Storage não informado para upload.")

    normalized_path = (storage_path or "").strip().lstrip("/")
    if not normalized_path:
        raise SupabaseOperationError("Caminho do arquivo no Storage não informado.")

    if not file_path or not os.path.isfile(file_path):
        raise SupabaseOperationError(f"Arquivo inexistente para upload: {file_path}")

    client = _get_client(url, key)

    if not content_type:
        guessed, _ = mimetypes.guess_type(file_path)
        content_type = guessed or "application/octet-stream"

    file_options: Dict[str, Any] = {
        "upsert": "true" if upsert else "false",
        "content-type": content_type,
    }

    try:
        with open(file_path, "rb") as fh:
            data = fh.read()

        resp = client.storage.from_(bucket).upload(
            path=normalized_path,
            file=data,
            file_options=file_options,
        )
    except APIError as err:
        raise _handle_api_error(err) from err
    except Exception as exc:
        raise SupabaseOperationError(
            f"Falha ao enviar arquivo ao bucket '{bucket}': {exc}"
        ) from exc

    if isinstance(resp, dict):
        stored_path = resp.get("path") or resp.get("Key") or normalized_path
    else:
        stored_path = normalized_path

    return stored_path


__all__ = ["upload_file_to_bucket"]


def download_file_from_bucket(
    url: str,
    key: str,
    *,
    bucket: str,
    storage_path: str,
) -> bytes:
    """Download a file from a Supabase Storage bucket."""

    if not bucket or not bucket.strip():
        raise SupabaseOperationError("Bucket do Storage não informado para download.")

    normalized_path = (storage_path or "").strip().lstrip("/")
    if not normalized_path:
        raise SupabaseOperationError("Caminho do arquivo no Storage não informado.")

    client = _get_client(url, key)

    try:
        data = client.storage.from_(bucket).download(normalized_path)
    except APIError as err:
        raise _handle_api_error(err) from err
    except Exception as exc:
        raise SupabaseOperationError(
            f"Falha ao baixar arquivo do bucket '{bucket}': {exc}"
        ) from exc

    if isinstance(data, bytes):
        return data

    # Alguns SDKs retornam objetos com o payload em "data" ou "content".
    for attr in ("data", "content"):
        value = getattr(data, attr, None)
        if isinstance(value, bytes):
            return value

    raise SupabaseOperationError(
        "Resposta inesperada ao baixar arquivo do Storage do Supabase."
    )


__all__.append("download_file_from_bucket")


def delete_file_from_bucket(
    url: str,
    key: str,
    *,
    bucket: str,
    storage_path: str,
) -> None:
    """Remove a file from a Supabase Storage bucket."""

    if not bucket or not bucket.strip():
        raise SupabaseOperationError("Bucket do Storage não informado para exclusão.")

    normalized_path = (storage_path or "").strip().lstrip("/")
    if not normalized_path:
        raise SupabaseOperationError("Caminho do arquivo no Storage não informado.")

    client = _get_client(url, key)

    try:
        client.storage.from_(bucket).remove([normalized_path])
    except APIError as err:
        raise _handle_api_error(err) from err
    except Exception as exc:
        raise SupabaseOperationError(
            f"Falha ao remover arquivo do bucket '{bucket}': {exc}"
        ) from exc


__all__.append("delete_file_from_bucket")
