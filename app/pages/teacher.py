"""Teacher-facing utilities and Gradio view construction."""

from __future__ import annotations

import mimetypes
import os
import re
from dataclasses import dataclass
from typing import Any, Dict, Iterable, List, Optional
from uuid import uuid4

import gradio as gr

from services.supabase_client import (
    SupabaseConfigurationError,
    SupabaseOperationError,
    create_classroom_document_record,
    create_classroom_record,
    create_subject_record,
    delete_classroom_document_record,
    delete_file_from_bucket,
    fetch_user_record,
    list_teacher_classroom_chats,
    remove_classroom_student,
    set_classroom_theme_config,
    update_classroom_document_record,
    update_subject_active,
    upload_file_to_bucket,
    upsert_classroom_student,
    upsert_classroom_teacher,
)

from app.config import (
    SUPABASE_CLASS_DOCS_BUCKET,
    SUPABASE_SERVICE_ROLE_KEY,
    SUPABASE_URL,
    SUPABASE_USERS_TABLE,
)
from app.pages.admin import _render_classrooms_md, _render_subjects_md, _refresh_states
from app.pages.history_shared import (
    HISTORY_TABLE_HEADERS,
    _chat_metadata_md,
    _comments_markdown,
    _format_timestamp,
    _subjects_label,
    append_chat_comment,
    generate_auto_evaluation,
    load_chat_entry,
    prepare_download,
    prepare_history_listing,
)
from app.utils import (
    _auth_matches_classroom_owner,
    _auth_matches_classroom_teacher,
    _auth_user_id,
    _class_member_labels,
    _get_class_by_id,
    _is_admin,
    _is_teacher,
    _merge_notice,
    _normalize_username,
    _teacher_username,
    _user_role,
)


@dataclass
class TeacherView:
    container: gr.Column
    notice: gr.Markdown
    select_dropdown: gr.Dropdown
    back_button: gr.Button
    history_class_dropdown: gr.Dropdown


def _resolve_classroom_filter(
    raw_filter: Optional[str],
    auth: Optional[Dict[str, Any]],
    classrooms: Optional[Iterable[Dict[str, Any]]],
    chats: Optional[Iterable[Dict[str, Any]]],
) -> str:
    """Return the classroom ID associated with the raw dropdown value."""

    value = str(raw_filter or "").strip()
    if not value:
        return ""

    def _match_entries(
        entries: Iterable[Dict[str, Any]],
        *,
        id_key: str = "id",
        name_key: str = "name",
    ) -> str:
        # First try to match by the raw identifier returned by the dropdown.
        for entry in entries:
            cid = str((entry or {}).get(id_key) or "").strip()
            if cid and value == cid:
                return cid

        lowered = value.lower()
        for entry in entries:
            data = entry or {}
            cid = str(data.get(id_key) or "").strip()
            name = str(data.get(name_key) or "").strip()
            if cid and name and name.lower() == lowered:
                return cid

        return ""

    teacher_classes = _teacher_classes(auth, classrooms or [])
    resolved = _match_entries(teacher_classes)
    if resolved:
        return resolved

    resolved = _match_entries(chats or [], id_key="classroom_id", name_key="classroom_name")
    if resolved:
        return resolved

    return ""


def teacher_history_refresh(auth, classroom_filter, classrooms=None):
    teacher_id = _auth_user_id(auth)
    if not teacher_id:
        return (
            gr.update(value=[]),
            [],
            gr.update(choices=[], value=None),
            "⚠️ Faça login como professor para visualizar os chats.",
            None,
        )

    try:
        chats = list_teacher_classroom_chats(
            SUPABASE_URL,
            SUPABASE_SERVICE_ROLE_KEY,
            teacher_id=teacher_id,
            users_table=SUPABASE_USERS_TABLE,
        )
    except SupabaseConfigurationError:
        return (
            gr.update(value=[]),
            [],
            gr.update(choices=[], value=None),
            "⚠️ Configure SUPABASE_URL e SUPABASE_SERVICE_ROLE_KEY para acessar o histórico de chats.",
            None,
        )
    except SupabaseOperationError as err:
        return (
            gr.update(value=[]),
            [],
            gr.update(choices=[], value=None),
            f"❌ Erro ao consultar chats: {err}",
            None,
        )

    classroom_filter = _resolve_classroom_filter(
        classroom_filter,
        auth,
        classrooms,
        chats,
    )

    def _filter_chat(chat: Dict[str, Any]) -> bool:
        return str(chat.get("classroom_id")) == classroom_filter

    def _dropdown_label(chat: Dict[str, Any]) -> str:
        student = chat.get("student_name") or chat.get("student_login") or "Aluno"
        started = _format_timestamp(chat.get("started_at"))
        return f"{student} — {started}"

    filter_fn = _filter_chat if classroom_filter else None

    table_update, filtered, dropdown_update, message, default_id = prepare_history_listing(
        chats,
        column_labels=HISTORY_TABLE_HEADERS,
        filter_fn=filter_fn,
        dropdown_label=_dropdown_label,
        empty_message="ℹ️ Nenhum chat para o filtro.",
        found_message="✅ {count} chat(s) encontrados.",
    )

    return table_update, filtered, dropdown_update, message, default_id


def teacher_history_load_chat(
    chat_id,
    history_entries,
    current_download_path,
):
    result = load_chat_entry(chat_id, history_entries, current_download_path)

    if result.notice:
        if result.notice.startswith("❌"):
            gr.Error(result.notice)
        else:
            gr.Warning(result.notice)

    manual_value = 0
    preview_value = result.preview_text or "ℹ️ Carregue um chat para visualizar a prévia."
    summary_value = result.summary_text or "ℹ️ Este chat ainda não possui um resumo gerado."

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


def teacher_history_generate_evaluation(
    chat_id,
    transcript,
    history_entries,
):
    evaluation, entries, metadata, notice = generate_auto_evaluation(
        chat_id, transcript, history_entries
    )
    metadata_update = gr.update(value=metadata) if metadata is not None else gr.update()
    return gr.update(value=evaluation), entries, metadata_update, notice


def teacher_history_add_comment(chat_id, rating, comment_text, history_entries, auth):
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
        author_login=_teacher_username(auth),
        author_name=author_display,
    )

    if comments_md is None:
        return updated, gr.update(value=comment_text), gr.update(), notice

    return updated, gr.update(value=""), gr.update(value=comments_md), notice


def teacher_history_prepare_download(download_path):
    path = prepare_download(download_path)
    if path:
        return path
    gr.Warning("⚠️ Nenhum arquivo disponível para download.")
    return None

def _teacher_classes(auth, classrooms: Iterable[Dict[str, Any]]):
    if _is_admin(auth):
        return list(classrooms or [])
    out = []
    for c in classrooms or []:
        if _auth_matches_classroom_teacher(auth, c):
            out.append(c)
    return out


def _teacher_choices(auth, classrooms):
    my = _teacher_classes(auth, classrooms)
    return [(c["name"], c["id"]) for c in my]


def _teacher_history_dropdown(auth, classrooms, current_value=None):
    choices = _teacher_choices(auth, classrooms)
    valid_ids = [value for _, value in choices]
    value = current_value if current_value in valid_ids else None
    return gr.update(choices=choices, value=value)


def teacher_history_dropdown(auth, classrooms, current_value=None):
    """Public helper to refresh the teacher history classroom filter."""

    return _teacher_history_dropdown(auth, classrooms, current_value)


def _render_teacher_members_md(cls_id, classrooms):
    c = _get_class_by_id(classrooms, cls_id)
    if not c:
        return "⚠️ Selecione uma de suas salas."
    members = c.get("members", {}) or {}
    teacher_ids = list(members.get("teachers", []) or [])
    student_ids = list(members.get("students", []) or [])

    teachers = ", ".join(
        _class_member_labels(c, "teachers", include_usernames=True)
    ) or "—"
    students = ", ".join(
        _class_member_labels(c, "students", include_usernames=True)
    ) or "—"

    return (
        f"### Membros da sala `{c['name']}`\n"
        f"- 👩‍🏫 Professores ({len(teacher_ids)}): {teachers}\n"
        f"- 🎓 Alunos ({len(student_ids)}): {students}"
    )



def _class_documents(classrooms, cls_id):
    classroom = _get_class_by_id(classrooms, cls_id)
    if not classroom:
        return []
    docs = classroom.get("documents") or []
    return list(docs)


def _safe_document_filename(name: str) -> str:
    base = os.path.basename(name or "")
    base = base.replace("\\", "/").split("/")[-1]
    sanitized = re.sub(r"[^A-Za-z0-9._-]+", "_", base)
    sanitized = sanitized.strip("._") or "documento"
    return sanitized


def _format_filesize(value: Any) -> str:
    try:
        size = int(value)
    except (TypeError, ValueError):
        return ""
    if size < 1024:
        return f"{size} B"
    units = ["KB", "MB", "GB", "TB"]
    scaled = float(size)
    for unit in units:
        scaled /= 1024.0
        if scaled < 1024.0:
            return f"{scaled:.1f} {unit}"
    return f"{scaled:.1f} PB"


def _render_documents_md(cls_id, classrooms):
    if not cls_id:
        return "ℹ️ Selecione uma sala para visualizar os materiais."
    docs = _class_documents(classrooms, cls_id)
    if not docs:
        return "ℹ️ Nenhum material enviado para esta sala ainda."

    lines = [f"### Materiais cadastrados ({len(docs)})"]
    for doc in docs:
        name = (doc.get("name") or "Documento").strip() or "Documento"
        size = _format_filesize(doc.get("file_size"))
        author = (
            doc.get("uploaded_by_name")
            or doc.get("uploaded_by_username")
            or doc.get("uploaded_by_login")
            or doc.get("uploaded_by")
        )
        updated_at = doc.get("updated_at") or doc.get("created_at")
        timestamp = _format_timestamp(updated_at) if updated_at else None
        details = [part for part in (size, author, timestamp) if part]
        detail_text = f" — {', '.join(details)}" if details else ""
        lines.append(f"- 📄 **{name}**{detail_text}")
        storage_bucket = doc.get("storage_bucket")
        storage_path = doc.get("storage_path")
        if storage_bucket and storage_path:
            lines.append(f"  - Storage: `{storage_bucket}/{storage_path}`")
        content_type = doc.get("content_type")
        if content_type:
            lines.append(f"  - Tipo: `{content_type}`")
    return "\n".join(lines)


def _documents_dropdown(classrooms, cls_id, current_value=None):
    docs = _class_documents(classrooms, cls_id)
    choices = []
    for doc in docs:
        doc_id = doc.get("id")
        if not doc_id:
            continue
        name = (doc.get("name") or str(doc_id)).strip() or str(doc_id)
        size = _format_filesize(doc.get("file_size"))
        label = f"{name} ({size})" if size else name
        choices.append((label, doc_id))
    valid_ids = [value for _, value in choices]
    value = current_value if current_value in valid_ids else None
    return gr.update(choices=choices, value=value)


def _get_document_by_id(classrooms, cls_id, doc_id):
    if not doc_id:
        return None
    for doc in _class_documents(classrooms, cls_id):
        if str(doc.get("id")) == str(doc_id):
            return doc
    return None



def _subjects_choices_teacher(auth, classrooms, selected_id, subjects_by_class):
    dd = gr.update(choices=_teacher_choices(auth, classrooms), value=selected_id if selected_id else None)
    if not selected_id:
        return dd, gr.update(choices=[], value=[]), "ℹ️ Selecione uma sala para gerenciar subtemas."
    lst = subjects_by_class.get(selected_id, [])
    all_names = [s["name"] for s in lst]
    active = [s["name"] for s in lst if s.get("active")]
    return dd, gr.update(choices=all_names, value=active), _render_subjects_md(subjects_by_class, selected_id, classrooms or [])


def teacher_load_params(cls_id, classrooms):
    c = _get_class_by_id(classrooms, cls_id)
    if not c:
        return (
            gr.update(value=0.7),
            gr.update(value=0.95),
            gr.update(value=40),
            gr.update(value=1024),
            gr.update(value="simples"),
            gr.update(value="detalhadas"),
            gr.update(value=""),
            "⚠️ Sala não encontrada.",
        )
    cfg = c.get("theme_config") or {}
    adv = cfg.get("adv") or {}
    script = cfg.get("script") or {}
    return (
        gr.update(value=float(adv.get("temperature", 0.7))),
        gr.update(value=float(adv.get("top_p", 0.95))),
        gr.update(value=int(adv.get("top_k", 40))),
        gr.update(value=int(adv.get("max_tokens", 1024))),
        gr.update(value=script.get("estilo", "simples")),
        gr.update(value=script.get("detalhamento", "detalhadas")),
        gr.update(value=script.get("extras", "")),
        "✅ Parâmetros carregados.",
    )


def teacher_save_params(
    cls_id,
    temp,
    top_p,
    top_k,
    max_t,
    estilo,
    detalhamento,
    extras,
    classrooms,
    subjects,
    auth,
):
    c = _get_class_by_id(classrooms, cls_id)
    if not c:
        return classrooms, subjects, "⚠️ Sala não encontrada."
    if not _auth_matches_classroom_teacher(auth, c) and not _is_admin(auth):
        return classrooms, subjects, "⛔ Você não é professor desta sala."

    cfg = {
        "adv": {
            "temperature": float(temp),
            "top_p": float(top_p),
            "top_k": int(top_k),
            "max_tokens": int(max_t),
        },
        "script": {
            "estilo": estilo or "simples",
            "detalhamento": detalhamento or "detalhadas",
            "extras": (extras or "").strip(),
        },
    }

    try:
        set_classroom_theme_config(
            SUPABASE_URL,
            SUPABASE_SERVICE_ROLE_KEY,
            classroom_id=cls_id,
            theme_config=cfg,
        )
    except SupabaseConfigurationError:
        return (
            classrooms,
            subjects,
            "⚠️ Configure SUPABASE_URL e SUPABASE_SERVICE_ROLE_KEY para salvar parâmetros da sala.",
        )
    except SupabaseOperationError as err:
        return classrooms, subjects, f"❌ Erro ao salvar parâmetros: {err}"

    classes, subjects_map, notice = _refresh_states(classrooms, subjects)
    msg = notice or "✅ Parâmetros salvos para a sala."
    return classes, subjects_map, msg


def _teacher_classrooms_outputs(auth, classrooms, notice=""):
    relevant = _teacher_classes(auth, classrooms or [])
    md = _render_classrooms_md(relevant)
    md = _merge_notice(md, notice)
    dd = gr.update(choices=_teacher_choices(auth, classrooms or []))
    return md, dd


def teacher_add_teacher(cls_id, uname, classrooms, subjects, auth):
    uname_norm = _normalize_username(uname)
    if not cls_id or not uname_norm:
        return classrooms, subjects, "⚠️ Informe sala e username."
    if not (_auth_user_id(auth) or _teacher_username(auth) or _is_admin(auth)):
        return classrooms, subjects, "⚠️ Faça login."

    classroom = _get_class_by_id(classrooms, cls_id)
    if not classroom:
        return classrooms, subjects, "⚠️ Sala não encontrada."

    if not _auth_matches_classroom_teacher(auth, classroom) and not _is_admin(auth):
        return classrooms, subjects, "⛔ Você não é professor desta sala."

    owner_known = classroom.get("owner_id") or classroom.get("owner_login")
    if owner_known and not _auth_matches_classroom_owner(auth, classroom) and not _is_admin(auth):
        return (
            classrooms,
            subjects,
            "⛔ Apenas o professor responsável por esta sala pode adicionar outros professores.",
        )

    try:
        record = fetch_user_record(
            SUPABASE_URL,
            SUPABASE_SERVICE_ROLE_KEY,
            SUPABASE_USERS_TABLE,
            uname_norm,
        )
    except SupabaseConfigurationError:
        return (
            classrooms,
            subjects,
            "⚠️ Configure SUPABASE_URL e SUPABASE_SERVICE_ROLE_KEY para gerenciar professores.",
        )
    except SupabaseOperationError as err:
        return classrooms, subjects, f"❌ Erro ao buscar usuário: {err}"

    if not record or not record.id:
        return classrooms, subjects, "⚠️ Usuário não encontrado."

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
        return (
            classrooms,
            subjects,
            "⚠️ Configure SUPABASE_URL e SUPABASE_SERVICE_ROLE_KEY para gerenciar professores.",
        )
    except SupabaseOperationError as err:
        return classrooms, subjects, f"❌ Erro ao adicionar professor: {err}"

    classes, subjects_map, notice = _refresh_states(classrooms, subjects)
    md = _merge_notice(_render_teacher_members_md(cls_id, classes), notice or "✅ Professor adicionado.")
    return classes, subjects_map, md


def teacher_add_classroom(name, theme, desc, locked, classrooms, subjects, auth):
    name = (name or "").strip()
    theme = (theme or "").strip() or name
    me_id = _auth_user_id(auth)
    role = _user_role(auth)

    if role not in ("professor", "admin"):
        md, dd = _teacher_classrooms_outputs(auth, classrooms, "⛔ Apenas professores ou admins podem criar salas.")
        return classrooms, subjects, md, dd, dd, "⛔ Apenas professores ou admins podem criar salas."
    if not me_id and not _is_admin(auth):
        md, dd = _teacher_classrooms_outputs(auth, classrooms, "⚠️ Faça login.")
        return classrooms, subjects, md, dd, dd, "⚠️ Faça login."
    if not name:
        md, dd = _teacher_classrooms_outputs(auth, classrooms, "⚠️ Informe um nome para a sala.")
        return classrooms, subjects, md, dd, dd, "⚠️ Informe um nome para a sala."

    creator_id = _auth_user_id(auth)
    if not creator_id and not _is_admin(auth):
        md, dd = _teacher_classrooms_outputs(auth, classrooms, "⚠️ Não foi possível identificar o usuário logado.")
        return classrooms, subjects, md, dd, dd, "⚠️ Não foi possível identificar o usuário logado."

    description = (desc or "").strip() or ""
    try:
        created = create_classroom_record(
            SUPABASE_URL,
            SUPABASE_SERVICE_ROLE_KEY,
            name=name,
            description=description,
            theme_name=theme,
            theme_locked=bool(locked),
            created_by=creator_id or "",
        )
    except SupabaseConfigurationError:
        md, dd = _teacher_classrooms_outputs(
            auth,
            classrooms,
            "⚠️ Configure SUPABASE_URL e SUPABASE_SERVICE_ROLE_KEY para cadastrar salas.",
        )
        return (
            classrooms,
            subjects,
            md,
            dd,
            dd,
            "⚠️ Configure SUPABASE_URL e SUPABASE_SERVICE_ROLE_KEY para cadastrar salas.",
        )
    except SupabaseOperationError as err:
        md, dd = _teacher_classrooms_outputs(auth, classrooms, f"❌ Erro ao criar sala: {err}")
        return classrooms, subjects, md, dd, dd, f"❌ Erro ao criar sala: {err}"

    classroom_id = (created or {}).get("id")
    if _is_teacher(auth) and me_id and creator_id and classroom_id:
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
            print(f"[SUPABASE] Falha ao definir professor responsável: {err}")

    classes, subjects_map, notice = _refresh_states(classrooms, subjects)
    md, dd = _teacher_classrooms_outputs(auth, classes, notice)
    message = notice or "✅ Sala criada."
    return classes, subjects_map, md, dd, dd, message


def teacher_refresh(auth, classrooms, subjects):
    classes, subjects_map, notice = _refresh_states(classrooms, subjects)
    md, dd = _teacher_classrooms_outputs(auth, classes, notice)
    return classes, subjects_map, md, dd


def teacher_add_student(cls_id, uname, classrooms, subjects, auth):
    uname_norm = _normalize_username(uname)
    if not cls_id or not uname_norm:
        return classrooms, subjects, "⚠️ Informe sala e username."
    if not (_auth_user_id(auth) or _teacher_username(auth) or _is_admin(auth)):
        return classrooms, subjects, "⚠️ Faça login."

    classroom = _get_class_by_id(classrooms, cls_id)
    if not classroom:
        return classrooms, subjects, "⚠️ Sala não encontrada."

    if not _auth_matches_classroom_teacher(auth, classroom) and not _is_admin(auth):
        return classrooms, subjects, "⛔ Você não é professor desta sala."

    try:
        record = fetch_user_record(
            SUPABASE_URL,
            SUPABASE_SERVICE_ROLE_KEY,
            SUPABASE_USERS_TABLE,
            uname_norm,
        )
    except SupabaseConfigurationError:
        return (
            classrooms,
            subjects,
            "⚠️ Configure SUPABASE_URL e SUPABASE_SERVICE_ROLE_KEY para gerenciar alunos.",
        )
    except SupabaseOperationError as err:
        return classrooms, subjects, f"❌ Erro ao buscar usuário: {err}"

    if not record or not record.id:
        return classrooms, subjects, "⚠️ Usuário não encontrado."

    try:
        upsert_classroom_student(
            SUPABASE_URL,
            SUPABASE_SERVICE_ROLE_KEY,
            classroom_id=cls_id,
            student_id=record.id,
            status="active",
        )
    except SupabaseConfigurationError:
        return (
            classrooms,
            subjects,
            "⚠️ Configure SUPABASE_URL e SUPABASE_SERVICE_ROLE_KEY para gerenciar alunos.",
        )
    except SupabaseOperationError as err:
        return classrooms, subjects, f"❌ Erro ao adicionar aluno: {err}"

    classes, subjects_map, notice = _refresh_states(classrooms, subjects)
    md = _merge_notice(_render_teacher_members_md(cls_id, classes), notice or "✅ Aluno adicionado.")
    return classes, subjects_map, md


def teacher_rm_user(cls_id, uname, classrooms, subjects, auth):
    uname_norm = _normalize_username(uname)
    if not cls_id or not uname_norm:
        return classrooms, subjects, "⚠️ Informe sala e username."
    if not (_auth_user_id(auth) or _teacher_username(auth) or _is_admin(auth)):
        return classrooms, subjects, "⚠️ Faça login."

    classroom = _get_class_by_id(classrooms, cls_id)
    if not classroom:
        return classrooms, subjects, "⚠️ Sala não encontrada."

    if not _auth_matches_classroom_teacher(auth, classroom) and not _is_admin(auth):
        return classrooms, subjects, "⛔ Você não é professor desta sala."

    try:
        record = fetch_user_record(
            SUPABASE_URL,
            SUPABASE_SERVICE_ROLE_KEY,
            SUPABASE_USERS_TABLE,
            uname_norm,
        )
    except SupabaseConfigurationError:
        return (
            classrooms,
            subjects,
            "⚠️ Configure SUPABASE_URL e SUPABASE_SERVICE_ROLE_KEY para gerenciar alunos.",
        )
    except SupabaseOperationError as err:
        return classrooms, subjects, f"❌ Erro ao buscar usuário: {err}"

    if not record or not record.id:
        return classrooms, subjects, "⚠️ Usuário não encontrado."

    members = classroom.get("members") or {}
    student_ids = {str(uid) for uid in (members.get("students") or []) if uid}
    student_usernames = members.get("student_usernames") or {}

    is_member = False
    if record.id and str(record.id) in student_ids:
        is_member = True
    else:
        normalized_values = {
            _normalize_username(value): str(uid)
            for uid, value in student_usernames.items()
            if uid and value
        }
        candidate_usernames = {
            uname_norm,
            _normalize_username(getattr(record, "username", None)),
            _normalize_username(getattr(record, "email", None)),
        }
        is_member = any(
            candidate and candidate in normalized_values for candidate in candidate_usernames
        )

    if not is_member:
        md = _merge_notice(
            _render_teacher_members_md(cls_id, classrooms),
            "⚠️ Usuário não é aluno desta sala.",
        )
        return classrooms, subjects, md

    try:
        remove_classroom_student(
            SUPABASE_URL,
            SUPABASE_SERVICE_ROLE_KEY,
            classroom_id=cls_id,
            student_id=record.id,
        )
    except SupabaseConfigurationError:
        return (
            classrooms,
            subjects,
            "⚠️ Configure SUPABASE_URL e SUPABASE_SERVICE_ROLE_KEY para gerenciar alunos.",
        )
    except SupabaseOperationError as err:
        return classrooms, subjects, f"❌ Erro ao remover aluno: {err}"

    classes, subjects_map, notice = _refresh_states(classrooms, subjects)
    md = _merge_notice(_render_teacher_members_md(cls_id, classes), notice or "✅ Aluno removido.")
    return classes, subjects_map, md


def teacher_subjects_refresh(auth, classrooms, selected_id, subjects_by_class):
    return _subjects_choices_teacher(auth, classrooms, selected_id, subjects_by_class)


def teacher_add_subject(auth, selected_id, subj, subjects_by_class, classrooms):
    if not selected_id:
        return classrooms, subjects_by_class, gr.update(), gr.update(), "ℹ️ Selecione uma sala."
    classroom = _get_class_by_id(classrooms, selected_id)
    if not classroom:
        return classrooms, subjects_by_class, gr.update(), gr.update(), "⚠️ Sala não encontrada."
    if not _auth_matches_classroom_teacher(auth, classroom) and not _is_admin(auth):
        return classrooms, subjects_by_class, gr.update(), gr.update(), "⛔ Você não é professor desta sala."
    subj_name = (subj or "").strip()
    if not subj_name:
        return classrooms, subjects_by_class, gr.update(), gr.update(), "⚠️ Informe o nome do subtema."
    current = list(subjects_by_class.get(selected_id, []))
    if any(s.get("name", "").lower() == subj_name.lower() for s in current):
        return classrooms, subjects_by_class, gr.update(), gr.update(), "⚠️ Esse subtema já existe."

    creator_id = _auth_user_id(auth) or ""
    try:
        create_subject_record(
            SUPABASE_URL,
            SUPABASE_SERVICE_ROLE_KEY,
            classroom_id=selected_id,
            name=subj_name,
            created_by=creator_id,
        )
    except SupabaseConfigurationError:
        return (
            classrooms,
            subjects_by_class,
            gr.update(),
            gr.update(),
            "⚠️ Configure SUPABASE_URL e SUPABASE_SERVICE_ROLE_KEY para gerenciar subtemas.",
        )
    except SupabaseOperationError as err:
        return classrooms, subjects_by_class, gr.update(), gr.update(), f"❌ Erro ao adicionar subtema: {err}"

    classes, subjects_map, notice = _refresh_states(classrooms, subjects_by_class)
    dd, chk, md = _subjects_choices_teacher(auth, classes, selected_id, subjects_map)
    md = _merge_notice(md, notice or "✅ Subtema adicionado.")
    return classes, subjects_map, dd, chk, md


def teacher_apply_active(auth, selected_id, actives, subjects_by_class, classrooms):
    if not selected_id:
        return classrooms, subjects_by_class, "ℹ️ Selecione uma sala."
    classroom = _get_class_by_id(classrooms, selected_id)
    if not classroom:
        return classrooms, subjects_by_class, "⚠️ Sala não encontrada."
    if not _auth_matches_classroom_teacher(auth, classroom) and not _is_admin(auth):
        return classrooms, subjects_by_class, "⛔ Você não é professor desta sala."

    current = list(subjects_by_class.get(selected_id, []))
    actives_set = set(actives or [])

    try:
        for subject in current:
            subject_id = subject.get("id")
            if not subject_id:
                continue
            desired_active = subject.get("name") in actives_set
            update_subject_active(
                SUPABASE_URL,
                SUPABASE_SERVICE_ROLE_KEY,
                subject_id=subject_id,
                is_active=desired_active,
            )
    except SupabaseConfigurationError:
        return (
            classrooms,
            subjects_by_class,
            "⚠️ Configure SUPABASE_URL e SUPABASE_SERVICE_ROLE_KEY para gerenciar subtemas.",
        )
    except SupabaseOperationError as err:
        return classrooms, subjects_by_class, f"❌ Erro ao atualizar subtemas: {err}"

    classes, subjects_map, notice = _refresh_states(classrooms, subjects_by_class)
    md = _merge_notice(
        _render_subjects_md(subjects_map, selected_id, classes or []),
        notice or "✅ Subtemas atualizados.",
    )
    return classes, subjects_map, md


def teacher_upload_document(
    files,
    selected_id,
    classrooms,
    subjects_by_class,
    auth,
):
    classroom = _get_class_by_id(classrooms, selected_id)
    if not classroom:
        return (
            classrooms,
            subjects_by_class,
            gr.update(value="⚠️ Sala não encontrada."),
            gr.update(value=_render_documents_md(selected_id, classrooms)),
            _documents_dropdown(classrooms, selected_id),
            gr.update(value=""),
            gr.update(value=None),
        )

    if not _auth_matches_classroom_teacher(auth, classroom) and not _is_admin(auth):
        return (
            classrooms,
            subjects_by_class,
            gr.update(value="⛔ Você não é professor desta sala."),
            gr.update(value=_render_documents_md(selected_id, classrooms)),
            _documents_dropdown(classrooms, selected_id),
            gr.update(value=""),
            gr.update(value=None),
        )

    file_obj = None
    if isinstance(files, list):
        file_obj = files[0] if files else None
    else:
        file_obj = files

    if file_obj is None:
        return (
            classrooms,
            subjects_by_class,
            gr.update(value="⚠️ Selecione um arquivo para enviar."),
            gr.update(value=_render_documents_md(selected_id, classrooms)),
            _documents_dropdown(classrooms, selected_id),
            gr.update(value=""),
            gr.update(value=None),
        )

    file_path = getattr(file_obj, "name", None)
    orig_name = getattr(file_obj, "orig_name", None) or os.path.basename(file_path or "")
    display_name = orig_name or "Documento"
    safe_name = _safe_document_filename(display_name)
    storage_name = f"{uuid4().hex}_{safe_name}"
    storage_path = f"{selected_id}/{storage_name}"
    content_type = getattr(file_obj, "mime_type", None)
    if not content_type and file_path:
        guessed, _ = mimetypes.guess_type(file_path)
        content_type = guessed or None
    file_size = None
    if file_path and os.path.isfile(file_path):
        try:
            file_size = os.path.getsize(file_path)
        except OSError:
            file_size = None

    try:
        stored_path = upload_file_to_bucket(
            SUPABASE_URL,
            SUPABASE_SERVICE_ROLE_KEY,
            bucket=SUPABASE_CLASS_DOCS_BUCKET,
            file_path=file_path,
            storage_path=storage_path,
            content_type=content_type,
        )
    except SupabaseConfigurationError:
        return (
            classrooms,
            subjects_by_class,
            gr.update(
                value="⚠️ Configure SUPABASE_URL, SUPABASE_SERVICE_ROLE_KEY e SUPABASE_CLASS_DOCS_BUCKET para enviar materiais."
            ),
            gr.update(value=_render_documents_md(selected_id, classrooms)),
            _documents_dropdown(classrooms, selected_id),
            gr.update(value=""),
            gr.update(value=None),
        )
    except SupabaseOperationError as err:
        return (
            classrooms,
            subjects_by_class,
            gr.update(value=f"❌ Erro ao enviar arquivo: {err}"),
            gr.update(value=_render_documents_md(selected_id, classrooms)),
            _documents_dropdown(classrooms, selected_id),
            gr.update(value=""),
            gr.update(value=None),
        )

    uploader_id = _auth_user_id(auth)

    try:
        created = create_classroom_document_record(
            SUPABASE_URL,
            SUPABASE_SERVICE_ROLE_KEY,
            classroom_id=selected_id,
            name=display_name,
            storage_bucket=SUPABASE_CLASS_DOCS_BUCKET,
            storage_path=stored_path or storage_path,
            uploaded_by=uploader_id,
            file_size=file_size,
            content_type=content_type,
        )
    except SupabaseConfigurationError:
        try:
            delete_file_from_bucket(
                SUPABASE_URL,
                SUPABASE_SERVICE_ROLE_KEY,
                bucket=SUPABASE_CLASS_DOCS_BUCKET,
                storage_path=stored_path or storage_path,
            )
        except (SupabaseOperationError, SupabaseConfigurationError):
            pass
        return (
            classrooms,
            subjects_by_class,
            gr.update(
                value="⚠️ Configure SUPABASE_URL e SUPABASE_SERVICE_ROLE_KEY para registrar o material."
            ),
            gr.update(value=_render_documents_md(selected_id, classrooms)),
            _documents_dropdown(classrooms, selected_id),
            gr.update(value=""),
            gr.update(value=None),
        )
    except SupabaseOperationError as err:
        try:
            delete_file_from_bucket(
                SUPABASE_URL,
                SUPABASE_SERVICE_ROLE_KEY,
                bucket=SUPABASE_CLASS_DOCS_BUCKET,
                storage_path=stored_path or storage_path,
            )
        except (SupabaseOperationError, SupabaseConfigurationError):
            pass
        return (
            classrooms,
            subjects_by_class,
            gr.update(value=f"❌ Erro ao registrar material: {err}"),
            gr.update(value=_render_documents_md(selected_id, classrooms)),
            _documents_dropdown(classrooms, selected_id),
            gr.update(value=""),
            gr.update(value=None),
        )

    classes, subjects_map, notice = _refresh_states(classrooms, subjects_by_class)
    doc_id = created.get("id") if isinstance(created, dict) else None
    dropdown_update = _documents_dropdown(classes, selected_id, current_value=doc_id)
    message = notice or "✅ Material enviado com sucesso."
    last_upload_value = (
        file_path if file_path and os.path.isfile(file_path) else None
    )
    return (
        classes,
        subjects_map,
        gr.update(value=message),
        gr.update(value=_render_documents_md(selected_id, classes)),
        dropdown_update,
        gr.update(value=""),
        gr.update(value=last_upload_value),
    )


def teacher_rename_document(
    document_id,
    new_name,
    selected_id,
    classrooms,
    subjects_by_class,
    auth,
):
    classroom = _get_class_by_id(classrooms, selected_id)
    if not classroom:
        return (
            classrooms,
            subjects_by_class,
            gr.update(value="⚠️ Sala não encontrada."),
            gr.update(value=_render_documents_md(selected_id, classrooms)),
            _documents_dropdown(classrooms, selected_id),
            gr.update(value=new_name or ""),
        )

    if not _auth_matches_classroom_teacher(auth, classroom) and not _is_admin(auth):
        return (
            classrooms,
            subjects_by_class,
            gr.update(value="⛔ Você não é professor desta sala."),
            gr.update(value=_render_documents_md(selected_id, classrooms)),
            _documents_dropdown(classrooms, selected_id, current_value=document_id),
            gr.update(value=new_name or ""),
        )

    doc = _get_document_by_id(classrooms, selected_id, document_id)
    if not doc:
        return (
            classrooms,
            subjects_by_class,
            gr.update(value="⚠️ Material não encontrado."),
            gr.update(value=_render_documents_md(selected_id, classrooms)),
            _documents_dropdown(classrooms, selected_id),
            gr.update(value=new_name or ""),
        )

    normalized_name = (new_name or "").strip()
    if not normalized_name:
        return (
            classrooms,
            subjects_by_class,
            gr.update(value="⚠️ Informe um novo nome para o material."),
            gr.update(value=_render_documents_md(selected_id, classrooms)),
            _documents_dropdown(classrooms, selected_id, current_value=document_id),
            gr.update(value=new_name or ""),
        )

    try:
        update_classroom_document_record(
            SUPABASE_URL,
            SUPABASE_SERVICE_ROLE_KEY,
            document_id=document_id,
            name=normalized_name,
        )
    except SupabaseConfigurationError:
        return (
            classrooms,
            subjects_by_class,
            gr.update(
                value="⚠️ Configure SUPABASE_URL e SUPABASE_SERVICE_ROLE_KEY para atualizar materiais."
            ),
            gr.update(value=_render_documents_md(selected_id, classrooms)),
            _documents_dropdown(classrooms, selected_id, current_value=document_id),
            gr.update(value=new_name or ""),
        )
    except SupabaseOperationError as err:
        return (
            classrooms,
            subjects_by_class,
            gr.update(value=f"❌ Erro ao renomear material: {err}"),
            gr.update(value=_render_documents_md(selected_id, classrooms)),
            _documents_dropdown(classrooms, selected_id, current_value=document_id),
            gr.update(value=new_name or ""),
        )

    classes, subjects_map, notice = _refresh_states(classrooms, subjects_by_class)
    dropdown_update = _documents_dropdown(classes, selected_id, current_value=document_id)
    message = notice or "✅ Material renomeado."
    return (
        classes,
        subjects_map,
        gr.update(value=message),
        gr.update(value=_render_documents_md(selected_id, classes)),
        dropdown_update,
        gr.update(value=""),
    )


def teacher_delete_document(
    document_id,
    selected_id,
    classrooms,
    subjects_by_class,
    auth,
):
    classroom = _get_class_by_id(classrooms, selected_id)
    if not classroom:
        return (
            classrooms,
            subjects_by_class,
            gr.update(value="⚠️ Sala não encontrada."),
            gr.update(value=_render_documents_md(selected_id, classrooms)),
            _documents_dropdown(classrooms, selected_id),
            gr.update(value=""),
        )

    if not _auth_matches_classroom_teacher(auth, classroom) and not _is_admin(auth):
        return (
            classrooms,
            subjects_by_class,
            gr.update(value="⛔ Você não é professor desta sala."),
            gr.update(value=_render_documents_md(selected_id, classrooms)),
            _documents_dropdown(classrooms, selected_id, current_value=document_id),
            gr.update(value=""),
        )

    doc = _get_document_by_id(classrooms, selected_id, document_id)
    if not doc:
        return (
            classrooms,
            subjects_by_class,
            gr.update(value="⚠️ Material não encontrado."),
            gr.update(value=_render_documents_md(selected_id, classrooms)),
            _documents_dropdown(classrooms, selected_id),
            gr.update(value=""),
        )

    bucket = doc.get("storage_bucket") or SUPABASE_CLASS_DOCS_BUCKET
    storage_path = doc.get("storage_path")

    if bucket and storage_path:
        try:
            delete_file_from_bucket(
                SUPABASE_URL,
                SUPABASE_SERVICE_ROLE_KEY,
                bucket=bucket,
                storage_path=storage_path,
            )
        except SupabaseConfigurationError:
            return (
                classrooms,
                subjects_by_class,
                gr.update(
                    value="⚠️ Configure SUPABASE_URL, SUPABASE_SERVICE_ROLE_KEY e SUPABASE_CLASS_DOCS_BUCKET para remover materiais."
                ),
                gr.update(value=_render_documents_md(selected_id, classrooms)),
                _documents_dropdown(classrooms, selected_id, current_value=document_id),
                gr.update(value=""),
            )
        except SupabaseOperationError as err:
            return (
                classrooms,
                subjects_by_class,
                gr.update(value=f"❌ Erro ao remover arquivo do Storage: {err}"),
                gr.update(value=_render_documents_md(selected_id, classrooms)),
                _documents_dropdown(classrooms, selected_id, current_value=document_id),
                gr.update(value=""),
            )

    try:
        delete_classroom_document_record(
            SUPABASE_URL,
            SUPABASE_SERVICE_ROLE_KEY,
            document_id=document_id,
        )
    except SupabaseConfigurationError:
        return (
            classrooms,
            subjects_by_class,
            gr.update(
                value="⚠️ Configure SUPABASE_URL e SUPABASE_SERVICE_ROLE_KEY para remover materiais."
            ),
            gr.update(value=_render_documents_md(selected_id, classrooms)),
            _documents_dropdown(classrooms, selected_id, current_value=document_id),
            gr.update(value=""),
        )
    except SupabaseOperationError as err:
        return (
            classrooms,
            subjects_by_class,
            gr.update(value=f"❌ Erro ao remover material: {err}"),
            gr.update(value=_render_documents_md(selected_id, classrooms)),
            _documents_dropdown(classrooms, selected_id, current_value=document_id),
            gr.update(value=""),
        )

    classes, subjects_map, notice = _refresh_states(classrooms, subjects_by_class)
    dropdown_update = _documents_dropdown(classes, selected_id)
    message = notice or "✅ Material removido."
    return (
        classes,
        subjects_map,
        gr.update(value=message),
        gr.update(value=_render_documents_md(selected_id, classes)),
        dropdown_update,
        gr.update(value=""),
    )


def _teacher_on_select(auth, classrooms, selected_id, subjects_by_class):
    md_members = _render_teacher_members_md(selected_id, classrooms)
    dd, chk, md_subjects = _subjects_choices_teacher(
        auth, classrooms, selected_id, subjects_by_class
    )
    docs_md = _render_documents_md(selected_id, classrooms)
    docs_dd = _documents_dropdown(classrooms, selected_id)
    return (
        md_members,
        dd,
        chk,
        md_subjects,
        docs_md,
        docs_dd,
        gr.update(value=""),
        gr.update(value=""),
        gr.update(value=None),
    )


def build_teacher_view(
    *,
    blocks: gr.Blocks,
    auth_state: gr.State,
    classrooms_state: gr.State,
    subjects_state: gr.State,
    home_view: gr.Column,
) -> TeacherView:
    teacher_history_state = gr.State([])
    teacher_history_selected = gr.State(None)
    teacher_history_transcript = gr.State("")
    teacher_download_path = gr.State(None)

    with gr.Column(visible=False) as viewTeacher:
        gr.Markdown("## 🏫 Gerenciar Salas")
        teacherNotice = gr.Markdown("")
        with gr.Group():
            with gr.Row():
                tClsName = gr.Textbox(label="Nome da sala", placeholder="Ex.: Algoritmos e ED")
                tClsTheme = gr.Textbox(label="Tema (exibição)", placeholder="Ex.: Algoritmos")
            tClsDesc = gr.Textbox(label="Descrição (opcional)")
            with gr.Row():
                tClsLocked = gr.Checkbox(value=True, label="Tema travado")
                btnTeacherAddClass = gr.Button("➕ Criar sala", variant="primary")
        with gr.Accordion("Selecionar sala", open=False):
            with gr.Row():
                tSelectClass = gr.Dropdown(choices=[], label="Minhas salas", value=None)
                btnTeacherRefresh = gr.Button("🔄")
        with gr.Accordion("Membros (Professores/Alunos)", open=False):
            with gr.Row():
                tAddTeacher = gr.Textbox(label="Adicionar professor (username)")
                btnTeacherAddTeacher = gr.Button("👩‍🏫 Adicionar")
            tTeachersMd = gr.Markdown("")
            with gr.Row():
                tAddStudent = gr.Textbox(label="Adicionar aluno (username)")
                btnTeacherAddStudent = gr.Button("🎓 Adicionar")
            with gr.Row():
                tRmUser = gr.Textbox(label="Remover usuário (username)")
                btnTeacherRmUser = gr.Button("🗑️ Remover")
            tMembersMd = gr.Markdown("")
        with gr.Accordion("Subtemas da sala", open=False):
            with gr.Row():
                tSubjName = gr.Textbox(label="Novo subtema", placeholder="Ex.: Ponteiros")
                btnTeacherAddSubj = gr.Button("➕ Adicionar subtema")
            with gr.Row():
                tActiveList = gr.CheckboxGroup(choices=[], label="Ativar/desativar subtemas", value=[])
                btnTeacherApplyActive = gr.Button("✅ Aplicar ativações")
            tSubjectsMd = gr.Markdown("")
        with gr.Accordion("Materiais da sala", open=False):
            tDocsMd = gr.Markdown("ℹ️ Selecione uma sala para visualizar os materiais.")
            with gr.Row():
                tDocsUpload = gr.UploadButton(
                    "⬆️ Enviar material",
                    file_types=["file"],
                    file_count="single",
                )
                tDocsLastUpload = gr.File(label="Último upload", interactive=False)
            tDocsSelect = gr.Dropdown(
                choices=[], label="Materiais cadastrados", value=None
            )
            with gr.Row():
                tDocsRename = gr.Textbox(
                    label="Renomear material",
                    placeholder="Novo nome para o material selecionado",
                )
                btnDocsRename = gr.Button("✏️ Renomear")
                btnDocsDelete = gr.Button("🗑️ Excluir", variant="stop")
            tDocsNotice = gr.Markdown("")
        with gr.Accordion("Parâmetros do Chat da Sala", open=False):
            with gr.Row():
                tTemp = gr.Slider(0.0, 1.5, value=0.7, step=0.05, label="temperature")
                tTopP = gr.Slider(0.0, 1.0, value=0.95, step=0.01, label="top_p")
            with gr.Row():
                tTopK = gr.Slider(1, 100, value=40, step=1, label="top_k")
                tMaxT = gr.Slider(128, 4096, value=1024, step=64, label="max_tokens")
            with gr.Row():
                tEstilo = gr.Radio(choices=["técnicas", "simples"], value="simples", label="Estilo de linguagem")
                tDetalhamento = gr.Radio(choices=["detalhadas", "curtas"], value="detalhadas", label="Nível de detalhamento")
            tExtras = gr.Textbox(
                label="Preferências adicionais (extras)",
                placeholder="Ex.: evitar jargões, passo-a-passo sempre, etc.",
            )
            with gr.Row():
                btnTeacherSaveParams = gr.Button("💾 Salvar parâmetros da sala", variant="primary")
                btnTeacherLoadParams = gr.Button("🔄 Carregar da sala selecionada")
            tParamsMsg = gr.Markdown("")
        with gr.Accordion("Histórico de Chats", open=False):
            with gr.Row():
                tHistoryClass = gr.Dropdown(choices=[], label="Sala", value=None)
                tHistoryRefresh = gr.Button("🔄 Atualizar histórico")
            tHistoryInfo = gr.Markdown("Selecione uma sala para listar os chats.")
            tHistoryTable = gr.Dataframe(
                headers=list(HISTORY_TABLE_HEADERS),
                datatype=["str"] * 6,
                interactive=False,
                wrap=True,
            )
            with gr.Row():
                tHistoryChat = gr.Dropdown(choices=[], label="Chat registrado", value=None)
                tHistoryLoad = gr.Button("📄 Ver detalhes")
            tHistoryMetadata = gr.Markdown(
                "ℹ️ Selecione um chat para visualizar os detalhes.",
                elem_classes=["history-box"],
            )
            gr.Markdown("#### Resumo da IA")
            tHistorySummary = gr.Markdown(
                "ℹ️ Selecione um chat para visualizar o resumo.",
                elem_classes=["history-box"],
            )
            gr.Markdown("#### Prévia do PDF")
            tHistoryPreview = gr.Markdown(
                "ℹ️ Carregue um chat para visualizar a prévia.",
                elem_classes=["history-box"],
            )
            with gr.Row():
                tHistoryDownload = gr.DownloadButton(
                    "⬇️ Baixar PDF", visible=False, variant="secondary"
                )
                tHistoryGenerateEval = gr.Button(
                    "🤖 Gerar avaliação automática", variant="secondary"
                )
            tHistoryEvaluation = gr.Textbox(
                label="Avaliação automática (Vertex)", lines=6, interactive=False, value=""
            )
            tManualRating = gr.Slider(
                0,
                100,
                value=0,
                step=1,
                label="Avaliação manual (0-100)",
            )
            gr.Markdown("A nota selecionada será registrada junto com o comentário enviado.")
            tHistoryComments = gr.Markdown(
                "ℹ️ Nenhum comentário registrado ainda.",
                elem_classes=["history-box"],
            )
            tCommentInput = gr.Textbox(
                label="Novo comentário",
                placeholder="Registre observações para outros professores",
            )
            with gr.Row():
                tAddComment = gr.Button("💬 Registrar comentário")
            tHistoryNotice = gr.Markdown("")
        tClassroomsMd = gr.Markdown("")
        with gr.Row():
            btnTeacherBack = gr.Button("← Voltar à Home")

    btnTeacherBack.click(
        lambda: (gr.update(visible=True), gr.update(visible=False)),
        inputs=None,
        outputs=[home_view, viewTeacher],
    )

    btnTeacherAddTeacher.click(
        teacher_add_teacher,
        inputs=[tSelectClass, tAddTeacher, classrooms_state, subjects_state, auth_state],
        outputs=[classrooms_state, subjects_state, tTeachersMd],
    )

    btnTeacherLoadParams.click(
        teacher_load_params,
        inputs=[tSelectClass, classrooms_state],
        outputs=[tTemp, tTopP, tTopK, tMaxT, tEstilo, tDetalhamento, tExtras, tParamsMsg],
    )

    btnTeacherSaveParams.click(
        teacher_save_params,
        inputs=[
            tSelectClass,
            tTemp,
            tTopP,
            tTopK,
            tMaxT,
            tEstilo,
            tDetalhamento,
            tExtras,
            classrooms_state,
            subjects_state,
            auth_state,
        ],
        outputs=[classrooms_state, subjects_state, tParamsMsg],
    )

    btnTeacherAddClass.click(
        teacher_add_classroom,
        inputs=[
            tClsName,
            tClsTheme,
            tClsDesc,
            tClsLocked,
            classrooms_state,
            subjects_state,
            auth_state,
        ],
        outputs=[classrooms_state, subjects_state, tClassroomsMd, tSelectClass, tSelectClass, teacherNotice],
    ).then(
        _teacher_on_select,
        inputs=[auth_state, classrooms_state, tSelectClass, subjects_state],
        outputs=[
            tMembersMd,
            tSelectClass,
            tActiveList,
            tSubjectsMd,
            tDocsMd,
            tDocsSelect,
            tDocsRename,
            tDocsNotice,
            tDocsLastUpload,
        ],
    ).then(
        lambda auth, classrooms, current: _teacher_history_dropdown(auth, classrooms, current),
        inputs=[auth_state, classrooms_state, tHistoryClass],
        outputs=tHistoryClass,
    )

    btnTeacherRefresh.click(
        teacher_refresh,
        inputs=[auth_state, classrooms_state, subjects_state],
        outputs=[classrooms_state, subjects_state, tClassroomsMd, tSelectClass],
    ).then(
        _teacher_on_select,
        inputs=[auth_state, classrooms_state, tSelectClass, subjects_state],
        outputs=[
            tMembersMd,
            tSelectClass,
            tActiveList,
            tSubjectsMd,
            tDocsMd,
            tDocsSelect,
            tDocsRename,
            tDocsNotice,
            tDocsLastUpload,
        ],
    ).then(
        lambda auth, classrooms, current: _teacher_history_dropdown(auth, classrooms, current),
        inputs=[auth_state, classrooms_state, tHistoryClass],
        outputs=tHistoryClass,
    )

    tSelectClass.change(
        _teacher_on_select,
        inputs=[auth_state, classrooms_state, tSelectClass, subjects_state],
        outputs=[
            tMembersMd,
            tSelectClass,
            tActiveList,
            tSubjectsMd,
            tDocsMd,
            tDocsSelect,
            tDocsRename,
            tDocsNotice,
            tDocsLastUpload,
        ],
    )

    btnTeacherAddStudent.click(
        teacher_add_student,
        inputs=[tSelectClass, tAddStudent, classrooms_state, subjects_state, auth_state],
        outputs=[classrooms_state, subjects_state, tMembersMd],
    )

    btnTeacherRmUser.click(
        teacher_rm_user,
        inputs=[tSelectClass, tRmUser, classrooms_state, subjects_state, auth_state],
        outputs=[classrooms_state, subjects_state, tMembersMd],
    )

    btnTeacherAddSubj.click(
        teacher_add_subject,
        inputs=[auth_state, tSelectClass, tSubjName, subjects_state, classrooms_state],
        outputs=[classrooms_state, subjects_state, tSelectClass, tActiveList, tSubjectsMd],
    )

    btnTeacherApplyActive.click(
        teacher_apply_active,
        inputs=[auth_state, tSelectClass, tActiveList, subjects_state, classrooms_state],
        outputs=[classrooms_state, subjects_state, tSubjectsMd],
    )

    tDocsUpload.upload(
        teacher_upload_document,
        inputs=[tSelectClass, classrooms_state, subjects_state, auth_state],
        outputs=[
            classrooms_state,
            subjects_state,
            tDocsNotice,
            tDocsMd,
            tDocsSelect,
            tDocsRename,
            tDocsLastUpload,
        ],
    )

    btnDocsRename.click(
        teacher_rename_document,
        inputs=[
            tDocsSelect,
            tDocsRename,
            tSelectClass,
            classrooms_state,
            subjects_state,
            auth_state,
        ],
        outputs=[
            classrooms_state,
            subjects_state,
            tDocsNotice,
            tDocsMd,
            tDocsSelect,
            tDocsRename,
        ],
    )

    btnDocsDelete.click(
        teacher_delete_document,
        inputs=[tDocsSelect, tSelectClass, classrooms_state, subjects_state, auth_state],
        outputs=[
            classrooms_state,
            subjects_state,
            tDocsNotice,
            tDocsMd,
            tDocsSelect,
            tDocsRename,
        ],
    )

    tHistoryRefresh.click(
        teacher_history_refresh,
        inputs=[auth_state, tHistoryClass, classrooms_state],
        outputs=[
            tHistoryTable,
            teacher_history_state,
            tHistoryChat,
            tHistoryInfo,
            teacher_history_selected,
        ],
    ).then(
        teacher_history_load_chat,
        inputs=[
            teacher_history_selected,
            teacher_history_state,
            teacher_download_path,
        ],
        outputs=[
            teacher_history_selected,
            tHistoryMetadata,
            tHistorySummary,
            tHistoryPreview,
            tHistoryEvaluation,
            tManualRating,
            tHistoryComments,
            teacher_history_transcript,
            teacher_download_path,
            tHistoryDownload,
            tCommentInput,
        ],
    )

    tHistoryClass.change(
        teacher_history_refresh,
        inputs=[auth_state, tHistoryClass, classrooms_state],
        outputs=[
            tHistoryTable,
            teacher_history_state,
            tHistoryChat,
            tHistoryInfo,
            teacher_history_selected,
        ],
    ).then(
        teacher_history_load_chat,
        inputs=[
            teacher_history_selected,
            teacher_history_state,
            teacher_download_path,
        ],
        outputs=[
            teacher_history_selected,
            tHistoryMetadata,
            tHistorySummary,
            tHistoryPreview,
            tHistoryEvaluation,
            tManualRating,
            tHistoryComments,
            teacher_history_transcript,
            teacher_download_path,
            tHistoryDownload,
            tCommentInput,
        ],
    )

    tHistoryChat.change(
        teacher_history_load_chat,
        inputs=[tHistoryChat, teacher_history_state, teacher_download_path],
        outputs=[
            teacher_history_selected,
            tHistoryMetadata,
            tHistorySummary,
            tHistoryPreview,
            tHistoryEvaluation,
            tManualRating,
            tHistoryComments,
            teacher_history_transcript,
            teacher_download_path,
            tHistoryDownload,
            tCommentInput,
        ],
    )

    tHistoryLoad.click(
        teacher_history_load_chat,
        inputs=[tHistoryChat, teacher_history_state, teacher_download_path],
        outputs=[
            teacher_history_selected,
            tHistoryMetadata,
            tHistorySummary,
            tHistoryPreview,
            tHistoryEvaluation,
            tManualRating,
            tHistoryComments,
            teacher_history_transcript,
            teacher_download_path,
            tHistoryDownload,
            tCommentInput,
        ],
    )

    tHistoryGenerateEval.click(
        teacher_history_generate_evaluation,
        inputs=[
            teacher_history_selected,
            teacher_history_transcript,
            teacher_history_state,
        ],
        outputs=[tHistoryEvaluation, teacher_history_state, tHistoryMetadata, tHistoryNotice],
    )

    tAddComment.click(
        teacher_history_add_comment,
        inputs=[
            teacher_history_selected,
            tManualRating,
            tCommentInput,
            teacher_history_state,
            auth_state,
        ],
        outputs=[teacher_history_state, tCommentInput, tHistoryComments, tHistoryNotice],
    )

    tHistoryDownload.click(
        teacher_history_prepare_download,
        inputs=[teacher_download_path],
        outputs=tHistoryDownload,
    )


    return TeacherView(
        container=viewTeacher,
        notice=teacherNotice,
        select_dropdown=tSelectClass,
        back_button=btnTeacherBack,
        history_class_dropdown=tHistoryClass,
    )


__all__ = [
    "TeacherView",
    "build_teacher_view",
    "teacher_history_dropdown",
    "teacher_add_classroom",
    "teacher_refresh",
    "teacher_add_teacher",
    "teacher_add_student",
    "teacher_rm_user",
    "teacher_subjects_refresh",
    "teacher_add_subject",
    "teacher_apply_active",
    "teacher_upload_document",
    "teacher_rename_document",
    "teacher_delete_document",
    "teacher_load_params",
    "teacher_save_params",
]
