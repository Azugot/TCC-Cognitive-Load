"""Admin area utilities and Gradio view builders."""

from __future__ import annotations
from dataclasses import dataclass
from typing import Any, Dict, Iterable, List, Optional, Tuple

import gradio as gr

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

from app.config import (
    ROLE_PT_TO_DB,
    SUPABASE_SERVICE_ROLE_KEY,
    SUPABASE_URL,
    SUPABASE_USERS_TABLE,
)
from app.utils import (
    _auth_user_id,
    _class_member_labels,
    _get_class_by_id,
    _is_admin,
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
        return "⚠️ Nenhuma sala cadastrada ainda."
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
        return "⚠️ Cadastre uma sala primeiro."
    if not selected_id:
        return "ℹ️ Selecione uma sala para gerenciar os subtemas."
    names = {c["id"]: c["name"] for c in classrooms}
    subjects = subjects_by_class.get(selected_id, [])
    title = f"### Subtemas da sala **{names.get(selected_id, '?')}**\n"
    if not subjects:
        return title + "⚠️ Nenhum subtema cadastrado."
    bullets = []
    for s in subjects:
        mark = "✅" if s.get("active") else "⏸️"
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
            "⚠️ Apenas administradores podem visualizar todos os chats.",
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
            "⚠️ Configure SUPABASE_URL e SUPABASE_SERVICE_ROLE_KEY para consultar o histórico.",
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
        f"✅ {len(filtered)} chat(s) encontrados." if filtered else "ℹ️ Nenhum chat para o filtro aplicado."
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
    gr.Warning("⚠️ Nenhum arquivo disponível para download.")
    return None


def _render_eval_md(chat):
    if not chat:
        return "⚠️ Selecione um chat para avaliar."
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
        warn = "⚠️ Configure SUPABASE_URL e SUPABASE_SERVICE_ROLE_KEY para gerenciar as salas."
        return current_classrooms or [], current_subjects or {}, warn
    except SupabaseOperationError as err:
        warn = f"❌ Erro ao consultar salas: {err}"
        return current_classrooms or [], current_subjects or {}, warn

    normalized_classrooms = []
    for item in raw_classrooms:
        teacher_map = {}
        for entry in item.get("teachers", []) or []:
            login = _normalize_username(entry.get("login"))
            if not login:
                continue
            display = (entry.get("display_name") or "").strip()
            teacher_map[login] = display or login

        student_map = {}
        for entry in item.get("students", []) or []:
            login = _normalize_username(entry.get("login"))
            if not login:
                continue
            status = str(entry.get("status", "active")).lower()
            if status != "active":
                continue
            display = (entry.get("display_name") or "").strip()
            student_map[login] = display or login

        owner_login = _normalize_username(item.get("owner_login"))
        if owner_login:
            if owner_login not in teacher_map:
                owner_label = None
                owner_id = item.get("owner_id")
                if owner_id:
                    for entry in item.get("teachers", []) or []:
                        if entry.get("user_id") == owner_id:
                            owner_label = (entry.get("display_name") or "").strip()
                            break
                teacher_map[owner_login] = owner_label or teacher_map.get(owner_login) or owner_login

        teacher_map = {login: label for login, label in teacher_map.items() if login}
        student_map = {login: label for login, label in student_map.items() if login}

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
                    "teachers": sorted(teacher_map),
                    "students": sorted(student_map),
                    "teacher_labels": teacher_map,
                    "student_labels": student_map,
                },
                "owner": owner_login,
                "owner_id": item.get("owner_id"),
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


def _admin_classrooms_outputs(classrooms, notice=""):
    md = _render_classrooms_md(classrooms or [])
    md = _merge_notice(md, notice)
    dd1, dd2 = _refresh_cls_dropdown(classrooms or [])
    return md, dd1, dd2


def _refresh_cls_dropdown(classrooms):
    choices = [(c["name"], c["id"]) for c in (classrooms or [])]
    return gr.update(choices=choices), gr.update(choices=choices)


def add_classroom(name, theme, desc, locked, classrooms, subjects, auth):
    role = (auth or {}).get("role")
    if (role or "").lower() not in ("admin", "professor"):
        md, dd1, dd2 = _admin_classrooms_outputs(classrooms, "⛔ Apenas professores ou admins podem criar salas.")
        return classrooms, subjects, md, dd1, dd2

    creator_id = _auth_user_id(auth)
    if not creator_id:
        md, dd1, dd2 = _admin_classrooms_outputs(classrooms, "⚠️ Faça login para criar salas.")
        return classrooms, subjects, md, dd1, dd2

    name = (name or "").strip()
    theme = (theme or "").strip() or name
    description = (desc or "").strip() or ""
    if not name:
        md, dd1, dd2 = _admin_classrooms_outputs(classrooms, "⚠️ Informe um nome para a sala.")
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
            "⚠️ Configure SUPABASE_URL e SUPABASE_SERVICE_ROLE_KEY para cadastrar salas.",
        )
        return classrooms, subjects, md, dd1, dd2
    except SupabaseOperationError as err:
        md, dd1, dd2 = _admin_classrooms_outputs(classrooms, f"❌ Erro ao criar sala: {err}")
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
    md, dd1, dd2 = _admin_classrooms_outputs(classes, notice or "✅ Sala criada.")
    return classes, subjects_map, md, dd1, dd2


def refresh_classrooms(classrooms, subjects):
    classes, subjects_map, notice = _refresh_states(classrooms, subjects)
    md, dd1, dd2 = _admin_classrooms_outputs(classes, notice)
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
            "⚠️ Sala não encontrada.",
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
        md = _merge_notice(_render_classrooms_md(classrooms or []), "⚠️ Selecione uma sala.")
        return classrooms, subjects, md

    original = next((c for c in (classrooms or []) if c.get("id") == cls_id), None)
    if not original:
        md = _merge_notice(_render_classrooms_md(classrooms or []), "⚠️ Sala não encontrada.")
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
            "⚠️ Configure SUPABASE_URL e SUPABASE_SERVICE_ROLE_KEY para atualizar salas.",
        )
        return classrooms, subjects, md
    except SupabaseOperationError as err:
        md = _merge_notice(
            _render_classrooms_md(classrooms or []),
            f"❌ Erro ao atualizar sala: {err}",
        )
        return classrooms, subjects, md

    classes, subjects_map, notice = _refresh_states(classrooms, subjects)
    md = _merge_notice(_render_classrooms_md(classes), notice or "✅ Sala atualizada.")
    return classes, subjects_map, md


def delete_cls(cls_id, classrooms, subjects):
    if not cls_id:
        md = _merge_notice(_render_classrooms_md(classrooms or []), "⚠️ Selecione uma sala.")
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
            "⚠️ Configure SUPABASE_URL e SUPABASE_SERVICE_ROLE_KEY para excluir salas.",
        )
        return classrooms, subjects, md
    except SupabaseOperationError as err:
        md = _merge_notice(
            _render_classrooms_md(classrooms or []),
            f"❌ Erro ao excluir sala: {err}",
        )
        return classrooms, subjects, md

    classes, subjects_map, notice = _refresh_states(classrooms, subjects)
    md = _merge_notice(_render_classrooms_md(classes), notice or "✅ Sala excluída.")
    return classes, subjects_map, md


def _render_members_md(cls_id, classrooms):
    c = next((x for x in (classrooms or []) if x["id"] == cls_id), None)
    if not c:
        return "⚠️ Selecione uma sala."
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
        return classrooms, subjects, "⚠️ Informe sala e username."
    uname_norm = _normalize_username(uname)
    me = _teacher_username(auth)
    classroom = _get_class_by_id(classrooms, cls_id)
    if not classroom:
        return classrooms, subjects, "⚠️ Sala não encontrada."

    normalized = [_normalize_username(t) for t in classroom["members"]["teachers"]]
    if me not in normalized and not _is_admin(auth):
        return classrooms, subjects, "⛔ Você não é professor desta sala."

    owner = _normalize_username(classroom.get("owner"))
    if owner and me != owner and not _is_admin(auth):
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
        return classrooms, subjects, "⚠️ Configure SUPABASE_URL e SUPABASE_SERVICE_ROLE_KEY para gerenciar professores."
    except SupabaseOperationError as err:
        return classrooms, subjects, f"❌ Erro ao buscar usuário: {err}"

    if not record or not record.id:
        return classrooms, subjects, "⚠️ Usuário não encontrado."

    role_label = None
    if not owner and me:
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
        return classrooms, subjects, "⚠️ Configure SUPABASE_URL e SUPABASE_SERVICE_ROLE_KEY para gerenciar professores."
    except SupabaseOperationError as err:
        return classrooms, subjects, f"❌ Erro ao adicionar professor: {err}"

    classes, subjects_map, notice = _refresh_states(classrooms, subjects)
    md = _merge_notice(_render_members_md(cls_id, classes), notice or "✅ Professor adicionado.")
    return classes, subjects_map, md


def add_student(cls_id, uname, classrooms, subjects):
    if not cls_id or not uname:
        return classrooms, subjects, "⚠️ Informe sala e username."
    classroom = _get_class_by_id(classrooms, cls_id)
    if not classroom:
        return classrooms, subjects, "⚠️ Sala não encontrada."

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
        return classrooms, subjects, "⚠️ Configure SUPABASE_URL e SUPABASE_SERVICE_ROLE_KEY para gerenciar alunos."
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
        )
    except SupabaseConfigurationError:
        return classrooms, subjects, "⚠️ Configure SUPABASE_URL e SUPABASE_SERVICE_ROLE_KEY para gerenciar alunos."
    except SupabaseOperationError as err:
        return classrooms, subjects, f"❌ Erro ao adicionar aluno: {err}"

    classes, subjects_map, notice = _refresh_states(classrooms, subjects)
    md = _merge_notice(_render_members_md(cls_id, classes), notice or "✅ Aluno adicionado.")
    return classes, subjects_map, md


def remove_member(cls_id, uname, classrooms, subjects):
    if not cls_id or not uname:
        return classrooms, subjects, "⚠️ Informe sala e username."
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
        return classrooms, subjects, "⚠️ Configure SUPABASE_URL e SUPABASE_SERVICE_ROLE_KEY para gerenciar integrantes."
    except SupabaseOperationError as err:
        return classrooms, subjects, f"❌ Erro ao buscar usuário: {err}"

    if not record or not record.id:
        return classrooms, subjects, "⚠️ Usuário não encontrado."

    status_messages: List[str] = []
    try:
        remove_classroom_teacher(
            SUPABASE_URL,
            SUPABASE_SERVICE_ROLE_KEY,
            classroom_id=cls_id,
            teacher_id=record.id,
        )
    except SupabaseConfigurationError:
        return classrooms, subjects, "⚠️ Configure SUPABASE_URL e SUPABASE_SERVICE_ROLE_KEY para gerenciar integrantes."
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
        return classrooms, subjects, "⚠️ Configure SUPABASE_URL e SUPABASE_SERVICE_ROLE_KEY para gerenciar integrantes."
    except SupabaseOperationError as err:
        status_messages.append(f"Aluno: {err}")

    classes, subjects_map, notice = _refresh_states(classrooms, subjects)
    base_md = _render_members_md(cls_id, classes)
    message = "⚠️ " + "; ".join(status_messages) if status_messages else "✅ Usuário removido."
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
            "ℹ️ Selecione uma sala para adicionar subtemas.",
        )
        return classrooms, subjects_by_class, chk, md

    subj_name = (subj or "").strip()
    if not subj_name:
        chk, md = _admin_subjects_ui(
            classrooms,
            subjects_by_class,
            cls_id,
            "⚠️ Informe o nome do subtema.",
        )
        return classrooms, subjects_by_class, chk, md

    existing = list(subjects_by_class.get(cls_id, []))
    if any(s.get("name", "").lower() == subj_name.lower() for s in existing):
        chk, md = _admin_subjects_ui(
            classrooms,
            subjects_by_class,
            cls_id,
            "⚠️ Esse subtema já existe.",
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
            "⚠️ Configure SUPABASE_URL e SUPABASE_SERVICE_ROLE_KEY para gerenciar subtemas.",
        )
        return classrooms, subjects_by_class, chk, md
    except SupabaseOperationError as err:
        chk, md = _admin_subjects_ui(
            classrooms,
            subjects_by_class,
            cls_id,
            f"❌ Erro ao adicionar subtema: {err}",
        )
        return classrooms, subjects_by_class, chk, md

    classes, subjects_map, notice = _refresh_states(classrooms, subjects_by_class)
    chk, md = _admin_subjects_ui(classes, subjects_map, cls_id, notice or "✅ Subtema adicionado.")
    return classes, subjects_map, chk, md


def admin_apply_active(cls_id, actives, subjects_by_class, classrooms):
    if not cls_id:
        chk, md = _admin_subjects_ui(
            classrooms,
            subjects_by_class,
            None,
            "⚠️ Selecione uma sala.",
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
            "⚠️ Configure SUPABASE_URL e SUPABASE_SERVICE_ROLE_KEY para gerenciar subtemas.",
        )
        return classrooms, subjects_by_class, chk, md
    except SupabaseOperationError as err:
        chk, md = _admin_subjects_ui(
            classrooms,
            subjects_by_class,
            cls_id,
            f"❌ Erro ao atualizar subtemas: {err}",
        )
        return classrooms, subjects_by_class, chk, md

    classes, subjects_map, notice = _refresh_states(classrooms, subjects_by_class)
    chk, md = _admin_subjects_ui(classes, subjects_map, cls_id, notice or "✅ Subtemas atualizados.")
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
        return "ℹ️ Selecione um chat para visualizar/avaliar."
    chat = (chats_map or {}).get(chat_id)
    return _render_eval_md(chat)


def eval_save(chat_id, score, rubric, feedback, chats_map):
    if not chat_id or chat_id not in (chats_map or {}):
        return chats_map, "⚠️ Selecione um chat válido."
    cm = chats_map[chat_id]
    cm["score"] = int(score) if score is not None else None
    cm["rubric"] = (rubric or "").strip() or None
    cm["feedback"] = (feedback or "").strip() or None
    return chats_map, "✅ Avaliação salva."


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
        gr.Markdown("## 🛠️ Administração (Placeholder)")
        gr.Markdown(
            "- Gerenciar usuários/roles (futuro)\n"
            "- Parâmetros globais do sistema (futuro)\n"
            "- Logs/telemetria (futuro)\n"
        )
        with gr.Row():
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
                clsSelect = gr.Dropdown(choices=[], label="Selecione a sala", value=None)
                btnRefreshCls = gr.Button("🔄")
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
                membClass = gr.Dropdown(choices=[], label="Sala", value=None)
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
                btnClsApplyActive = gr.Button("✅ Aplicar ativações")
            clsSubjectsMd = gr.Markdown("")
        classroomsMd = gr.Markdown("")
        with gr.Row():
            clsBackAdminHome = gr.Button("← Voltar à Home do Admin")

    with gr.Column(visible=False) as viewHistory:
        gr.Markdown("## 🗂️ Histórico de Chats")
        with gr.Row():
            adHistoryClass = gr.Dropdown(choices=[], label="Sala", value="")
            adHistoryRefresh = gr.Button("🔄 Atualizar histórico")
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
            adHistoryChat = gr.Dropdown(choices=[], label="Chat registrado", value=None)
            adHistoryLoad = gr.Button("📄 Ver detalhes")
        adHistoryMetadata = gr.Markdown(
            "ℹ️ Selecione um chat para visualizar os detalhes.",
            elem_classes=["history-box"],
        )
        gr.Markdown("#### Resumo da IA")
        adHistorySummary = gr.Markdown(
            "ℹ️ Selecione um chat para visualizar o resumo.",
            elem_classes=["history-box"],
        )
        gr.Markdown("#### Prévia do PDF")
        adHistoryPreview = gr.Markdown(
            "ℹ️ Carregue um chat para visualizar a prévia.",
            elem_classes=["history-box"],
        )
        with gr.Row():
            adHistoryDownload = gr.DownloadButton("⬇️ Baixar PDF", visible=False, variant="secondary")
            adHistoryGenerateEval = gr.Button("🤖 Gerar avaliação automática", variant="secondary")
        adHistoryEvaluation = gr.Textbox(
            label="Avaliação automática (Vertex)", lines=6, interactive=False, value=""
        )
        adManualRating = gr.Slider(0, 100, value=0, step=1, label="Avaliação manual (0-100)")
        gr.Markdown("A nota selecionada será registrada junto ao comentário enviado.")
        adHistoryComments = gr.Markdown(
            "ℹ️ Nenhum comentário registrado ainda.",
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
            evalChatId = gr.Dropdown(choices=[], label="Chat para avaliar", value=None)
            btnEvalRefresh = gr.Button("🔄")
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
            btnProgRefresh = gr.Button("🔄 Atualizar")
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

    adminPgBack.click(
        lambda: _go_admin("home"),
        outputs=[admin_nav_state, viewHomeAdmin, viewClassrooms, viewHistory, viewEvaluate, viewProgress, viewAdminPg],
    )

    btnAddClass.click(
        add_classroom,
        inputs=[clsName, clsTheme, clsDesc, clsLocked, classrooms_state, subjects_state, auth_state],
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
        inputs=[classrooms_state, subjects_state],
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
    "eval_refresh_dropdown",
    "eval_load",
    "eval_save",
    "refresh_progress",
]
