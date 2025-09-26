"""Shared helpers for rendering and managing chat history views."""

from __future__ import annotations

import os
import re
import tempfile
import unicodedata
from dataclasses import dataclass
from datetime import datetime
from typing import Any, Callable, Dict, List, Optional, Sequence, Tuple, Union

import gradio as gr

from services.docs import extractPdfText
from services.supabase_client import (
    SupabaseConfigurationError,
    SupabaseOperationError,
    add_chat_comment,
    download_file_from_bucket,
    record_auto_chat_evaluation,
)
from services.vertex_client import VERTEX_CFG, _vertex_err, generate_chat_evaluation

from app.config import SUPABASE_SERVICE_ROLE_KEY, SUPABASE_URL


@dataclass
class ChatLoadResult:
    """Normalized payload returned when loading a chat entry."""

    chat_id: Optional[str]
    chat: Optional[Dict[str, Any]]
    metadata_md: str
    summary_text: str
    preview_text: str
    evaluation_text: str
    comments_md: str
    transcript_text: str
    download_path: Optional[str]
    download_visible: bool
    notice: Optional[str]
    notice_is_error: bool


def _format_timestamp(value: Any) -> str:
    if value in (None, ""):
        return "—"
    if isinstance(value, (int, float)):
        try:
            return datetime.fromtimestamp(float(value)).strftime("%d/%m/%Y %H:%M")
        except (ValueError, OverflowError, OSError):
            return str(value)
    if isinstance(value, str):
        text = value.strip()
        if not text:
            return "—"
        try:
            if text.endswith("Z"):
                text = text.replace("Z", "+00:00")
            return datetime.fromisoformat(text).strftime("%d/%m/%Y %H:%M")
        except ValueError:
            return text
    if isinstance(value, datetime):
        return value.strftime("%d/%m/%Y %H:%M")
    return str(value)


def _subjects_label(entry: Dict[str, Any]) -> str:
    subjects = entry.get("subjects") or []
    if subjects:
        return ", ".join(subjects)
    free = entry.get("subject_free_text")
    if isinstance(free, str) and free.strip():
        return free.strip()
    topic = entry.get("topic_source")
    if isinstance(topic, str) and topic.strip():
        return topic.strip()
    return "—"


def _comments_markdown(comments: List[Dict[str, Any]]) -> str:
    if not comments:
        return "ℹ️ Nenhum comentário registrado ainda."
    lines = ["### Comentários dos professores"]
    for comment in comments:
        author = comment.get("author_name") or comment.get("author_login") or "Professor(a)"
        created = _format_timestamp(comment.get("created_at"))
        text = comment.get("text") or ""
        score = comment.get("score")
        score_label = ""
        try:
            if score is not None:
                score_label = f" (Nota: {float(score):.1f})"
        except (TypeError, ValueError):
            score_label = ""
        lines.append(f"- **{author}{score_label}** — {created}: {text}")
    return "\n".join(lines)


_SPEAKER_PATTERN = re.compile(r"^\s*([\wÀ-ÖØ-öø-ÿ]+(?:\s+[\wÀ-ÖØ-öø-ÿ]+)*)\s*:\s*")


def _normalize_speaker(label: str) -> Optional[str]:
    base = "".join(
        char for char in unicodedata.normalize("NFKD", label) if not unicodedata.combining(char)
    ).lower()

    for needle, key in (
        ("aplic", "aplicacao"),
        ("assist", "assistente"),
        ("usuario", "usuario"),
        ("user", "usuario"),
        ("app", "aplicacao"),
        ("aluno", "aluno"),
        ("professor", "professor"),
        ("tutor", "tutor"),
        ("sistema", "sistema"),
    ):
        if needle in base:
            return key
    return base.strip() or None


def _format_transcript_markdown(transcript: str) -> str:
    if not transcript:
        return ""

    normalized_lines = transcript.replace("\r\n", "\n").replace("\r", "\n").split("\n")
    formatted_lines: List[str] = []
    previous_speaker: Optional[str] = None

    for raw_line in normalized_lines:
        line = raw_line.rstrip()
        match = _SPEAKER_PATTERN.match(line)
        speaker_key: Optional[str] = None
        if match:
            speaker_key = _normalize_speaker(match.group(1))

        if (
            formatted_lines
            and speaker_key
            and previous_speaker
            and speaker_key != previous_speaker
            and formatted_lines[-1] != ""
        ):
            formatted_lines.append("")

        formatted_lines.append(line)

        if speaker_key:
            previous_speaker = speaker_key

    return "\n".join(formatted_lines).strip()


HISTORY_TABLE_HEADERS: Tuple[str, ...] = (
    "Aluno",
    "Sala",
    "Assuntos",
    "Resumo",
    "Nota",
    "Iniciado em",
)


def _history_table_data(entries: List[Dict[str, Any]]) -> List[List[str]]:
    table: List[List[str]] = []
    for chat in entries:
        student = chat.get("student_name") or chat.get("student_login") or "—"
        classroom = chat.get("classroom_name") or chat.get("classroom_id") or "—"
        subjects = _subjects_label(chat)
        summary = chat.get("summary_preview") or chat.get("summary") or ""
        grade = chat.get("grade")
        if grade in (None, ""):
            comments = chat.get("teacher_comments") or []
            if comments:
                latest = comments[-1]
                grade = latest.get("score")
        grade_txt = "—"
        try:
            if grade not in (None, ""):
                grade_txt = f"{float(grade):.1f}"
        except (TypeError, ValueError):
            grade_txt = str(grade)
        started = _format_timestamp(chat.get("started_at"))
        table.append([student, classroom, subjects, summary, grade_txt, started])
    return table


def prepare_history_listing(
    chats: Optional[List[Dict[str, Any]]],
    *,
    column_labels: Sequence[str],
    filter_fn: Optional[Callable[[Dict[str, Any]], bool]],
    dropdown_label: Callable[[Dict[str, Any]], str],
    dropdown_value_key: str = "id",
    empty_message: str,
    found_message: Union[str, Callable[[int], str]] = "✅ {count} chat(s) encontrados.",
) -> Tuple[Any, List[Dict[str, Any]], Any, str, Optional[str]]:
    """Normalize shared outputs for history listings.

    The helper centralizes filtering, table rendering and dropdown preparation for
    history views.  The caller controls the label formatting, the value key used
    for the dropdown and feedback messages displayed to the user.
    """

    entries = list(chats or [])
    if filter_fn:
        filtered = [chat for chat in entries if filter_fn(chat)]
    else:
        filtered = entries

    table = _history_table_data(filtered)
    expected_cols = len(column_labels)
    if expected_cols:
        normalized_table: List[List[str]] = []
        for row in table:
            normalized = list(row[:expected_cols])
            if len(normalized) < expected_cols:
                normalized.extend([""] * (expected_cols - len(normalized)))
            normalized_table.append(normalized)
        table = normalized_table

    dropdown_choices: List[Tuple[str, Any]] = []
    for chat in filtered:
        value = chat.get(dropdown_value_key)
        if not value:
            continue
        dropdown_choices.append((dropdown_label(chat), value))

    default_value = dropdown_choices[0][1] if dropdown_choices else None

    if filtered:
        if callable(found_message):
            message = str(found_message(len(filtered)))
        else:
            try:
                message = str(found_message).format(count=len(filtered))
            except Exception:
                message = str(found_message)
    else:
        message = empty_message

    table_update = gr.update(value=table)
    dropdown_update = gr.update(choices=dropdown_choices, value=default_value)

    return table_update, filtered, dropdown_update, message, default_value


def _chat_metadata_md(chat: Dict[str, Any]) -> str:
    student = chat.get("student_name") or chat.get("student_login") or chat.get("student_id")
    classroom = chat.get("classroom_name") or chat.get("classroom_id") or "—"
    subjects = _subjects_label(chat)
    started = _format_timestamp(chat.get("started_at"))
    ended = _format_timestamp(chat.get("ended_at"))
    goal = chat.get("student_goal") or "—"
    interest = chat.get("student_interest") or "—"
    lines = [
        f"### Chat com {student}",
        f"- **Sala:** {classroom}",
        f"- **Assuntos:** {subjects}",
        f"- **Iniciado em:** {started}",
        f"- **Finalizado em:** {ended}",
        f"- **Objetivo do aluno:** {goal}",
        f"- **Interesses do aluno:** {interest}",
    ]
    auto_score = chat.get("auto_evaluation_score")
    try:
        if auto_score is not None:
            lines.append(f"- **Nota automática:** {float(auto_score):.1f}")
    except (TypeError, ValueError):
        lines.append(f"- **Nota automática:** {auto_score}")
    if chat.get("auto_evaluation_updated_at"):
        lines.append(
            f"- **Avaliação automática atualizada em:** {_format_timestamp(chat['auto_evaluation_updated_at'])}"
        )
    return "\n".join(lines)


def load_chat_entry(
    chat_id: Optional[str],
    history_entries: Optional[List[Dict[str, Any]]],
    current_download_path: Optional[str],
) -> ChatLoadResult:
    entries = history_entries or []
    chat = next((entry for entry in entries if entry.get("id") == chat_id), None)

    if not chat:
        if current_download_path and os.path.exists(current_download_path):
            try:
                os.remove(current_download_path)
            except OSError:
                pass
        return ChatLoadResult(
            chat_id=None,
            chat=None,
            metadata_md="⚠️ Selecione um chat válido.",
            summary_text="",
            preview_text="",
            evaluation_text="",
            comments_md="ℹ️ Nenhum comentário registrado ainda.",
            transcript_text="",
            download_path=None,
            download_visible=False,
            notice=None,
            notice_is_error=False,
        )

    bucket = chat.get("storage_bucket")
    path = chat.get("storage_path")
    transcript_text = ""
    download_path = None
    notice_msg: Optional[str] = None
    notice_is_error = False

    if bucket and path:
        try:
            pdf_bytes = download_file_from_bucket(
                SUPABASE_URL,
                SUPABASE_SERVICE_ROLE_KEY,
                bucket=bucket,
                storage_path=path,
            )
        except SupabaseConfigurationError:
            notice_msg = "⚠️ Configure o Supabase Storage para baixar o PDF do chat."
        except SupabaseOperationError as err:
            notice_msg = f"❌ Erro ao baixar PDF: {err}"
            notice_is_error = True
        else:
            try:
                with tempfile.NamedTemporaryFile(delete=False, suffix=".pdf") as tmp:
                    tmp.write(pdf_bytes)
                    download_path = tmp.name
                transcript_text = extractPdfText(download_path) or ""
            except Exception as exc:  # pragma: no cover - depende de I/O externo
                notice_msg = f"⚠️ Não foi possível ler o PDF: {exc}"
                download_path = None
                transcript_text = ""
    else:
        notice_msg = "ℹ️ Este chat não possui PDF armazenado."

    if current_download_path and current_download_path != download_path:
        if os.path.exists(current_download_path):
            try:
                os.remove(current_download_path)
            except OSError:
                pass

    chat["transcript_text"] = transcript_text
    transcript_markdown = _format_transcript_markdown(transcript_text)

    metadata = _chat_metadata_md(chat)
    comments_md = _comments_markdown(chat.get("teacher_comments") or [])
    evaluation_text = chat.get("auto_evaluation") or ""
    auto_score = chat.get("auto_evaluation_score")
    display_eval = evaluation_text
    try:
        if auto_score is not None:
            score_label = f"Nota automática: {float(auto_score):.1f}"
            display_eval = (
                f"{score_label}\n\n{evaluation_text}".strip()
                if evaluation_text
                else score_label
            )
    except (TypeError, ValueError):
        if auto_score not in (None, ""):
            score_label = f"Nota automática: {auto_score}"
            display_eval = (
                f"{score_label}\n\n{evaluation_text}".strip()
                if evaluation_text
                else score_label
            )
    evaluation_text = display_eval
    preview_text = transcript_markdown if transcript_markdown else ""
    if not preview_text:
        preview_text = "(PDF indisponível ou sem conteúdo.)"

    summary_text = chat.get("summary") or chat.get("summary_preview") or ""
    summary_text = summary_text.strip()

    return ChatLoadResult(
        chat_id=chat.get("id"),
        chat=chat,
        metadata_md=metadata,
        summary_text=summary_text,
        preview_text=preview_text,
        evaluation_text=evaluation_text,
        comments_md=comments_md,
        transcript_text=transcript_text,
        download_path=download_path,
        download_visible=bool(download_path),
        notice=notice_msg,
        notice_is_error=notice_is_error,
    )


def generate_auto_evaluation(
    chat_id: Optional[str],
    transcript: Optional[str],
    history_entries: Optional[List[Dict[str, Any]]],
):
    if not chat_id:
        return "", history_entries or [], None, "⚠️ Selecione um chat."
    if _vertex_err:
        return "", history_entries or [], None, f"⚠️ Vertex indisponível: {_vertex_err}"
    if not VERTEX_CFG:
        return "", history_entries or [], None, "⚠️ Configure as credenciais do Vertex para gerar avaliações."

    entries = history_entries or []
    chat = next((entry for entry in entries if entry.get("id") == chat_id), None)
    if not chat:
        return "", entries, None, "⚠️ Chat não encontrado."

    transcript_text = (transcript or chat.get("transcript_text") or "").strip()
    if not transcript_text:
        metadata = _chat_metadata_md(chat)
        return "", entries, metadata, "⚠️ Transcript do chat indisponível para avaliação."

    try:
        evaluation_payload = generate_chat_evaluation(
            transcript_text,
            VERTEX_CFG,
            subjects=chat.get("subjects") or [chat.get("subject_free_text")],
        )
    except Exception as exc:  # pragma: no cover - depende de chamada externa
        metadata = _chat_metadata_md(chat)
        return "", entries, metadata, f"❌ Erro ao gerar avaliação: {exc}"

    if isinstance(evaluation_payload, dict):
        evaluation_text = evaluation_payload.get("text") or ""
        evaluation_score = evaluation_payload.get("score")
        raw_response = evaluation_payload.get("raw")
    else:
        evaluation_text = str(evaluation_payload or "")
        evaluation_score = None
        raw_response = None

    persisted_entry: Optional[Dict[str, Any]] = None
    notice = "✅ Avaliação automática gerada."
    extra_payload = {
        "text": evaluation_text,
        "score": evaluation_score,
        "raw_response": raw_response,
        "subjects": chat.get("subjects") or [chat.get("subject_free_text")],
    }

    try:
        persisted_entry = record_auto_chat_evaluation(
            SUPABASE_URL,
            SUPABASE_SERVICE_ROLE_KEY,
            chat_id=chat_id,
            evaluation_text=evaluation_text,
            evaluation_score=evaluation_score,
            extra_payload=extra_payload,
        )
        stored_text = persisted_entry.get("text") or evaluation_text
        stored_score = persisted_entry.get("score", evaluation_score)
        stored_at = persisted_entry.get("created_at")
        notice = (
            f"✅ Avaliação automática registrada (nota {float(stored_score):.1f})."
            if stored_score is not None
            else "✅ Avaliação automática registrada."
        )
    except SupabaseConfigurationError:
        stored_text = evaluation_text
        stored_score = evaluation_score
        stored_at = None
        notice = "⚠️ Configure o Supabase para salvar a avaliação automaticamente."
    except SupabaseOperationError as err:
        stored_text = evaluation_text
        stored_score = evaluation_score
        stored_at = None
        notice = f"❌ Avaliação não salva no Supabase: {err}"

    if stored_at:
        chat["auto_evaluation_updated_at"] = stored_at
    else:
        chat["auto_evaluation_updated_at"] = datetime.utcnow().isoformat() + "Z"
    chat["auto_evaluation"] = stored_text
    chat["auto_evaluation_score"] = stored_score

    metadata = _chat_metadata_md(chat)

    display_text = stored_text or ""
    final_score = stored_score
    try:
        if final_score is not None:
            prefix = f"Nota automática: {float(final_score):.1f}"
            display_text = f"{prefix}\n\n{display_text}".strip() if display_text else prefix
    except (TypeError, ValueError):
        if final_score not in (None, ""):
            prefix = f"Nota automática: {final_score}"
            display_text = f"{prefix}\n\n{display_text}".strip() if display_text else prefix

    return display_text, entries, metadata, notice


def append_chat_comment(
    chat_id: Optional[str],
    rating: Any,
    comment_text: str,
    history_entries: Optional[List[Dict[str, Any]]],
    *,
    author_id: Optional[str],
    author_login: Optional[str],
    author_name: Optional[str],
):
    text = (comment_text or "").strip()
    if not chat_id:
        return history_entries or [], None, "⚠️ Selecione um chat."
    if not text:
        return history_entries or [], None, "⚠️ Escreva um comentário antes de enviar."

    try:
        numeric_rating = float(rating)
    except (TypeError, ValueError):
        return (
            history_entries or [],
            None,
            "⚠️ Informe uma nota numérica antes de registrar o comentário.",
        )

    try:
        entry = add_chat_comment(
            SUPABASE_URL,
            SUPABASE_SERVICE_ROLE_KEY,
            chat_id=chat_id,
            author_id=author_id,
            author_login=author_login,
            author_name=author_name,
            text=text,
            score=numeric_rating,
        )
    except SupabaseConfigurationError:
        return history_entries or [], None, "⚠️ Configure SUPABASE_URL e SUPABASE_SERVICE_ROLE_KEY para salvar comentários."
    except SupabaseOperationError as err:
        return history_entries or [], None, f"❌ Erro ao salvar comentário: {err}"

    updated: List[Dict[str, Any]] = []
    comments_md = ""
    for chat in history_entries or []:
        if chat.get("id") == chat_id:
            comments = list(chat.get("teacher_comments") or [])
            comments.append(entry)
            comments.sort(key=lambda item: item.get("created_at") or "")
            chat["teacher_comments"] = comments
            comments_md = _comments_markdown(comments)
        updated.append(chat)
    message = "✅ Comentário registrado."
    score = entry.get("score")
    try:
        if score is not None:
            message = f"✅ Comentário registrado com nota {float(score):.1f}."
    except (TypeError, ValueError):
        if score not in (None, ""):
            message = f"✅ Comentário registrado com nota {score}."

    return updated, comments_md, message


def prepare_download(download_path):
    if download_path and os.path.exists(download_path):
        return download_path
    return None


__all__ = [
    "HISTORY_TABLE_HEADERS",
    "ChatLoadResult",
    "_format_timestamp",
    "_subjects_label",
    "_comments_markdown",
    "_history_table_data",
    "prepare_history_listing",
    "_chat_metadata_md",
    "load_chat_entry",
    "generate_auto_evaluation",
    "append_chat_comment",
    "prepare_download",
]
