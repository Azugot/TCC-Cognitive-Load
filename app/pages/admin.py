"""Admin area utilities and Gradio view builders."""

from __future__ import annotations
import os
import re
import tempfile
import zipfile
from collections import defaultdict
from dataclasses import dataclass
from typing import Any, Dict, Iterable, List, Optional, Tuple

import gradio as gr
from reportlab.lib.pagesizes import letter
from reportlab.lib.styles import ParagraphStyle, getSampleStyleSheet
from reportlab.platypus import Paragraph, SimpleDocTemplate, Spacer

from services.docs import extractPdfText
from services.supabase_client import (
    SupabaseConfigurationError,
    SupabaseOperationError,
    create_classroom_record,
    create_subject_record,
    delete_classroom_record,
    fetch_classroom_domain,
    list_all_chats,
    remove_classroom_student,
    remove_classroom_teacher,
    set_classroom_theme_config,
    update_classroom_record,
    update_subject_active,
    upsert_classroom_student,
    upsert_classroom_teacher,
)
from services.supabase.storage import download_file_from_bucket
from services.vertex_client import VERTEX_CFG, _vertex_err, _vertex_init_or_raise

from app.config import (
    ROLE_PT_TO_DB,
    SUPABASE_CLASS_DOCS_BUCKET,
    SUPABASE_CLASS_DOCS_PREFIX,
    SUPABASE_SERVICE_ROLE_KEY,
    SUPABASE_URL,
    SUPABASE_USERS_TABLE,
)
from app.utils import (
    _auth_matches_classroom_owner,
    _auth_matches_classroom_teacher,
    _auth_user_id,
    _class_member_labels,
    _get_class_by_id,
    _is_admin,
    _legacy_owner_sentinel,
    _merge_notice,
    _normalize_username,
    _teacher_username,
)

from app.pages.history_shared import (
    _format_timestamp,
    _history_table_data,
    append_chat_comment,
    generate_auto_evaluation,
    load_chat_entry,
    prepare_download,
)


@dataclass
class AdminViews:
    home: gr.Column
    admin_page: gr.Column
    classrooms: gr.Column
    history: gr.Column
    evaluate: gr.Column
    progress: gr.Column
    btn_logout: gr.Button
    btn_admin_as_student: gr.Button
    btn_admin_list_students: gr.Button


def _render_classrooms_md(classrooms: Iterable[Dict[str, Any]]):
    if not classrooms:
        return "Warning: Nenhuma sala cadastrada ainda."
    lines = ["### Salas cadastradas\n"]
    for c in classrooms:
        status = "arquivada" if c.get("is_archived") else "ativa"
        lock = "🔒" if c.get("theme_locked") else "🔓"
        members = c.get("members", {"teachers": [], "students": []})
        tcount = len(members.get("teachers", []))
        scount = len(members.get("students", []))
        lines.append(
            f"- **{c['name']}** ({status}) — tema: _{c.get('theme_name', '?')}_ {lock} — id: `{c['id']}` — 👩‍🏫 {tcount} | 🎓 {scount}"
        )
    return "\n".join(lines)


def _render_subjects_md(subjects_by_class, selected_id, classrooms):
    if not classrooms:
        return "Warning: Cadastre uma sala primeiro."
    if not selected_id:
        return "Info: Selecione uma sala para gerenciar os subtemas."
    names = {c["id"]: c["name"] for c in classrooms}
    subjects = subjects_by_class.get(selected_id, [])
    title = f"### Subtemas da sala **{names.get(selected_id, '?')}**\n"
    if not subjects:
        return title + "Warning: Nenhum subtema cadastrado."
    bullets = []
    for s in subjects:
        mark = "OK:" if s.get("active") else "⏸️"
        bullets.append(f"- {mark} {s['name']}")
    return title + "\n".join(bullets)


def _admin_history_dropdown(classrooms, current_value=None):
    choices = [("Todas as salas", "")]
    for classroom in classrooms or []:
        cid = classroom.get("id")
        if not cid:
            continue
        label = classroom.get("name") or cid
        choices.append((label, cid))
    valid_ids = [value for _, value in choices]
    normalized = current_value or ""
    value = normalized if normalized in valid_ids else (choices[0][1] if choices else None)
    return gr.update(choices=choices, value=value)


def admin_history_dropdown(classrooms, current_value=None):
    return _admin_history_dropdown(classrooms, current_value)


def admin_history_refresh(auth, classroom_filter):
    if not _is_admin(auth):
        return (
            gr.update(value=[]),
            [],
            gr.update(choices=[], value=None),
            "Warning: Apenas administradores podem visualizar todos os chats.",
            None,
        )

    try:
        chats = list_all_chats(
            SUPABASE_URL,
            SUPABASE_SERVICE_ROLE_KEY,
            users_table=SUPABASE_USERS_TABLE,
        )
    except SupabaseConfigurationError:
        return (
            gr.update(value=[]),
            [],
            gr.update(choices=[], value=None),
            "Warning: Configure SUPABASE_URL e SUPABASE_SERVICE_ROLE_KEY para consultar o histórico.",
            None,
        )
    except SupabaseOperationError as err:
        return (
            gr.update(value=[]),
            [],
            gr.update(choices=[], value=None),
            f"ERROR: Erro ao consultar chats: {err}",
            None,
        )

    classroom_filter = (classroom_filter or "").strip()
    if classroom_filter:
        filtered = [chat for chat in chats if str(chat.get("classroom_id")) == classroom_filter]
    else:
        filtered = chats

    table = _history_table_data(filtered)
    dropdown_choices = []
    for chat in filtered:
        chat_id = chat.get("id")
        if not chat_id:
            continue
        student = chat.get("student_name") or chat.get("student_login") or "Aluno"
        classroom = chat.get("classroom_name") or chat.get("classroom_id") or "Sala"
        started = _format_timestamp(chat.get("started_at"))
        dropdown_choices.append((f"{student} — {classroom} — {started}", chat_id))

    default_id = dropdown_choices[0][1] if dropdown_choices else None
    message = (
        f"OK: {len(filtered)} chat(s) encontrados." if filtered else "Info: Nenhum chat para o filtro aplicado."
    )
    return (
        gr.update(value=table),
        filtered,
        gr.update(choices=dropdown_choices, value=default_id),
        message,
        default_id,
    )


def admin_history_load_chat(chat_id, history_entries, current_download_path):
    result = load_chat_entry(chat_id, history_entries, current_download_path)

    if result.notice:
        if result.notice.startswith("ERROR:"):
            gr.Error(result.notice)
        else:
            gr.Warning(result.notice)

    manual_value = 0
    preview_value = result.preview_text or "Info: Carregue um chat para visualizar a prévia."
    summary_value = result.summary_text or "Info: Este chat ainda não possui um resumo gerado."

    return (
        result.chat_id,
        gr.update(value=result.metadata_md),
        gr.update(value=summary_value),
        gr.update(value=preview_value),
        gr.update(value=result.evaluation_text),
        gr.update(value=manual_value),
        gr.update(value=result.comments_md),
        result.transcript_text,
        result.download_path,
        gr.update(visible=result.download_visible),
        gr.update(value=""),
    )


def admin_history_generate_evaluation(chat_id, transcript, history_entries):
    evaluation, entries, metadata, notice = generate_auto_evaluation(
        chat_id, transcript, history_entries
    )
    metadata_update = gr.update(value=metadata) if metadata is not None else gr.update()
    return gr.update(value=evaluation), entries, metadata_update, notice


def admin_history_add_comment(chat_id, rating, comment_text, history_entries, auth):
    login = _teacher_username(auth) or _normalize_username((auth or {}).get("username"))
    author_display = None
    if isinstance(auth, dict):
        author_display = (
            auth.get("full_name") or auth.get("display_name") or auth.get("username")
        )

    updated, comments_md, notice = append_chat_comment(
        chat_id,
        rating,
        comment_text,
        history_entries,
        author_id=_auth_user_id(auth),
        author_login=login,
        author_name=author_display,
    )

    if comments_md is None:
        return updated, gr.update(value=comment_text), gr.update(), notice

    return updated, gr.update(value=""), gr.update(value=comments_md), notice


def admin_history_prepare_download(download_path):
    path = prepare_download(download_path)
    if path:
        return path
    gr.Warning("Warning: Nenhum arquivo disponível para download.")
    return None


def _classroom_dropdown_choices(classrooms):
    choices = []
    for cls in classrooms or []:
        cid = cls.get("id")
        if cid:
            label = cls.get("name") or cid
            choices.append((label, cid))
    return choices


def _group_chats_by_student_md(chats: List[Dict[str, Any]]) -> str:
    if not chats:
        return "Info: Nenhum chat disponível para a sala selecionada."

    grouped: Dict[str, List[Dict[str, Any]]] = defaultdict(list)
    for chat in chats:
        student = chat.get("student_name") or chat.get("student_login") or "Aluno"
        grouped[student].append(chat)

    lines = ["### Chats por aluno"]
    for student, items in sorted(grouped.items()):
        lines.append(f"- **{student}**")
        for chat in items:
            subjects = ", ".join(chat.get("subjects") or []) or (chat.get("subject_free_text") or "—")
            started = _format_timestamp(chat.get("started_at"))
            eval_badge = " ✅ auto-eval" if chat.get("auto_evaluation") else ""
            has_pdf = "📄" if chat.get("storage_bucket") and chat.get("storage_path") else "⚠️ sem PDF"
            lines.append(
                f"  - {has_pdf} {started} — assuntos: {subjects}{eval_badge} (id: `{chat.get('id')}`)"
            )
    return "\n".join(lines)


def _spinner_html(message: str) -> str:
    return f"""
<div style='display:flex;align-items:center;gap:8px;'>
  <div style='width:18px;height:18px;border:3px solid #d1d5db;border-top-color:#4f46e5;border-radius:9999px;animation:spin 0.8s linear infinite'></div>
  <div>{message}</div>
</div>
<style>
@keyframes spin { from { transform: rotate(0deg);} to { transform: rotate(360deg);} }
</style>
"""


def admin_load_classroom_chats(auth, classrooms, classroom_id, selected_ids=None):
    if not _is_admin(auth):
        return (
            gr.update(),
            gr.update(value=[]),
            [],
            gr.update(choices=[], value=[]),
            "Warning: Apenas administradores podem carregar chats de sala.",
        )

    choices = _classroom_dropdown_choices(classrooms)
    valid_ids = [str(cid) for _, cid in choices]
    normalized_id = str(classroom_id) if classroom_id not in (None, "") else None
    target_id = normalized_id if normalized_id in valid_ids else None
    if target_id is None and valid_ids:
        # Keep the currently selected classroom if it's valid; otherwise, fall back to the
        # first available option so the chat list is never empty when classrooms exist.
        target_id = valid_ids[0]

    try:
        chats = list_all_chats(
            SUPABASE_URL,
            SUPABASE_SERVICE_ROLE_KEY,
            users_table=SUPABASE_USERS_TABLE,
        )
    except SupabaseConfigurationError:
        return (
            gr.update(choices=choices, value=target_id),
            gr.update(value=[]),
            [],
            gr.update(choices=[], value=[]),
            "Warning: Configure SUPABASE_URL e SUPABASE_SERVICE_ROLE_KEY para consultar os chats.",
        )
    except SupabaseOperationError as err:
        return (
            gr.update(choices=choices, value=target_id),
            gr.update(value=[]),
            [],
            gr.update(choices=[], value=[]),
            f"ERROR: Erro ao buscar chats: {err}",
        )

    filtered = [chat for chat in chats if str(chat.get("classroom_id")) == str(target_id)] if target_id else []

    listing_md = _group_chats_by_student_md(filtered)
    options = []
    for chat in filtered:
        label = _format_timestamp(chat.get("started_at")) or chat.get("id") or "Chat"
        student = chat.get("student_name") or chat.get("student_login") or "Aluno"
        subjects = ", ".join(chat.get("subjects") or []) or (chat.get("subject_free_text") or "—")
        options.append((f"{student} — {label} — {subjects}", chat.get("id")))

    if not target_id:
        message = "Info: Selecione uma sala para carregar os chats."
    else:
        message = (
            f"OK: {len(filtered)} chat(s) encontrados para a sala." if filtered else "Info: Nenhum chat encontrado para a sala."
        )

    selected_ids = {str(val) for val in (selected_ids or [])}
    valid_selected = [val for _, val in options if str(val) in selected_ids]

    return (
        gr.update(choices=choices, value=target_id),
        gr.update(value=listing_md),
        filtered,
        gr.update(choices=options, value=valid_selected),
        message,
    )


def _safe_component(name: str) -> str:
    if not name:
        return "desconhecido"
    cleaned = re.sub(r"[^\w\-\.]+", "_", str(name))
    return cleaned.strip("_ ") or "desconhecido"


def _download_chat_pdf_tmp(chat: Dict[str, Any]) -> Optional[str]:
    bucket = chat.get("storage_bucket")
    path = chat.get("storage_path")
    if not bucket or not path:
        return None

    pdf_bytes = download_file_from_bucket(
        SUPABASE_URL, SUPABASE_SERVICE_ROLE_KEY, bucket=bucket, storage_path=path
    )
    tmpdir = tempfile.mkdtemp(prefix="chat-pdf-")
    filename = f"{_safe_component(chat.get('id')) or 'chat'}.pdf"
    target = os.path.join(tmpdir, filename)
    with open(target, "wb") as fp:
        fp.write(pdf_bytes)
    return target


def admin_prepare_chat_zip(selected_ids, chats_state, auth):
    if not _is_admin(auth):
        return gr.update(visible=False), "Warning: Apenas administradores podem baixar os PDFs."

    entries = [chat for chat in (chats_state or []) if chat.get("id") in (selected_ids or [])]
    if not entries:
        return gr.update(visible=False), "Warning: Selecione ao menos um chat com PDF."

    tmpdir = tempfile.mkdtemp(prefix="admin-zip-")
    zip_path = os.path.join(tmpdir, "chats.zip")
    skipped = []

    with zipfile.ZipFile(zip_path, "w", compression=zipfile.ZIP_DEFLATED) as zf:
        for chat in entries:
            pdf_path = None
            try:
                pdf_path = _download_chat_pdf_tmp(chat)
            except SupabaseConfigurationError:
                return gr.update(visible=False), "Warning: Configure o Supabase Storage para baixar os PDFs."
            except SupabaseOperationError as err:
                skipped.append(f"{chat.get('id')}: erro ao baixar ({err})")
            except Exception as exc:  # pragma: no cover - depende de I/O externo
                skipped.append(f"{chat.get('id')}: falha inesperada ({exc})")

            if not pdf_path:
                skipped.append(f"{chat.get('id')}: PDF ausente")
                continue

            class_folder = _safe_component(chat.get("classroom_name") or chat.get("classroom_id"))
            student_folder = _safe_component(chat.get("student_name") or chat.get("student_login"))
            rel_dir = os.path.join(class_folder, student_folder)
            rel_path = os.path.join(rel_dir, os.path.basename(pdf_path))
            zf.write(pdf_path, rel_path)

    notice_parts = ["OK: ZIP gerado para download."]
    if skipped:
        notice_parts.append("Alguns chats foram ignorados: " + "; ".join(skipped))

    return gr.update(value=zip_path, visible=True), "\n".join(notice_parts)


def _compose_chat_context(chat: Dict[str, Any], transcript: str) -> str:
    subjects = ", ".join(chat.get("subjects") or []) or (chat.get("subject_free_text") or "—")
    goal = chat.get("student_goal") or "—"
    interest = chat.get("student_interest") or "—"
    auto_eval = (chat.get("auto_evaluation") or "").strip()
    student = chat.get("student_name") or chat.get("student_login") or "Aluno"
    classroom = chat.get("classroom_name") or chat.get("classroom_id") or "Sala"

    lines = [
        f"[CHAT {chat.get('id')}] {student} — {classroom}",
        f"Assuntos: {subjects}",
        f"Objetivo do aluno: {goal}",
        f"Interesses do aluno: {interest}",
    ]
    if auto_eval:
        lines.append(f"Avaliação automática existente: {auto_eval}")
    if transcript:
        lines.append("Transcript (PDF):")
        lines.append(transcript)
    return "\n".join(lines)


def _write_vertex_pdf(content: str, title: str = "Resposta do Vertex", footer: str = "") -> str:
    tmpdir = tempfile.mkdtemp(prefix="vertex-out-")
    output = os.path.join(tmpdir, "vertex_resposta.pdf")

    doc = SimpleDocTemplate(output, pagesize=letter, leftMargin=36, rightMargin=36, topMargin=36, bottomMargin=36)
    styles = getSampleStyleSheet()
    body: List[Any] = []

    title_style = ParagraphStyle("title", parent=styles["Heading1"], alignment=1, spaceAfter=12)
    body.append(Paragraph(title, title_style))
    body.append(Spacer(1, 12))

    for paragraph in (content or "Sem resposta gerada.").split("\n\n"):
        safe_text = paragraph.replace("\n", "<br/>")
        body.append(Paragraph(safe_text, styles["Normal"]))
        body.append(Spacer(1, 6))

    if footer:
        footer_style = ParagraphStyle("footer", parent=styles["Normal"], textColor="#555555", fontSize=9)
        body.append(Spacer(1, 12))
        body.append(Paragraph(footer, footer_style))

    doc.build(body)
    return output


def admin_vertex_summarize_chats(
    selected_ids,
    chats_state,
    auth,
    instructions,
    model_name,
    temperature,
    top_p,
    top_k,
    max_output_tokens,
):
    if not _is_admin(auth):
        return (
            gr.update(visible=False),
            "Warning: Apenas administradores podem usar o Vertex nesta área.",
            gr.update(value="", visible=False),
        )

    if _vertex_err:
        return (
            gr.update(visible=False),
            f"Warning: Vertex indisponível: {_vertex_err}",
            gr.update(value="", visible=False),
        )
    if not VERTEX_CFG:
        return (
            gr.update(visible=False),
            "Warning: Configure o Vertex AI no arquivo de credenciais.",
            gr.update(value="", visible=False),
        )

    entries = [chat for chat in (chats_state or []) if chat.get("id") in (selected_ids or [])]
    if not entries:
        return (
            gr.update(visible=False),
            "Warning: Selecione ao menos um chat para enviar ao Vertex.",
            gr.update(value="", visible=False),
        )

    cfg = dict(VERTEX_CFG)
    if model_name and isinstance(model_name, str):
        cfg["model"] = model_name.strip()

    try:
        model = _vertex_init_or_raise(cfg)
    except Exception as exc:  # pragma: no cover - depende de dependências externas
        return (
            gr.update(visible=False),
            f"ERROR: Não foi possível inicializar o Vertex: {exc}",
            gr.update(value="", visible=False),
        )

    contexts = []
    for chat in entries:
        transcript = ""
        try:
            pdf_path = _download_chat_pdf_tmp(chat)
            if pdf_path:
                transcript = extractPdfText(pdf_path) or ""
        except Exception as exc:  # pragma: no cover - depende de I/O externo
            transcript = f"[Falha ao ler PDF: {exc}]"
        contexts.append(_compose_chat_context(chat, transcript))

    prompt_parts = ["Você receberá detalhes de chats de alunos."]
    if instructions:
        prompt_parts.append(str(instructions))
    prompt_parts.append("Conteúdo dos chats:")
    prompt_parts.extend([f"\n\n{ctx}" for ctx in contexts])
    prompt = "\n\n".join(prompt_parts)

    gen_cfg: Dict[str, Any] = {}
    try:
        if temperature not in (None, ""):
            gen_cfg["temperature"] = float(temperature)
        if top_p not in (None, ""):
            gen_cfg["top_p"] = float(top_p)
        if top_k not in (None, ""):
            gen_cfg["top_k"] = int(top_k)
        if max_output_tokens not in (None, ""):
            gen_cfg["max_output_tokens"] = int(max_output_tokens)
    except Exception:
        gen_cfg = {}

    try:
        response = model.generate_content(prompt, generation_config=gen_cfg or None)
        text = getattr(response, "text", None) or str(response)
    except Exception as exc:  # pragma: no cover - depende de serviço externo
        return (
            gr.update(visible=False),
            f"ERROR: Erro ao chamar o Vertex: {exc}",
            gr.update(value="", visible=False),
        )

    footer = f"Modelo: {cfg.get('model')} — Chats: {len(entries)}"
    pdf_path = _write_vertex_pdf(text, footer=footer)
    return (
        gr.update(value=pdf_path, visible=True),
        "OK: Resposta gerada pelo Vertex.",
        gr.update(value=text, visible=True),
    )


def _render_eval_md(chat):
    if not chat:
        return "Warning: Selecione um chat para avaliar."
    prev = []
    if chat.get("score") is not None:
        prev.append(f"- Nota atual: **{chat['score']}**")
    if chat.get("rubric"):
        prev.append(f"- Rubrica: {chat['rubric']}")
    if chat.get("feedback"):
        prev.append(f"- Feedback: {chat['feedback']}")
    return "### Avaliação atual\n" + ("\n".join(prev) if prev else "Sem avaliação registrada.")


def _load_domain_state(current_classrooms=None, current_subjects=None):
    try:
        raw_classrooms, raw_subjects = fetch_classroom_domain(
            SUPABASE_URL,
            SUPABASE_SERVICE_ROLE_KEY,
            users_table=SUPABASE_USERS_TABLE,
        )
    except SupabaseConfigurationError:
        warn = "Warning: Configure SUPABASE_URL e SUPABASE_SERVICE_ROLE_KEY para gerenciar as salas."
        return current_classrooms or [], current_subjects or {}, warn
    except SupabaseOperationError as err:
        warn = f"ERROR: Erro ao consultar salas: {err}"
        return current_classrooms or [], current_subjects or {}, warn

    normalized_classrooms = []
    for item in raw_classrooms:
        teacher_ids: List[str] = []
        teacher_labels: Dict[str, str] = {}
        teacher_usernames: Dict[str, str] = {}
        for entry in item.get("teachers", []) or []:
            uid = entry.get("user_id")
            if not uid:
                continue
            username = (entry.get("username") or entry.get("login") or "").strip()
            display = (
                entry.get("full_name")
                or entry.get("display_name")
                or entry.get("name")
                or ""
            ).strip()
            teacher_ids.append(uid)
            if display:
                teacher_labels[uid] = display
            elif username:
                teacher_labels[uid] = username
            teacher_usernames[uid] = username or teacher_usernames.get(uid, "")

        student_ids: List[str] = []
        student_labels: Dict[str, str] = {}
        student_usernames: Dict[str, str] = {}
        for entry in item.get("students", []) or []:
            uid = entry.get("user_id")
            if not uid:
                continue
            status = str(entry.get("status", "active")).lower()
            if status != "active":
                continue
            username = (entry.get("username") or entry.get("login") or "").strip()
            display = (
                entry.get("full_name")
                or entry.get("display_name")
                or entry.get("name")
                or ""
            ).strip()
            student_ids.append(uid)
            if display:
                student_labels[uid] = display
            elif username:
                student_labels[uid] = username
            student_usernames[uid] = username or student_usernames.get(uid, "")

        owner_id = item.get("owner_id")
        owner_login = (item.get("owner_login") or "").strip()
        owner_username = (item.get("owner_username") or "").strip()
        if owner_id and owner_id not in teacher_ids:
            teacher_ids.append(owner_id)
        if owner_id and owner_login:
            teacher_usernames.setdefault(owner_id, owner_login)
        if owner_id and owner_username and owner_id not in teacher_usernames:
            teacher_usernames.setdefault(owner_id, owner_username)
        owner_label = None
        if owner_id:
            owner_label = teacher_labels.get(owner_id)
            if not owner_label:
                for entry in item.get("teachers", []) or []:
                    if entry.get("user_id") == owner_id:
                        owner_label = (
                            entry.get("full_name")
                            or entry.get("display_name")
                            or entry.get("name")
                            or ""
                        ).strip()
                        username = (entry.get("username") or entry.get("login") or "").strip()
                        if username:
                            teacher_usernames.setdefault(owner_id, username)
                        break
            if not owner_label:
                owner_label = owner_username or owner_login or owner_id
            teacher_labels.setdefault(owner_id, owner_label)

        legacy_owner_id = None
        if not owner_id and owner_login:
            legacy_owner_id = _legacy_owner_sentinel(owner_login)
            if legacy_owner_id:
                if legacy_owner_id not in teacher_ids:
                    teacher_ids.append(legacy_owner_id)
                teacher_usernames.setdefault(legacy_owner_id, owner_login)
                if owner_username:
                    teacher_labels.setdefault(legacy_owner_id, owner_username)
                else:
                    teacher_labels.setdefault(legacy_owner_id, owner_login)

        documents_entries: List[Dict[str, Any]] = []
        documents_map: Dict[str, Dict[str, Any]] = {}
        for doc in item.get("documents", []) or []:
            doc_id = doc.get("id")
            uploader_label = (
                doc.get("uploader_name")
                or doc.get("uploader_login")
                or doc.get("uploader_username")
                or doc.get("uploaded_by")
                or ""
            )
            entry = {
                "id": doc_id,
                "classroom_id": doc.get("classroom_id") or item.get("id"),
                "filename": doc.get("file_name") or doc.get("filename") or "",
                "storage_path": doc.get("storage_path") or "",
                "description": (doc.get("description") or "").strip(),
                "uploaded_by": doc.get("uploaded_by"),
                "uploaded_by_login": doc.get("uploader_login"),
                "uploaded_by_name": doc.get("uploader_name"),
                "uploaded_by_username": doc.get("uploader_username"),
                "uploaded_label": uploader_label,
                "created_at": doc.get("created_at"),
                "updated_at": doc.get("updated_at"),
                "storage_bucket": SUPABASE_CLASS_DOCS_BUCKET,
                "storage_prefix": SUPABASE_CLASS_DOCS_PREFIX,
            }
            documents_entries.append(entry)
            if doc_id:
                documents_map[doc_id] = entry

        normalized_classrooms.append(
            {
                "id": item.get("id"),
                "name": item.get("name"),
                "description": item.get("description") or "",
                "theme_name": item.get("theme_name") or item.get("name"),
                "theme_config": item.get("theme_config") or {},
                "theme_locked": bool(item.get("theme_locked")),
                "is_archived": bool(item.get("is_archived")),
                "members": {
                    "teachers": sorted({uid for uid in teacher_ids if uid}),
                    "students": sorted({uid for uid in student_ids if uid}),
                    "teacher_labels": {
                        uid: label for uid, label in teacher_labels.items() if uid
                    },
                    "student_labels": {
                        uid: label for uid, label in student_labels.items() if uid
                    },
                    "teacher_usernames": {
                        uid: username
                        for uid, username in teacher_usernames.items()
                        if uid
                    },
                    "student_usernames": {
                        uid: username
                        for uid, username in student_usernames.items()
                        if uid
                    },
                },
                "owner_id": owner_id,
                "owner_username": owner_username or teacher_labels.get(owner_id or "") or None,
                "owner_login": owner_login or teacher_usernames.get(owner_id or "") or None,
                "documents": list(item.get("documents") or []),
                "documents_map": documents_map,
                "documents_count": len(documents_entries),
                "documents_bucket": SUPABASE_CLASS_DOCS_BUCKET,
                "documents_prefix": SUPABASE_CLASS_DOCS_PREFIX,
            }
        )

    normalized_classrooms.sort(key=lambda cls: (cls.get("name") or "").lower())

    normalized_subjects = {}
    for cid, entries in raw_subjects.items():
        normalized_subjects[cid] = [
            {
                "id": entry.get("id"),
                "name": entry.get("name"),
                "active": bool(entry.get("is_active", True)),
            }
            for entry in entries
        ]
        normalized_subjects[cid].sort(key=lambda item: (item.get("name") or "").lower())

    return normalized_classrooms, normalized_subjects, ""


def _refresh_states(current_classrooms=None, current_subjects=None):
    classrooms, subjects, notice = _load_domain_state(current_classrooms, current_subjects)
    return classrooms, subjects, notice


def _sync_domain_after_auth(auth, classrooms, subjects):
    classes, subjects_map, notice = _refresh_states(classrooms, subjects)
    if notice:
        print(f"[SUPABASE] {notice}")
    return classes, subjects_map


def _admin_classrooms_outputs(
    classrooms,
    notice="",
    current_edit=None,
    current_members=None,
):
    md = _render_classrooms_md(classrooms or [])
    md = _merge_notice(md, notice)
    dd1, dd2 = _refresh_cls_dropdown(
        classrooms or [],
        current_edit=current_edit,
        current_members=current_members,
    )
    return md, dd1, dd2


def _refresh_cls_dropdown(classrooms, current_edit=None, current_members=None):
    choices = [
        (c.get("name") or c.get("id") or "Sala", c.get("id"))
        for c in (classrooms or [])
        if c.get("id")
    ]
    valid_ids = [value for _, value in choices]

    def _safe(current):
        if current in valid_ids:
            return current
        return valid_ids[0] if valid_ids else None

    return (
        gr.update(choices=choices, value=_safe(current_edit)),
        gr.update(choices=choices, value=_safe(current_members)),
    )


def add_classroom(
    name,
    theme,
    desc,
    locked,
    classrooms,
    subjects,
    auth,
    current_edit=None,
    current_members=None,
):
    role = (auth or {}).get("role")
    if (role or "").lower() not in ("admin", "professor"):
        md, dd1, dd2 = _admin_classrooms_outputs(
            classrooms,
            "⛔ Apenas professores ou admins podem criar salas.",
            current_edit=current_edit,
            current_members=current_members,
        )
        return classrooms, subjects, md, dd1, dd2

    creator_id = _auth_user_id(auth)
    if not creator_id:
        md, dd1, dd2 = _admin_classrooms_outputs(
            classrooms,
            "Warning: Faça login para criar salas.",
            current_edit=current_edit,
            current_members=current_members,
        )
        return classrooms, subjects, md, dd1, dd2

    name = (name or "").strip()
    theme = (theme or "").strip() or name
    description = (desc or "").strip() or ""
    if not name:
        md, dd1, dd2 = _admin_classrooms_outputs(
            classrooms,
            "Warning: Informe um nome para a sala.",
            current_edit=current_edit,
            current_members=current_members,
        )
        return classrooms, subjects, md, dd1, dd2

    try:
        created = create_classroom_record(
            SUPABASE_URL,
            SUPABASE_SERVICE_ROLE_KEY,
            name=name,
            description=description,
            theme_name=theme,
            theme_locked=bool(locked),
            created_by=creator_id,
        )
    except SupabaseConfigurationError:
        md, dd1, dd2 = _admin_classrooms_outputs(
            classrooms,
            "Warning: Configure SUPABASE_URL e SUPABASE_SERVICE_ROLE_KEY para cadastrar salas.",
            current_edit=current_edit,
            current_members=current_members,
        )
        return classrooms, subjects, md, dd1, dd2
    except SupabaseOperationError as err:
        md, dd1, dd2 = _admin_classrooms_outputs(
            classrooms,
            f"ERROR: Erro ao criar sala: {err}",
            current_edit=current_edit,
            current_members=current_members,
        )
        return classrooms, subjects, md, dd1, dd2

    classroom_id = (created or {}).get("id")
    if role and role.lower() == "professor" and classroom_id and creator_id:
        try:
            upsert_classroom_teacher(
                SUPABASE_URL,
                SUPABASE_SERVICE_ROLE_KEY,
                classroom_id=classroom_id,
                teacher_id=creator_id,
                role_label="owner",
            )
        except SupabaseConfigurationError:
            pass
        except SupabaseOperationError as err:
            print(f"[SUPABASE] Falha ao registrar professor responsável: {err}")

    classes, subjects_map, notice = _refresh_states(classrooms, subjects)
    selected_edit = classroom_id or current_edit
    md, dd1, dd2 = _admin_classrooms_outputs(
        classes,
        notice or "OK: Sala criada.",
        current_edit=selected_edit,
        current_members=current_members,
    )
    return classes, subjects_map, md, dd1, dd2


def refresh_classrooms(classrooms, subjects, current_edit=None, current_members=None):
    classes, subjects_map, notice = _refresh_states(classrooms, subjects)
    md, dd1, dd2 = _admin_classrooms_outputs(
        classes,
        notice,
        current_edit=current_edit,
        current_members=current_members,
    )
    return classes, subjects_map, md, dd1, dd2


def load_cls_for_edit(cls_id, classrooms):
    c = next((x for x in (classrooms or []) if x["id"] == cls_id), None)
    if not c:
        return (
            gr.update(value=""),
            gr.update(value=""),
            gr.update(value=""),
            gr.update(value=True),
            gr.update(value=False),
            "Warning: Sala não encontrada.",
        )
    return (
        gr.update(value=c["name"]),
        gr.update(value=c["theme_name"]),
        gr.update(value=c["description"]),
        gr.update(value=c["theme_locked"]),
        gr.update(value=c["is_archived"]),
        "",
    )


def save_cls(cls_id, name, theme, desc, locked, archived, classrooms, subjects):
    if not cls_id:
        md = _merge_notice(_render_classrooms_md(classrooms or []), "Warning: Selecione uma sala.")
        return classrooms, subjects, md

    original = next((c for c in (classrooms or []) if c.get("id") == cls_id), None)
    if not original:
        md = _merge_notice(_render_classrooms_md(classrooms or []), "Warning: Sala não encontrada.")
        return classrooms, subjects, md

    payload = {
        "name": (name or "").strip() or original.get("name"),
        "theme_name": (theme or "").strip() or original.get("theme_name"),
        "description": (desc or "").strip() or "",
        "theme_locked": bool(locked),
        "is_archived": bool(archived),
    }

    try:
        update_classroom_record(
            SUPABASE_URL,
            SUPABASE_SERVICE_ROLE_KEY,
            cls_id,
            **payload,
        )
    except SupabaseConfigurationError:
        md = _merge_notice(
            _render_classrooms_md(classrooms or []),
            "Warning: Configure SUPABASE_URL e SUPABASE_SERVICE_ROLE_KEY para atualizar salas.",
        )
        return classrooms, subjects, md
    except SupabaseOperationError as err:
        md = _merge_notice(
            _render_classrooms_md(classrooms or []),
            f"ERROR: Erro ao atualizar sala: {err}",
        )
        return classrooms, subjects, md

    classes, subjects_map, notice = _refresh_states(classrooms, subjects)
    md = _merge_notice(_render_classrooms_md(classes), notice or "OK: Sala atualizada.")
    return classes, subjects_map, md


def delete_cls(cls_id, classrooms, subjects):
    if not cls_id:
        md = _merge_notice(_render_classrooms_md(classrooms or []), "Warning: Selecione uma sala.")
        return classrooms, subjects, md

    try:
        delete_classroom_record(
            SUPABASE_URL,
            SUPABASE_SERVICE_ROLE_KEY,
            cls_id,
        )
    except SupabaseConfigurationError:
        md = _merge_notice(
            _render_classrooms_md(classrooms or []),
            "Warning: Configure SUPABASE_URL e SUPABASE_SERVICE_ROLE_KEY para excluir salas.",
        )
        return classrooms, subjects, md
    except SupabaseOperationError as err:
        md = _merge_notice(
            _render_classrooms_md(classrooms or []),
            f"ERROR: Erro ao excluir sala: {err}",
        )
        return classrooms, subjects, md

    classes, subjects_map, notice = _refresh_states(classrooms, subjects)
    md = _merge_notice(_render_classrooms_md(classes), notice or "OK: Sala excluída.")
    return classes, subjects_map, md


def _render_members_md(cls_id, classrooms):
    c = next((x for x in (classrooms or []) if x["id"] == cls_id), None)
    if not c:
        return "Warning: Selecione uma sala."
    members = c.get("members", {}) or {}
    teachers = ", ".join(
        _class_member_labels(c, "teachers", include_usernames=True)
    ) or "—"
    students = ", ".join(
        _class_member_labels(c, "students", include_usernames=True)
    ) or "—"
    return (
        f"### Membros da sala `{c['name']}`\n"
        f"- 👩‍🏫 Professores ({len(members.get('teachers', []))}): {teachers}\n"
        f"- 🎓 Alunos ({len(members.get('students', []))}): {students}"
    )


def add_teacher(cls_id, uname, classrooms, subjects, auth):
    if not cls_id or not uname:
        return classrooms, subjects, "Warning: Informe sala e username."
    uname_norm = _normalize_username(uname)
    if not (_auth_user_id(auth) or _teacher_username(auth) or _is_admin(auth)):
        return classrooms, subjects, "Warning: Faça login."
    classroom = _get_class_by_id(classrooms, cls_id)
    if not classroom:
        return classrooms, subjects, "Warning: Sala não encontrada."

    if not _auth_matches_classroom_teacher(auth, classroom) and not _is_admin(auth):
        return classrooms, subjects, "⛔ Você não é professor desta sala."

    owner_known = classroom.get("owner_id") or classroom.get("owner_login")
    if owner_known and not _auth_matches_classroom_owner(auth, classroom) and not _is_admin(auth):
        return classrooms, subjects, "⛔ Apenas o professor responsável por esta sala pode adicionar outros professores."

    try:
        from services.supabase_client import fetch_user_record

        record = fetch_user_record(
            SUPABASE_URL,
            SUPABASE_SERVICE_ROLE_KEY,
            SUPABASE_USERS_TABLE,
            uname_norm,
        )
    except SupabaseConfigurationError:
        return classrooms, subjects, "Warning: Configure SUPABASE_URL e SUPABASE_SERVICE_ROLE_KEY para gerenciar professores."
    except SupabaseOperationError as err:
        return classrooms, subjects, f"ERROR: Erro ao buscar usuário: {err}"

    if not record or not record.id:
        return classrooms, subjects, "Warning: Usuário não encontrado."

    role_label = None
    if not classroom.get("owner_id") and _auth_matches_classroom_owner(auth, classroom):
        role_label = "owner"

    try:
        upsert_classroom_teacher(
            SUPABASE_URL,
            SUPABASE_SERVICE_ROLE_KEY,
            classroom_id=cls_id,
            teacher_id=record.id,
            role_label=role_label,
        )
    except SupabaseConfigurationError:
        return classrooms, subjects, "Warning: Configure SUPABASE_URL e SUPABASE_SERVICE_ROLE_KEY para gerenciar professores."
    except SupabaseOperationError as err:
        return classrooms, subjects, f"ERROR: Erro ao adicionar professor: {err}"

    classes, subjects_map, notice = _refresh_states(classrooms, subjects)
    md = _merge_notice(_render_members_md(cls_id, classes), notice or "OK: Professor adicionado.")
    return classes, subjects_map, md


def add_student(cls_id, uname, classrooms, subjects):
    if not cls_id or not uname:
        return classrooms, subjects, "Warning: Informe sala e username."
    classroom = _get_class_by_id(classrooms, cls_id)
    if not classroom:
        return classrooms, subjects, "Warning: Sala não encontrada."

    uname_norm = _normalize_username(uname)
    try:
        from services.supabase_client import fetch_user_record

        record = fetch_user_record(
            SUPABASE_URL,
            SUPABASE_SERVICE_ROLE_KEY,
            SUPABASE_USERS_TABLE,
            uname_norm,
        )
    except SupabaseConfigurationError:
        return classrooms, subjects, "Warning: Configure SUPABASE_URL e SUPABASE_SERVICE_ROLE_KEY para gerenciar alunos."
    except SupabaseOperationError as err:
        return classrooms, subjects, f"ERROR: Erro ao buscar usuário: {err}"

    if not record or not record.id:
        return classrooms, subjects, "Warning: Usuário não encontrado."

    try:
        upsert_classroom_student(
            SUPABASE_URL,
            SUPABASE_SERVICE_ROLE_KEY,
            classroom_id=cls_id,
            student_id=record.id,
        )
    except SupabaseConfigurationError:
        return classrooms, subjects, "Warning: Configure SUPABASE_URL e SUPABASE_SERVICE_ROLE_KEY para gerenciar alunos."
    except SupabaseOperationError as err:
        return classrooms, subjects, f"ERROR: Erro ao adicionar aluno: {err}"

    classes, subjects_map, notice = _refresh_states(classrooms, subjects)
    md = _merge_notice(_render_members_md(cls_id, classes), notice or "OK: Aluno adicionado.")
    return classes, subjects_map, md


def remove_member(cls_id, uname, classrooms, subjects):
    if not cls_id or not uname:
        return classrooms, subjects, "Warning: Informe sala e username."
    uname_norm = _normalize_username(uname)
    try:
        from services.supabase_client import fetch_user_record

        record = fetch_user_record(
            SUPABASE_URL,
            SUPABASE_SERVICE_ROLE_KEY,
            SUPABASE_USERS_TABLE,
            uname_norm,
        )
    except SupabaseConfigurationError:
        return classrooms, subjects, "Warning: Configure SUPABASE_URL e SUPABASE_SERVICE_ROLE_KEY para gerenciar integrantes."
    except SupabaseOperationError as err:
        return classrooms, subjects, f"ERROR: Erro ao buscar usuário: {err}"

    if not record or not record.id:
        return classrooms, subjects, "Warning: Usuário não encontrado."

    status_messages: List[str] = []
    try:
        remove_classroom_teacher(
            SUPABASE_URL,
            SUPABASE_SERVICE_ROLE_KEY,
            classroom_id=cls_id,
            teacher_id=record.id,
        )
    except SupabaseConfigurationError:
        return classrooms, subjects, "Warning: Configure SUPABASE_URL e SUPABASE_SERVICE_ROLE_KEY para gerenciar integrantes."
    except SupabaseOperationError as err:
        status_messages.append(f"Professor: {err}")

    try:
        remove_classroom_student(
            SUPABASE_URL,
            SUPABASE_SERVICE_ROLE_KEY,
            classroom_id=cls_id,
            student_id=record.id,
        )
    except SupabaseConfigurationError:
        return classrooms, subjects, "Warning: Configure SUPABASE_URL e SUPABASE_SERVICE_ROLE_KEY para gerenciar integrantes."
    except SupabaseOperationError as err:
        status_messages.append(f"Aluno: {err}")

    classes, subjects_map, notice = _refresh_states(classrooms, subjects)
    base_md = _render_members_md(cls_id, classes)
    message = "Warning: " + "; ".join(status_messages) if status_messages else "OK: Usuário removido."
    result = _merge_notice(base_md, message)
    result = _merge_notice(result, notice)
    return classes, subjects_map, result


def _admin_subjects_ui(classrooms, subjects_by_class, selected_id, notice=""):
    chk = gr.update(choices=[], value=[])
    md = _render_subjects_md(subjects_by_class, selected_id, classrooms or [])
    if selected_id:
        lst = list(subjects_by_class.get(selected_id, []))
        names = [s.get("name") for s in lst]
        active = [s.get("name") for s in lst if s.get("active")]
        chk = gr.update(choices=names, value=active)
    md = _merge_notice(md, notice)
    return chk, md


def admin_refresh_subjects(classrooms, subjects_by_class, selected_id):
    return _admin_subjects_ui(classrooms, subjects_by_class, selected_id)


def admin_add_subject(cls_id, subj, subjects_by_class, classrooms, auth):
    if not cls_id:
        chk, md = _admin_subjects_ui(
            classrooms,
            subjects_by_class,
            None,
            "Info: Selecione uma sala para adicionar subtemas.",
        )
        return classrooms, subjects_by_class, chk, md

    subj_name = (subj or "").strip()
    if not subj_name:
        chk, md = _admin_subjects_ui(
            classrooms,
            subjects_by_class,
            cls_id,
            "Warning: Informe o nome do subtema.",
        )
        return classrooms, subjects_by_class, chk, md

    existing = list(subjects_by_class.get(cls_id, []))
    if any(s.get("name", "").lower() == subj_name.lower() for s in existing):
        chk, md = _admin_subjects_ui(
            classrooms,
            subjects_by_class,
            cls_id,
            "Warning: Esse subtema já existe.",
        )
        return classrooms, subjects_by_class, chk, md

    creator_id = _auth_user_id(auth) or ""
    try:
        create_subject_record(
            SUPABASE_URL,
            SUPABASE_SERVICE_ROLE_KEY,
            classroom_id=cls_id,
            name=subj_name,
            created_by=creator_id,
        )
    except SupabaseConfigurationError:
        chk, md = _admin_subjects_ui(
            classrooms,
            subjects_by_class,
            cls_id,
            "Warning: Configure SUPABASE_URL e SUPABASE_SERVICE_ROLE_KEY para gerenciar subtemas.",
        )
        return classrooms, subjects_by_class, chk, md
    except SupabaseOperationError as err:
        chk, md = _admin_subjects_ui(
            classrooms,
            subjects_by_class,
            cls_id,
            f"ERROR: Erro ao adicionar subtema: {err}",
        )
        return classrooms, subjects_by_class, chk, md

    classes, subjects_map, notice = _refresh_states(classrooms, subjects_by_class)
    chk, md = _admin_subjects_ui(classes, subjects_map, cls_id, notice or "OK: Subtema adicionado.")
    return classes, subjects_map, chk, md


def admin_apply_active(cls_id, actives, subjects_by_class, classrooms):
    if not cls_id:
        chk, md = _admin_subjects_ui(
            classrooms,
            subjects_by_class,
            None,
            "Warning: Selecione uma sala.",
        )
        return classrooms, subjects_by_class, chk, md

    lst = list(subjects_by_class.get(cls_id, []))
    names = set(actives or [])
    try:
        for entry in lst:
            subject_id = entry.get("id")
            if not subject_id:
                continue
            update_subject_active(
                SUPABASE_URL,
                SUPABASE_SERVICE_ROLE_KEY,
                subject_id=subject_id,
                is_active=entry.get("name") in names,
            )
    except SupabaseConfigurationError:
        chk, md = _admin_subjects_ui(
            classrooms,
            subjects_by_class,
            cls_id,
            "Warning: Configure SUPABASE_URL e SUPABASE_SERVICE_ROLE_KEY para gerenciar subtemas.",
        )
        return classrooms, subjects_by_class, chk, md
    except SupabaseOperationError as err:
        chk, md = _admin_subjects_ui(
            classrooms,
            subjects_by_class,
            cls_id,
            f"ERROR: Erro ao atualizar subtemas: {err}",
        )
        return classrooms, subjects_by_class, chk, md

    classes, subjects_map, notice = _refresh_states(classrooms, subjects_by_class)
    chk, md = _admin_subjects_ui(classes, subjects_map, cls_id, notice or "OK: Subtemas atualizados.")
    return classes, subjects_map, chk, md


def eval_refresh_dropdown(chats_map):
    ids = []
    for cid, chat in (chats_map or {}).items():
        if any(m for m in chat["messages"] if m["role"] == "user"):
            ids.append((chat.get("title") or cid, cid))
    default_val = ids[0][1] if ids else None
    return gr.update(choices=ids, value=default_val)


def eval_load(chat_id, chats_map=None):
    print(f"[EVAL] eval_load: chat_id={chat_id!r} has_map={bool(chats_map)}")
    if not chat_id:
        return "Info: Selecione um chat para visualizar/avaliar."
    chat = (chats_map or {}).get(chat_id)
    return _render_eval_md(chat)


def eval_save(chat_id, score, rubric, feedback, chats_map):
    if not chat_id or chat_id not in (chats_map or {}):
        return chats_map, "Warning: Selecione um chat válido."
    cm = chats_map[chat_id]
    cm["score"] = int(score) if score is not None else None
    cm["rubric"] = (rubric or "").strip() or None
    cm["feedback"] = (feedback or "").strip() or None
    return chats_map, "OK: Avaliação salva."


def refresh_progress(chats_map, mine_only, auth):
    from app.pages.student import _render_progress_md  # delayed import to avoid cycle

    user = (auth or {}).get("username")
    return _render_progress_md(chats_map, user_filter=user if mine_only else None)


def _go_admin(page):
    vis = {
        "home": (True, False, False, False, False, False),
        "classrooms": (False, True, False, False, False, False),
        "history": (False, False, True, False, False, False),
        "evaluate": (False, False, False, True, False, False),
        "progress": (False, False, False, False, True, False),
        "admin": (False, False, False, False, False, True),
    }.get(page, (True, False, False, False, False, False))
    (homeV, clsV, histV, evalV, progV, admV) = vis
    return (
        {"page": page},
        gr.update(visible=homeV),
        gr.update(visible=clsV),
        gr.update(visible=histV),
        gr.update(visible=evalV),
        gr.update(visible=progV),
        gr.update(visible=admV),
    )


def build_admin_views(
    *,
    blocks: gr.Blocks,
    auth_state: gr.State,
    classrooms_state: gr.State,
    subjects_state: gr.State,
    chats_state: gr.State,
    admin_nav_state: gr.State,
    studio_container: gr.Column,
) -> AdminViews:
    admin_history_state = gr.State([])
    admin_history_selected = gr.State(None)
    admin_history_transcript = gr.State("")
    admin_download_path = gr.State(None)
    admin_classroom_chats_state = gr.State([])

    with gr.Column(visible=False) as viewHomeAdmin:
        adminGreet = gr.Markdown("## 🧭 Home do Admin")
        with gr.Row():
            navClassrooms = gr.Button("🏫 Salas")
            navHistory = gr.Button("🗂️ Histórico")
            navEvaluate = gr.Button("📝 Avaliação")
            navProgress = gr.Button("📊 Progresso")
            navAdmin = gr.Button("🛠️ Administração")
            btnLogoutAdmin = gr.Button("Sair")
        gr.Markdown("---\n#### Áreas disponíveis dentro da Home do Admin\n")
        with gr.Row():
            with gr.Column():
                gr.Markdown("### 🎓 Área do Aluno (demonstração)")
                btnAdminAsStudent = gr.Button("⚙️ Abrir Personalização do Chat (Aluno)")
            with gr.Column():
                gr.Markdown("### 👩‍🏫 Área do Professor (demonstração)")
                btnAdminListStudents = gr.Button("👥 Ver alunos cadastrados")

    with gr.Column(visible=False) as viewAdminPg:
        gr.Markdown("## 🛠️ Administração")
        gr.Markdown(
            "Listagem e operações em lote de chats por sala. Selecione a sala desejada, marque os chats e utilize as ações abaixo."
        )
        with gr.Row():
            adClassroomDropdown = gr.Dropdown(label="Sala", choices=[], value=None, allow_custom_value=True)
            adReloadChats = gr.Button("🔄 Carregar chats")
        adClassroomInfo = gr.Markdown("Info: escolha uma sala e clique em carregar.")
        adChatListing = gr.Markdown()
        adChatSelector = gr.CheckboxGroup(label="Chats disponíveis", choices=[], value=[])
        adChatNotice = gr.Markdown()
        adLoadingIndicator = gr.HTML("", visible=False)
        with gr.Row():
            btnZipPdfs = gr.Button("📦 Preparar ZIP de PDFs", variant="secondary")
            adZipDownload = gr.DownloadButton("⬇️ Baixar ZIP", visible=False, file_name="chats.zip")
        with gr.Accordion("Gerar resposta via Vertex", open=False):
            adVertexInstructions = gr.Textbox(
                label="Instruções para o Vertex",
                placeholder="Explique como o modelo deve usar os chats, avaliando objetivos, interesses ou sugerindo planos.",
                lines=4,
            )
            with gr.Row():
                adVertexModel = gr.Textbox(
                    label="Modelo do Vertex",
                    value=(VERTEX_CFG or {}).get("model") if VERTEX_CFG else "",
                    placeholder="Ex.: gemini-2.0-pro",
                )
                adVertexTemperature = gr.Slider(label="Temperature", minimum=0.0, maximum=1.0, value=0.4, step=0.05)
            with gr.Row():
                adVertexTopP = gr.Slider(label="Top P", minimum=0.0, maximum=1.0, value=0.95, step=0.01)
                adVertexTopK = gr.Slider(label="Top K", minimum=1, maximum=100, value=40, step=1)
                adVertexMaxTokens = gr.Slider(label="Máx. tokens de saída", minimum=256, maximum=8192, value=2048, step=64)
            with gr.Row():
                btnVertexRun = gr.Button("✨ Enviar ao Vertex", variant="primary")
                adVertexDownload = gr.DownloadButton(
                    "⬇️ Baixar resposta", visible=False, file_name="vertex_resposta.pdf"
                )
            adVertexPreview = gr.Markdown("", visible=False, elem_classes=["history-box"])
        adminPgBack = gr.Button("← Voltar à Home do Admin")

    with gr.Column(visible=False) as viewClassrooms:
        gr.Markdown("## 🏫 Gerenciar Salas")
        with gr.Group():
            with gr.Row():
                clsName = gr.Textbox(label="Nome da sala", placeholder="Ex.: Algoritmos e Estruturas de Dados")
                clsTheme = gr.Textbox(label="Tema (exibição)", placeholder="Ex.: Algoritmos e ED")
            clsDesc = gr.Textbox(label="Descrição (opcional)")
            with gr.Row():
                clsLocked = gr.Checkbox(value=True, label="Tema travado")
                btnAddClass = gr.Button("➕ Criar sala", variant="primary")
        with gr.Accordion("Editar/Arquivar/Excluir", open=False):
            with gr.Row():
                clsSelect = gr.Dropdown(
                    choices=[], label="Selecione a sala", value=None, allow_custom_value=True
                )
                btnRefreshCls = gr.Button("Recarregar Dados")
            with gr.Row():
                eName = gr.Textbox(label="Nome")
                eTheme = gr.Textbox(label="Tema")
            eDesc = gr.Textbox(label="Descrição")
            with gr.Row():
                eLocked = gr.Checkbox(value=True, label="Tema travado")
                eArchived = gr.Checkbox(value=False, label="Arquivada")
            with gr.Row():
                btnSaveCls = gr.Button("💾 Salvar alterações", variant="primary")
                btnDeleteCls = gr.Button("🗑️ Excluir sala", variant="stop")
        with gr.Accordion("Membros (Professores/Alunos)", open=False):
            with gr.Row():
                membClass = gr.Dropdown(
                    choices=[], label="Sala", value=None, allow_custom_value=True
                )
            with gr.Row():
                addTeacher = gr.Textbox(label="Adicionar professor (username)")
                btnAddTeacher = gr.Button("👩‍🏫 Adicionar")
            with gr.Row():
                addStudent = gr.Textbox(label="Adicionar aluno (username)")
                btnAddStudent = gr.Button("🎓 Adicionar")
            with gr.Row():
                rmUser = gr.Textbox(label="Remover usuário (username)")
                btnRmUser = gr.Button("🗑️ Remover")
            membersMd = gr.Markdown("")
        with gr.Accordion("Subtemas da sala", open=False):
            with gr.Row():
                clsSubjName = gr.Textbox(label="Novo subtema", placeholder="Ex.: Ponteiros")
                btnClsAddSubj = gr.Button("➕ Adicionar subtema")
            with gr.Row():
                clsActiveList = gr.CheckboxGroup(choices=[], label="Ativar/desativar subtemas", value=[])
                btnClsApplyActive = gr.Button("OK: Aplicar ativações")
            clsSubjectsMd = gr.Markdown("")
        classroomsMd = gr.Markdown("")
        with gr.Row():
            clsBackAdminHome = gr.Button("← Voltar à Home do Admin")

    with gr.Column(visible=False) as viewHistory:
        gr.Markdown("## 🗂️ Histórico de Chats")
        with gr.Row():
            adHistoryClass = gr.Dropdown(
                choices=[], label="Sala", value="", allow_custom_value=True
            )
            adHistoryRefresh = gr.Button("Recarregar Dados Atualizar histórico")
        adHistoryInfo = gr.Markdown("Selecione uma sala para filtrar ou mantenha em branco para ver todas.")
        adHistoryTable = gr.Dataframe(
            headers=[
                "Aluno",
                "Sala",
                "Assuntos",
                "Resumo",
                "Nota",
                "Iniciado em",
            ],
            datatype=["str"] * 6,
            interactive=False,
            wrap=True,
             
        )
        with gr.Row():
            adHistoryChat = gr.Dropdown(
                choices=[], label="Chat registrado", value=None, allow_custom_value=True
            )
            adHistoryLoad = gr.Button("📄 Ver detalhes")
        adHistoryMetadata = gr.Markdown(
            "Info: Selecione um chat para visualizar os detalhes.",
            elem_classes=["history-box"],
        )
        gr.Markdown("#### Resumo da IA")
        adHistorySummary = gr.Markdown(
            "Info: Selecione um chat para visualizar o resumo.",
            elem_classes=["history-box"],
        )
        gr.Markdown("#### Prévia do PDF")
        adHistoryPreview = gr.Markdown(
            "Info: Carregue um chat para visualizar a prévia.",
            elem_classes=["history-box"],
        )
        with gr.Row():
            adHistoryDownload = gr.DownloadButton(" Baixar PDF", visible=False, variant="secondary")
            adHistoryGenerateEval = gr.Button("🤖 Gerar avaliação automática", variant="secondary")
        adHistoryEvaluation = gr.Textbox(
            label="Avaliação automática (Vertex)", lines=6, interactive=False, value=""
        )
        adManualRating = gr.Slider(0, 100, value=0, step=1, label="Avaliação manual (0-100)")
        gr.Markdown("A nota selecionada será registrada junto ao comentário enviado.")
        adHistoryComments = gr.Markdown(
            "Info: Nenhum comentário registrado ainda.",
            elem_classes=["history-box"],
        )
        adCommentInput = gr.Textbox(
            label="Novo comentário",
            placeholder="Compartilhe observações com os professores",
        )
        with gr.Row():
            adAddComment = gr.Button("💬 Registrar comentário")
            histBack = gr.Button("← Voltar à Home do Admin")
        adHistoryNotice = gr.Markdown("")

    with gr.Column(visible=False) as viewEvaluate:
        gr.Markdown("## 📝 Avaliar Chats")
        with gr.Row():
            evalChatId = gr.Dropdown(
                choices=[], label="Chat para avaliar", value=None, allow_custom_value=True
            )
            btnEvalRefresh = gr.Button("Recarregar Dados")
        evalCurrent = gr.Markdown("")
        with gr.Row():
            evalScore = gr.Slider(0, 10, value=8, step=1, label="Nota")
            evalRubric = gr.Textbox(label="Rubrica (curta)", placeholder="Ex.: Clareza, Correção, Raciocínio")
        evalFeedback = gr.Textbox(label="Feedback para o aluno", placeholder="Escreva um comentário objetivo")
        btnSaveEval = gr.Button("💾 Salvar avaliação", variant="primary")
        with gr.Row():
            evalBack = gr.Button("← Voltar à Home do Admin")

    with gr.Column(visible=False) as viewProgress:
        gr.Markdown("## 📊 Progresso e Relatórios")
        with gr.Row():
            progMineOnly = gr.Checkbox(value=False, label="Restringir aos meus chats")
            btnProgRefresh = gr.Button("Recarregar Dados Atualizar")
        progressMd = gr.Markdown("")
        with gr.Row():
            progBack = gr.Button("← Voltar à Home do Admin")

    navClassrooms.click(
        lambda: _go_admin("classrooms"),
        outputs=[admin_nav_state, viewHomeAdmin, viewClassrooms, viewHistory, viewEvaluate, viewProgress, viewAdminPg],
    )
    navHistory.click(
        lambda: _go_admin("history"),
        outputs=[admin_nav_state, viewHomeAdmin, viewClassrooms, viewHistory, viewEvaluate, viewProgress, viewAdminPg],
    ).then(
        admin_history_dropdown,
        inputs=[classrooms_state, adHistoryClass],
        outputs=adHistoryClass,
    )
    navEvaluate.click(
        lambda: _go_admin("evaluate"),
        outputs=[admin_nav_state, viewHomeAdmin, viewClassrooms, viewHistory, viewEvaluate, viewProgress, viewAdminPg],
    )
    navProgress.click(
        lambda: _go_admin("progress"),
        outputs=[admin_nav_state, viewHomeAdmin, viewClassrooms, viewHistory, viewEvaluate, viewProgress, viewAdminPg],
    )
    navAdmin.click(
        lambda: _go_admin("admin"),
        outputs=[admin_nav_state, viewHomeAdmin, viewClassrooms, viewHistory, viewEvaluate, viewProgress, viewAdminPg],
    )

    navAdmin.click(
        admin_load_classroom_chats,
        inputs=[auth_state, classrooms_state, adClassroomDropdown, adChatSelector],
        outputs=[adClassroomDropdown, adChatListing, admin_classroom_chats_state, adChatSelector, adClassroomInfo],
    )

    adClassroomDropdown.change(
        admin_load_classroom_chats,
        inputs=[auth_state, classrooms_state, adClassroomDropdown, adChatSelector],
        outputs=[adClassroomDropdown, adChatListing, admin_classroom_chats_state, adChatSelector, adClassroomInfo],
    )

    adminPgBack.click(
        lambda: _go_admin("home"),
        outputs=[admin_nav_state, viewHomeAdmin, viewClassrooms, viewHistory, viewEvaluate, viewProgress, viewAdminPg],
    )

    btnAddClass.click(
        add_classroom,
        inputs=[
            clsName,
            clsTheme,
            clsDesc,
            clsLocked,
            classrooms_state,
            subjects_state,
            auth_state,
            clsSelect,
            membClass,
        ],
        outputs=[classrooms_state, subjects_state, classroomsMd, clsSelect, membClass],
    ).then(
        admin_refresh_subjects,
        inputs=[classrooms_state, subjects_state, clsSelect],
        outputs=[clsActiveList, clsSubjectsMd],
    ).then(
        admin_history_dropdown,
        inputs=[classrooms_state, adHistoryClass],
        outputs=adHistoryClass,
    )

    btnRefreshCls.click(
        refresh_classrooms,
        inputs=[classrooms_state, subjects_state, clsSelect, membClass],
        outputs=[classrooms_state, subjects_state, classroomsMd, clsSelect, membClass],
    ).then(
        admin_refresh_subjects,
        inputs=[classrooms_state, subjects_state, clsSelect],
        outputs=[clsActiveList, clsSubjectsMd],
    ).then(
        admin_history_dropdown,
        inputs=[classrooms_state, adHistoryClass],
        outputs=adHistoryClass,
    )

    clsSelect.change(
        load_cls_for_edit,
        inputs=[clsSelect, classrooms_state],
        outputs=[eName, eTheme, eDesc, eLocked, eArchived, classroomsMd],
    ).then(
        admin_refresh_subjects,
        inputs=[classrooms_state, subjects_state, clsSelect],
        outputs=[clsActiveList, clsSubjectsMd],
    )

    btnSaveCls.click(
        save_cls,
        inputs=[clsSelect, eName, eTheme, eDesc, eLocked, eArchived, classrooms_state, subjects_state],
        outputs=[classrooms_state, subjects_state, classroomsMd],
    ).then(
        admin_refresh_subjects,
        inputs=[classrooms_state, subjects_state, clsSelect],
        outputs=[clsActiveList, clsSubjectsMd],
    ).then(
        admin_history_dropdown,
        inputs=[classrooms_state, adHistoryClass],
        outputs=adHistoryClass,
    )

    btnDeleteCls.click(
        delete_cls,
        inputs=[clsSelect, classrooms_state, subjects_state],
        outputs=[classrooms_state, subjects_state, classroomsMd],
    ).then(
        admin_refresh_subjects,
        inputs=[classrooms_state, subjects_state, clsSelect],
        outputs=[clsActiveList, clsSubjectsMd],
    ).then(
        admin_history_dropdown,
        inputs=[classrooms_state, adHistoryClass],
        outputs=adHistoryClass,
    )

    membClass.change(
        lambda cid, cls: _render_members_md(cid, cls),
        inputs=[membClass, classrooms_state],
        outputs=[membersMd],
    )

    btnAddTeacher.click(
        add_teacher,
        inputs=[membClass, addTeacher, classrooms_state, subjects_state, auth_state],
        outputs=[classrooms_state, subjects_state, membersMd],
    )

    btnAddStudent.click(
        add_student,
        inputs=[membClass, addStudent, classrooms_state, subjects_state],
        outputs=[classrooms_state, subjects_state, membersMd],
    )

    btnRmUser.click(
        remove_member,
        inputs=[membClass, rmUser, classrooms_state, subjects_state],
        outputs=[classrooms_state, subjects_state, membersMd],
    )

    btnClsAddSubj.click(
        admin_add_subject,
        inputs=[clsSelect, clsSubjName, subjects_state, classrooms_state, auth_state],
        outputs=[classrooms_state, subjects_state, clsActiveList, clsSubjectsMd],
    )

    btnClsApplyActive.click(
        admin_apply_active,
        inputs=[clsSelect, clsActiveList, subjects_state, classrooms_state],
        outputs=[classrooms_state, subjects_state, clsSubjectsMd],
    )

    clsBackAdminHome.click(
        lambda: _go_admin("home"),
        outputs=[admin_nav_state, viewHomeAdmin, viewClassrooms, viewHistory, viewEvaluate, viewProgress, viewAdminPg],
    )

    adHistoryRefresh.click(
        admin_history_refresh,
        inputs=[auth_state, adHistoryClass],
        outputs=[
            adHistoryTable,
            admin_history_state,
            adHistoryChat,
            adHistoryInfo,
            admin_history_selected,
        ],
    ).then(
        admin_history_load_chat,
        inputs=[admin_history_selected, admin_history_state, admin_download_path],
        outputs=[
            admin_history_selected,
            adHistoryMetadata,
            adHistorySummary,
            adHistoryPreview,
            adHistoryEvaluation,
            adManualRating,
            adHistoryComments,
            admin_history_transcript,
            admin_download_path,
            adHistoryDownload,
            adCommentInput,
        ],
    )

    adHistoryClass.change(
        admin_history_refresh,
        inputs=[auth_state, adHistoryClass],
        outputs=[
            adHistoryTable,
            admin_history_state,
            adHistoryChat,
            adHistoryInfo,
            admin_history_selected,
        ],
    ).then(
        admin_history_load_chat,
        inputs=[admin_history_selected, admin_history_state, admin_download_path],
        outputs=[
            admin_history_selected,
            adHistoryMetadata,
            adHistorySummary,
            adHistoryPreview,
            adHistoryEvaluation,
            adManualRating,
            adHistoryComments,
            admin_history_transcript,
            admin_download_path,
            adHistoryDownload,
            adCommentInput,
        ],
    )

    adHistoryChat.change(
        admin_history_load_chat,
        inputs=[adHistoryChat, admin_history_state, admin_download_path],
        outputs=[
            admin_history_selected,
            adHistoryMetadata,
            adHistorySummary,
            adHistoryPreview,
            adHistoryEvaluation,
            adManualRating,
            adHistoryComments,
            admin_history_transcript,
            admin_download_path,
            adHistoryDownload,
            adCommentInput,
        ],
    )

    adHistoryLoad.click(
        admin_history_load_chat,
        inputs=[adHistoryChat, admin_history_state, admin_download_path],
        outputs=[
            admin_history_selected,
            adHistoryMetadata,
            adHistorySummary,
            adHistoryPreview,
            adHistoryEvaluation,
            adManualRating,
            adHistoryComments,
            admin_history_transcript,
            admin_download_path,
            adHistoryDownload,
            adCommentInput,
        ],
    )

    adHistoryGenerateEval.click(
        admin_history_generate_evaluation,
        inputs=[admin_history_selected, admin_history_transcript, admin_history_state],
        outputs=[adHistoryEvaluation, admin_history_state, adHistoryMetadata, adHistoryNotice],
    )

    adAddComment.click(
        admin_history_add_comment,
        inputs=[
            admin_history_selected,
            adManualRating,
            adCommentInput,
            admin_history_state,
            auth_state,
        ],
        outputs=[admin_history_state, adCommentInput, adHistoryComments, adHistoryNotice],
    )

    adHistoryDownload.click(
        admin_history_prepare_download,
        inputs=[admin_download_path],
        outputs=adHistoryDownload,
    )

    adReloadChats.click(
        admin_load_classroom_chats,
        inputs=[auth_state, classrooms_state, adClassroomDropdown, adChatSelector],
        outputs=[adClassroomDropdown, adChatListing, admin_classroom_chats_state, adChatSelector, adClassroomInfo],
    )

    btnZipPdfs.click(
        lambda: gr.update(value=_spinner_html("Gerando ZIP..."), visible=True),
        outputs=[adLoadingIndicator],
    ).then(
        admin_prepare_chat_zip,
        inputs=[adChatSelector, admin_classroom_chats_state, auth_state],
        outputs=[adZipDownload, adChatNotice],
    ).then(
        lambda: gr.update(visible=False),
        outputs=[adLoadingIndicator],
    )

    btnVertexRun.click(
        lambda: gr.update(value=_spinner_html("Enviando ao Vertex..."), visible=True),
        outputs=[adLoadingIndicator],
    ).then(
        admin_vertex_summarize_chats,
        inputs=[
            adChatSelector,
            admin_classroom_chats_state,
            auth_state,
            adVertexInstructions,
            adVertexModel,
            adVertexTemperature,
            adVertexTopP,
            adVertexTopK,
            adVertexMaxTokens,
        ],
        outputs=[adVertexDownload, adChatNotice, adVertexPreview],
    ).then(
        lambda: gr.update(visible=False),
        outputs=[adLoadingIndicator],
    )

    histBack.click(
        lambda: _go_admin("home"),
        outputs=[admin_nav_state, viewHomeAdmin, viewClassrooms, viewHistory, viewEvaluate, viewProgress, viewAdminPg],
    )

    btnEvalRefresh.click(
        eval_refresh_dropdown,
        inputs=[chats_state],
        outputs=[evalChatId],
    )

    evalChatId.change(
        eval_load,
        inputs=[evalChatId, chats_state],
        outputs=[evalCurrent],
    )

    btnSaveEval.click(
        eval_save,
        inputs=[evalChatId, evalScore, evalRubric, evalFeedback, chats_state],
        outputs=[chats_state, evalCurrent],
    )

    evalBack.click(
        lambda: _go_admin("home"),
        outputs=[admin_nav_state, viewHomeAdmin, viewClassrooms, viewHistory, viewEvaluate, viewProgress, viewAdminPg],
    )

    btnProgRefresh.click(
        refresh_progress,
        inputs=[chats_state, progMineOnly, auth_state],
        outputs=[progressMd],
    )

    progBack.click(
        lambda: _go_admin("home"),
        outputs=[admin_nav_state, viewHomeAdmin, viewClassrooms, viewHistory, viewEvaluate, viewProgress, viewAdminPg],
    )

    btnAdminAsStudent.click(
        lambda: (gr.update(visible=False), gr.update(visible=True)),
        inputs=None,
        outputs=[viewHomeAdmin, studio_container],
    )

    return AdminViews(
        home=viewHomeAdmin,
        admin_page=viewAdminPg,
        classrooms=viewClassrooms,
        history=viewHistory,
        evaluate=viewEvaluate,
        progress=viewProgress,
        btn_logout=btnLogoutAdmin,
        btn_admin_as_student=btnAdminAsStudent,
        btn_admin_list_students=btnAdminListStudents,
    )


__all__ = [
    "AdminViews",
    "build_admin_views",
    "_render_classrooms_md",
    "_render_subjects_md",
    "_render_eval_md",
    "_refresh_states",
    "_sync_domain_after_auth",
    "add_classroom",
    "refresh_classrooms",
    "load_cls_for_edit",
    "save_cls",
    "delete_cls",
    "add_teacher",
    "add_student",
    "remove_member",
    "admin_refresh_subjects",
    "admin_add_subject",
    "admin_apply_active",
    "admin_history_dropdown",
    "admin_history_refresh",
    "admin_history_load_chat",
    "admin_history_generate_evaluation",
    "admin_history_add_comment",
    "admin_history_prepare_download",
    "admin_load_classroom_chats",
    "admin_prepare_chat_zip",
    "admin_vertex_summarize_chats",
    "eval_refresh_dropdown",
    "eval_load",
    "eval_save",
    "refresh_progress",
]
