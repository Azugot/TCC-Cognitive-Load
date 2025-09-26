"""Student area flows and supporting helpers."""

from __future__ import annotations

import os
import time
from dataclasses import dataclass
from typing import Any, Dict, Iterable, List, Optional, Sequence, Tuple

import gradio as gr

from services.docs import createChatPdf
from services.script_builder import buildCustomScript
from services.supabase_client import (
    SupabaseConfigurationError,
    SupabaseOperationError,
    create_chat_record,
    list_student_chats,
    upload_file_to_bucket,
)
from services.vertex_client import VERTEX_CFG, _vertex_err, summarize_chat_history

from app.config import (
    SUPABASE_CHAT_BUCKET,
    SUPABASE_CHAT_STORAGE_PREFIX,
    SUPABASE_SERVICE_ROLE_KEY,
    SUPABASE_URL,
    SUPABASE_USERS_TABLE,
)
from app.pages.chat import addMessage, bot, clearChat
from app.pages.history_shared import (
    HISTORY_TABLE_HEADERS,
    _format_timestamp,
    load_chat_entry,
    prepare_download,
    prepare_history_listing,
)
from app.utils import (
    _class_member_labels,
    _get_class_by_id,
    _mk_id,
    _normalize_username,
    _now_ts,
    _student_username,
    _user_role,
)


@dataclass
class StudentViews:
    rooms_view: gr.Column
    setup_view: gr.Column
    rooms_dropdown: gr.Dropdown
    rooms_refresh: gr.Button
    rooms_info: gr.Markdown
    enter_setup_button: gr.Button
    rooms_back_button: gr.Button
    config_column: gr.Column
    chat_column: gr.Column
    assunto: gr.Textbox
    subthemes: gr.CheckboxGroup
    estilo: gr.Radio
    detalhamento: gr.Radio
    objetivo: gr.Textbox
    interesses: gr.Textbox
    submit_button: gr.Button
    provider_markdown: gr.Markdown
    chatbot: gr.Chatbot
    clear_button: gr.Button
    back_to_setup_button: gr.Button
    end_chat_button: gr.Button
    chat_input: gr.MultimodalTextbox
    setup_back_button: gr.Button
    history_class_dropdown: gr.Dropdown
    history_refresh_button: gr.Button
    history_info: gr.Markdown
    history_table: gr.Dataframe
    history_chat_dropdown: gr.Dropdown
    history_load_button: gr.Button
    history_metadata: gr.Markdown
    history_summary: gr.Markdown
    history_preview: gr.Markdown
    history_download_button: gr.DownloadButton
    history_evaluation: gr.Textbox
    history_comments: gr.Markdown
    history_notice: gr.Markdown


def _student_history_dropdown(auth, classrooms, current_value=None):
    classes = _student_classes(auth, classrooms or [])
    choices = [(c["name"], c["id"]) for c in classes if c.get("id")]
    valid_ids = [value for _, value in choices]
    value = current_value if current_value in valid_ids else (valid_ids[0] if valid_ids else None)
    return gr.update(choices=choices, value=value)


def student_history_dropdown(auth, classrooms, current_value=None):
    return _student_history_dropdown(auth, classrooms, current_value)


def student_history_refresh(auth, classroom_filter):
    student_id = (auth or {}).get("user_id")
    if not student_id:
        return (
            gr.update(value=[]),
            [],
            gr.update(choices=[], value=None),
            "⚠️ Faça login como aluno para consultar seus chats.",
            None,
        )

    try:
        chats = list_student_chats(
            SUPABASE_URL,
            SUPABASE_SERVICE_ROLE_KEY,
            student_id=student_id,
            users_table=SUPABASE_USERS_TABLE,
        )
    except SupabaseConfigurationError:
        return (
            gr.update(value=[]),
            [],
            gr.update(choices=[], value=None),
            "⚠️ Configure o Supabase para carregar o histórico de chats.",
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

    def _filter_chat(chat: Dict[str, Any]) -> bool:
        return str(chat.get("classroom_id")) == classroom_filter

    def _dropdown_label(chat: Dict[str, Any]) -> str:
        classroom = chat.get("classroom_name") or chat.get("classroom_id") or "Sala"
        started = _format_timestamp(chat.get("started_at"))
        return f"{classroom} — {started}"

    filter_fn = _filter_chat if classroom_filter else None

    table_update, filtered, dropdown_update, message, default_id = prepare_history_listing(
        chats,
        column_labels=HISTORY_TABLE_HEADERS,
        filter_fn=filter_fn,
        dropdown_label=_dropdown_label,
        empty_message="ℹ️ Nenhum chat para o filtro selecionado.",
        found_message="✅ {count} chat(s) encontrados.",
    )

    return table_update, filtered, dropdown_update, message, default_id


def student_history_load_chat(chat_id, history_entries, current_download_path):
    result = load_chat_entry(chat_id, history_entries, current_download_path)

    if result.notice:
        if result.notice.startswith("❌"):
            gr.Error(result.notice)
        else:
            gr.Warning(result.notice)

    preview_value = result.preview_text or "ℹ️ Carregue um chat para visualizar a prévia."
    summary_value = result.summary_text or "ℹ️ Este chat ainda não possui um resumo gerado."

    return (
        result.chat_id,
        gr.update(value=result.metadata_md),
        gr.update(value=summary_value),
        gr.update(value=preview_value),
        gr.update(value=result.evaluation_text),
        gr.update(value=result.comments_md),
        result.transcript_text,
        result.download_path,
        gr.update(visible=result.download_visible),
        gr.update(value=result.notice or ""),
    )


def student_history_prepare_download(download_path):
    path = prepare_download(download_path)
    if path:
        return path
    gr.Warning("⚠️ Nenhum arquivo disponível para download.")
    return None


def _student_classes(auth: Optional[Dict[str, Any]], classrooms: Iterable[Dict[str, Any]]):
    me = _student_username(auth)
    out = []
    for c in classrooms or []:
        students = (c.get("members", {}) or {}).get("students", []) or []
        if me and me in [s.strip().lower() for s in students]:
            out.append(c)
    return out


def _render_class_details(cls_id: Optional[str], classrooms, subjects_by_class):
    c = _get_class_by_id(classrooms, cls_id)
    if not c:
        return "⚠️ Selecione uma sala."
    teachers = ", ".join(_class_member_labels(c, "teachers")) or "—"
    students = ", ".join(_class_member_labels(c, "students")) or "—"
    theme = c.get("theme_name") or c["name"]
    subs = subjects_by_class.get(cls_id, [])
    subs_txt = ", ".join([s["name"] for s in subs if s.get("active", True)]) or "—"
    return (
        f"## {c['name']}\n"
        f"- **Tema da sala:** _{theme}_\n"
        f"- 👩‍🏫 **Professores:** {teachers}\n"
        f"- 🎓 **Alunos:** {students}\n"
        f"- 🧩 **Subtemas disponíveis:** {subs_txt}\n"
    )


def _student_subtheme_choices(cls_id: Optional[str], subjects_by_class: Dict[str, Sequence[Dict[str, Any]]]):
    lst = subjects_by_class.get(cls_id, [])
    return [s["name"] for s in lst if s.get("active", True)]


def student_setup_from_class(selected_id, classrooms, subjects_by_class, current_adv):
    c = _get_class_by_id(classrooms, selected_id)
    if not c:
        return (
            gr.update(value=""),
            gr.update(choices=[], value=[]),
            gr.update(value="simples"),
            gr.update(value="detalhadas"),
            gr.update(value=""),
            gr.update(value=""),
            gr.update(visible=True),
            gr.update(visible=False),
            current_adv or {},
            "⚠️ Sala não encontrada.",
        )

    assunto = c.get("theme_name") or c.get("name") or ""
    sbj = list(subjects_by_class.get(selected_id, []))
    st_choices = [s["name"] for s in sbj if s.get("name")]
    cfg = c.get("theme_config") or {}
    script = cfg.get("script") or {}
    adv = cfg.get("adv") or {}
    new_adv = {
        "temperature": float(adv.get("temperature", 0.7)),
        "top_p": float(adv.get("top_p", 0.95)),
        "top_k": int(adv.get("top_k", 40)),
        "max_tokens": int(adv.get("max_tokens", 1024)),
    }
    return (
        gr.update(value=assunto),
        gr.update(choices=st_choices, value=[]),
        gr.update(value=script.get("estilo", "simples")),
        gr.update(value=script.get("detalhamento", "detalhadas")),
        gr.update(value=""),
        gr.update(value=""),
        gr.update(visible=True),
        gr.update(visible=False),
        new_adv,
        "✅ Parâmetros da sala aplicados.",
    )


def student_rooms_refresh(auth, classrooms, subjects_by_class):
    my = _student_classes(auth, classrooms or [])
    choices = [(c["name"], c["id"]) for c in my]
    default = choices[0][1] if choices else None
    info = (
        _render_class_details(default, classrooms, subjects_by_class or {})
        if default
        else "⚠️ Você ainda não está em nenhuma sala."
    )
    return gr.update(choices=choices, value=default), info, default


def student_on_select(cid, classrooms, subjects_by_class):
    return _render_class_details(cid, classrooms, subjects_by_class), cid


def student_go_rooms():
    return gr.update(visible=False), gr.update(visible=True)


def student_rooms_back():
    return gr.update(visible=True), gr.update(visible=False)


def student_go_setup(auth, cid, classrooms, subjects_by_class):
    c = _get_class_by_id(classrooms, cid)
    if not c:
        return (
            gr.update(visible=True),
            gr.update(visible=False),
            gr.update(value=""),
            gr.update(choices=[]),
        )
    theme = c.get("theme_name") or c["name"]
    subs = _student_subtheme_choices(cid, subjects_by_class or {})
    return (
        gr.update(visible=False),
        gr.update(visible=True),
        gr.update(value=theme),
        gr.update(choices=subs, value=subs),
    )


def _build_extras_from_classroom(theme, subthemes, interesses):
    subs_txt = ", ".join(subthemes or [])
    intr_txt = (interesses or "").strip()
    parts: List[str] = []
    if subs_txt:
        parts.append(f"Priorize os subtemas: {subs_txt}.")
    if intr_txt:
        parts.append(f"Correlacione com temas de interesse do aluno: {intr_txt}.")
    parts.append("Adote o tom motivador e pedagógico.")
    return " ".join(parts)


def student_apply_setup(
    cid,
    theme,
    estilo,
    detalhamento,
    objetivo,
    interesses,
    subthemes,
    script_state,
):
    extras = _build_extras_from_classroom(theme, subthemes, interesses)
    new_script = buildCustomScript(
        theme,
        estilo,
        detalhamento,
        objetivo,
        extras,
        subtemas=subthemes,
        interesses=interesses,
    )
    return new_script, gr.update(visible=False), gr.update(visible=True)


def _student_chat_back_to_setup():
    return gr.update(visible=True), gr.update(visible=False)


def _student_chat_enable():
    return gr.update(interactive=True)


def _sanitize_storage_segment(value):
    if value is None:
        return None
    text = str(value).strip()
    if not text:
        return None
    return text.replace("/", "-")


def student_end_chat(
    history,
    docs_state,
    auth_state,
    current_chat_id,
    chats_state,
    selected_class,
    classrooms,
    selected_theme,
    selected_subjects,
    student_goal=None,
    student_interests=None,
):
    chat_history = history if isinstance(history, list) else []
    docs = docs_state if isinstance(docs_state, dict) else {}
    chats_map = chats_state if isinstance(chats_state, dict) else {}
    active_chat_id = current_chat_id if isinstance(current_chat_id, str) else None
    storage_chat_id = active_chat_id or _mk_id("chat")
    pdf_path = None
    summary_text = ""
    classrooms_list = classrooms if isinstance(classrooms, list) else []
    selected_theme_text = (selected_theme or "") if isinstance(selected_theme, str) else ""
    if selected_theme_text:
        selected_theme_text = selected_theme_text.strip()
    normalized_subjects: List[str] = []
    if isinstance(selected_subjects, (list, tuple, set)):
        for item in selected_subjects:
            if isinstance(item, str) and item.strip():
                normalized_subjects.append(item.strip())

    goal_text = str(student_goal).strip() if isinstance(student_goal, str) else ""
    interest_text = str(student_interests).strip() if isinstance(student_interests, str) else ""
    normalized_goal_value = goal_text or "None"
    normalized_interest_value = interest_text or "None"

    def _failure(message: str, warn: bool = False):
        if warn:
            gr.Warning(message)
        else:
            gr.Error(message)
        return (
            gr.update(visible=True),
            gr.update(visible=False),
            gr.update(visible=False),
            gr.update(visible=True),
            chat_history,
            docs,
            active_chat_id,
            chats_map,
        )

    student_id = (auth_state or {}).get("user_id")
    if not student_id:
        return _failure("⚠️ Não foi possível identificar o aluno logado.", warn=True)

    is_class_chat = bool(selected_class)
    if not is_class_chat and not selected_theme_text:
        return _failure("⚠️ Informe um tema para registrar o chat independente.", warn=True)

    owner_segment = _normalize_username((auth_state or {}).get("username")) or "anon"
    class_segment = _sanitize_storage_segment(selected_class) or "sem_sala"
    student_segment = _sanitize_storage_segment(student_id) or owner_segment
    prefix_segment = _sanitize_storage_segment(SUPABASE_CHAT_STORAGE_PREFIX)
    filename = f"{storage_chat_id}_{_now_ts()}.pdf"
    path_parts = [segment for segment in (prefix_segment, class_segment, student_segment) if segment]
    storage_path = "/".join(path_parts + [filename])

    entry = chats_map.get(active_chat_id or storage_chat_id)
    created_at_ts = None
    chat_title = None
    classroom_theme = None
    if isinstance(entry, dict):
        created_at_ts = entry.get("created_at")
        chat_title = entry.get("title")
        classroom_theme = entry.get("classroom_theme")

    selected_classroom = _get_class_by_id(classrooms_list, selected_class)
    if selected_classroom and not classroom_theme:
        classroom_theme = selected_classroom.get("theme_name") or selected_classroom.get("name")

    topic_value = (classroom_theme or selected_theme_text or chat_title or "").strip()
    subject_free_text_value = "NONE" if is_class_chat else (topic_value or "Adhoc")
    if not topic_value:
        topic_value = "Indefinido"
    if not subject_free_text_value:
        subject_free_text_value = "Adhoc"

    if chat_history and VERTEX_CFG and not _vertex_err:
        try:
            summary_text = summarize_chat_history(chat_history, VERTEX_CFG, max_phrases=2)
        except Exception as exc:  # pragma: no cover - depende de chamadas externas
            summary_text = ""
            gr.Warning(f"Não foi possível gerar resumo do chat: {exc}")

    try:
        pdf_path = createChatPdf(chat_history, docs)
    except Exception as exc:  # pragma: no cover - depende de I/O
        return _failure(f"Erro ao gerar PDF do chat: {exc}")

    try:
        stored_path = upload_file_to_bucket(
            SUPABASE_URL,
            SUPABASE_SERVICE_ROLE_KEY,
            bucket=SUPABASE_CHAT_BUCKET,
            file_path=pdf_path,
            storage_path=storage_path,
            content_type="application/pdf",
            upsert=True,
        )
    except SupabaseConfigurationError:
        return _failure(
            "Configure SUPABASE_URL, SUPABASE_SERVICE_ROLE_KEY e SUPABASE_CHAT_BUCKET para enviar o PDF.",
            warn=True,
        )
    except SupabaseOperationError as exc:
        return _failure(f"Falha ao enviar PDF para o Storage: {exc}")
    except Exception as exc:  # pragma: no cover - falhas inesperadas do SDK
        return _failure(f"Erro inesperado ao enviar PDF: {exc}")
    finally:
        if pdf_path and os.path.exists(pdf_path):
            try:
                os.remove(pdf_path)
            except OSError:
                pass

    ended_ts = _now_ts()
    started_ts = created_at_ts or ended_ts

    try:
        supabase_payload = create_chat_record(
            SUPABASE_URL,
            SUPABASE_SERVICE_ROLE_KEY,
            student_id=student_id,
            classroom_id=selected_class,
            started_at=started_ts,
            ended_at=ended_ts,
            chat_history=chat_history,
            storage_chat_id=storage_chat_id,
            storage_path_id=storage_chat_id,
            storage_bucket=SUPABASE_CHAT_BUCKET,
            storage_path=stored_path,
            chat_title=chat_title,
            subject_free_text=subject_free_text_value,
            topic_source=topic_value,
            summary=summary_text or None,
            subject_titles=normalized_subjects,
            student_goal=goal_text,
            student_interest=interest_text,
            is_adhoc_chat=not is_class_chat,
            store_messages=False,
        )
    except SupabaseConfigurationError:
        return _failure(
            "Configure SUPABASE_URL e SUPABASE_SERVICE_ROLE_KEY para registrar o chat.",
            warn=True,
        )
    except SupabaseOperationError as exc:
        return _failure(f"Falha ao registrar chat no Supabase: {exc}")
    except Exception as exc:  # pragma: no cover - falhas inesperadas do SDK
        return _failure(f"Erro inesperado ao registrar chat no Supabase: {exc}")

    if not isinstance(entry, dict):
        entry = {
            "id": storage_chat_id,
            "owner": owner_segment,
            "role": _user_role(auth_state) or "aluno",
            "created_at": started_ts,
            "messages": chat_history,
        }
        chats_map[storage_chat_id] = entry
    else:
        entry["messages"] = chat_history
        if not isinstance(entry.get("created_at"), (int, float)):
            entry["created_at"] = started_ts

    if chat_title and not entry.get("title"):
        entry["title"] = chat_title

    entry["classroom_id"] = selected_class
    entry["student_id"] = student_id
    entry["started_at"] = started_ts
    entry["ended_at"] = ended_ts
    entry["storage_bucket"] = SUPABASE_CHAT_BUCKET
    entry["storage_path"] = stored_path
    entry["storage_path_id"] = storage_chat_id
    entry["topic_source"] = topic_value
    entry["subject_free_text"] = subject_free_text_value
    if classroom_theme:
        entry["classroom_theme"] = classroom_theme
    if normalized_subjects:
        entry["subjects"] = normalized_subjects
    elif not entry.get("subjects"):
        entry["subjects"] = []
    entry["student_goal"] = normalized_goal_value
    entry["student_interest"] = normalized_interest_value
    if summary_text:
        entry["summary"] = summary_text

    supabase_chat = None
    supabase_messages = None
    if isinstance(supabase_payload, dict):
        supabase_chat = supabase_payload.get("chat")
        supabase_messages = supabase_payload.get("messages")
        if not supabase_chat and supabase_payload.get("id"):
            supabase_chat = supabase_payload

    if supabase_chat:
        entry["supabase_chat_id"] = supabase_chat.get("id")
        entry["supabase_chat_record"] = supabase_chat
    if supabase_messages:
        entry["supabase_chat_messages"] = supabase_messages
    entry["supabase_synced_at"] = ended_ts

    attachments = entry.setdefault("attachments", [])
    attachments.append({"bucket": SUPABASE_CHAT_BUCKET, "path": stored_path, "type": "pdf"})

    gr.Info("Chat encerrado! O PDF foi enviado e o registro foi salvo no Supabase.")
    supabase_chat_id = entry.get("supabase_chat_id")
    supabase_tag = f" supabase_id='{supabase_chat_id}'" if supabase_chat_id else ""
    print(
        "[CHAT] PDF enviado para Storage -> bucket='%s' path='%s' chat='%s'%s"
        % (SUPABASE_CHAT_BUCKET, stored_path, storage_chat_id, supabase_tag)
    )

    return (
        gr.update(visible=False),
        gr.update(visible=True),
        gr.update(visible=True),
        gr.update(visible=False),
        [],
        {},
        None,
        chats_map,
    )


def _render_progress_md(chats_map: Optional[Dict[str, Any]], user_filter: Optional[str] = None) -> str:
    if not chats_map:
        return "⚠️ Nenhum chat registrado ainda."

    normalized_filter = _normalize_username(user_filter)
    entries: List[Tuple[str, Dict[str, Any]]] = []
    for cid, chat in (chats_map or {}).items():
        owner_login = _normalize_username(chat.get("owner"))
        student_login = _normalize_username(chat.get("student_id"))
        if normalized_filter and normalized_filter not in (owner_login, student_login):
            continue
        entries.append((cid, chat))

    if not entries:
        if user_filter:
            return f"⚠️ Nenhum chat encontrado para o usuário `{user_filter}`."
        return "⚠️ Nenhum chat registrado para o filtro aplicado."

    entries.sort(key=lambda item: item[1].get("ended_at") or item[1].get("created_at") or 0, reverse=True)
    total = len(entries)
    scored = sum(1 for _, chat in entries if chat.get("score") is not None)
    last_ts = entries[0][1].get("ended_at") or entries[0][1].get("created_at") or 0

    lines = ["### Relatório de Progresso"]
    if user_filter:
        lines.append(f"- Filtro por aluno: `{user_filter}`")
    lines.append(f"- Total de chats registrados: **{total}**")
    lines.append(f"- Chats avaliados: **{scored}**")
    if last_ts:
        lines.append(
            "- Última atividade: %s"
            % time.strftime("%d/%m/%Y %H:%M", time.localtime(last_ts))
        )

    by_class: Dict[str, List[Dict[str, Any]]] = {}
    for _, chat in entries:
        class_id = chat.get("classroom_id") or "fora_da_sala"
        by_class.setdefault(class_id, []).append(chat)

    lines.append("\n#### Chats por sala")
    for class_id, items in sorted(by_class.items(), key=lambda kv: kv[0] or ""):
        theme = items[0].get("classroom_theme") or items[0].get("topic_source") or "Indefinido"
        lines.append(f"- `{class_id or 'fora_da_sala'}` — {theme}: {len(items)} chat(s)")

    lines.append("\n#### Últimas conversas")
    for cid, chat in entries[:5]:
        owner = chat.get("owner") or chat.get("student_id") or "?"
        ended = chat.get("ended_at") or chat.get("created_at") or 0
        ts_label = time.strftime("%d/%m %H:%M", time.localtime(ended)) if ended else "?"
        score = chat.get("score")
        score_txt = f" — nota: {score}" if score is not None else ""
        subject = chat.get("topic_source") or chat.get("subject_free_text") or "Adhoc"
        lines.append(f"- `{cid}` — {ts_label} — aluno: `{owner}` — tema: _{subject}_{score_txt}")

    return "\n".join(lines)


def build_student_views(
    *,
    blocks: gr.Blocks,
    auth_state: gr.State,
    classrooms_state: gr.State,
    subjects_state: gr.State,
    docs_state: gr.State,
    script_state: gr.State,
    adv_state: gr.State,
    current_chat_id: gr.State,
    chats_state: gr.State,
    student_selected_class: gr.State,
) -> StudentViews:
    student_history_state = gr.State([])
    student_history_selected = gr.State(None)
    student_history_transcript = gr.State("")
    student_download_path = gr.State(None)

    with gr.Column(visible=False) as viewStudentRooms:
        gr.Markdown("## 🎒 Minhas Salas")
        with gr.Row():
            stRoomSelect = gr.Dropdown(choices=[], label="Selecione uma sala", value=None)
            stRoomsRefresh = gr.Button("🔄")
        stRoomInfo = gr.Markdown("")
        with gr.Row():
            stEnterRoomChatSetup = gr.Button("💬 Entrar no chat da sala", variant="primary")
            stRoomsBack = gr.Button("← Voltar à Home")
        with gr.Accordion("Histórico de Chats", open=False):
            with gr.Row():
                stHistoryClass = gr.Dropdown(choices=[], label="Sala", value=None)
                stHistoryRefresh = gr.Button("🔄 Atualizar histórico")
            stHistoryInfo = gr.Markdown("Selecione uma sala ou atualize para ver seus chats.")
            stHistoryTable = gr.Dataframe(
                headers=list(HISTORY_TABLE_HEADERS),
                datatype=["str"] * 6,
                interactive=False,
                wrap=True,
            )
            with gr.Row():
                stHistoryChat = gr.Dropdown(choices=[], label="Chat registrado", value=None)
                stHistoryLoad = gr.Button("📄 Ver detalhes")
            stHistoryMetadata = gr.Markdown(
                "ℹ️ Selecione um chat para visualizar os detalhes.",
                elem_classes=["history-box"],
            )
            gr.Markdown("#### Resumo da IA")
            stHistorySummary = gr.Markdown(
                "ℹ️ Selecione um chat para visualizar o resumo.",
                elem_classes=["history-box"],
            )
            gr.Markdown("#### Prévia do PDF")
            stHistoryPreview = gr.Markdown(
                "ℹ️ Carregue um chat para visualizar a prévia.",
                elem_classes=["history-box"],
            )
            with gr.Row():
                stHistoryDownload = gr.DownloadButton(
                    "⬇️ Baixar PDF", visible=False, variant="secondary"
                )
            stHistoryEvaluation = gr.Textbox(
                label="Avaliação automática (professores)",
                lines=4,
                interactive=False,
                value="",
            )
            stHistoryComments = gr.Markdown(
                "ℹ️ Nenhum comentário registrado ainda.",
                elem_classes=["history-box"],
            )
            stHistoryNotice = gr.Markdown("")

    with gr.Column(visible=False) as viewStudentSetup:
        gr.Markdown("## 🧩 Configurar Chat da Sala")
        with gr.Row():
            stSetupBackRooms = gr.Button("← Voltar às minhas salas")
        with gr.Row():
            with gr.Column(scale=1) as stCfgCol:
                stAssunto = gr.Textbox(label="1) Assunto (tema da sala)", interactive=False)
                stSubthemes = gr.CheckboxGroup(choices=[], label="2) Subtemas")
                stEstilo = gr.Radio(choices=["técnicas", "simples"], value="simples", label="3) Estilo de linguagem")
                stDetalhamento = gr.Radio(
                    choices=["detalhadas", "curtas"], value="detalhadas", label="4) Nível de detalhamento"
                )
                stObjetivo = gr.Textbox(
                    label="5) Objetivo",
                    placeholder="Ex: Revisar para prova de estruturas de dados",
                )
                stInteresses = gr.Textbox(
                    label="6) Temas de interesse",
                    placeholder="Ex: futebol, games, música",
                )
                gr.Markdown("_Correlacione com um assunto que você conhece para aprender melhor!_")
                stSubmit = gr.Button("Aplicar Configuração da Sala", variant="primary", interactive=True)
            with gr.Column(scale=2, visible=False) as stChatCol:
                stProvider = gr.Markdown("**Chat da Sala** — usa seu tema, subtemas e interesses.")
                stChatbot = gr.Chatbot(label="Chat (Sala)", type="messages", height=420)
                with gr.Row():
                    stClear = gr.Button("Limpar chat")
                with gr.Row():
                    stBackToSetup = gr.Button("⬅️ Voltar para configuração da sala")
                    stEndChat = gr.Button("Encerrar Chat", variant="stop")
                allow_file_upload = bool(VERTEX_CFG and not _vertex_err)
                chat_placeholder = (
                    "Digite sua mensagem ou envie um PDF..."
                    if allow_file_upload
                    else "Digite sua mensagem..."
                )
                chat_sources = ["upload"] if allow_file_upload else []

                stChatInput = gr.MultimodalTextbox(
                    show_label=False,
                    placeholder=chat_placeholder,
                    sources=chat_sources,
                    interactive=True,
                )

    stRoomsRefresh.click(
        student_rooms_refresh,
        inputs=[auth_state, classrooms_state, subjects_state],
        outputs=[stRoomSelect, stRoomInfo, student_selected_class],
    ).then(
        student_history_dropdown,
        inputs=[auth_state, classrooms_state, stHistoryClass],
        outputs=stHistoryClass,
    )

    stRoomSelect.change(
        student_on_select,
        inputs=[stRoomSelect, classrooms_state, subjects_state],
        outputs=[stRoomInfo, student_selected_class],
    )

    setup_evt = stEnterRoomChatSetup.click(
        student_setup_from_class,
        inputs=[stRoomSelect, classrooms_state, subjects_state, adv_state],
        outputs=[
            stAssunto,
            stSubthemes,
            stEstilo,
            stDetalhamento,
            stObjetivo,
            stInteresses,
            stCfgCol,
            stChatCol,
            adv_state,
            stProvider,
        ],
    )

    setup_evt.then(
        student_go_setup,
        inputs=[auth_state, student_selected_class, classrooms_state, subjects_state],
        outputs=[viewStudentRooms, viewStudentSetup, stAssunto, stSubthemes],
    )

    stSubmit.click(
        student_apply_setup,
        inputs=[
            student_selected_class,
            stAssunto,
            stEstilo,
            stDetalhamento,
            stObjetivo,
            stInteresses,
            stSubthemes,
            script_state,
        ],
        outputs=[script_state, stCfgCol, stChatCol],
    )

    stChatInput.submit(
        addMessage,
        inputs=[stChatbot, stChatInput, docs_state, auth_state, current_chat_id, chats_state],
        outputs=[stChatbot, stChatInput, docs_state, current_chat_id, chats_state],
    ).then(
        bot,
        inputs=[stChatbot, docs_state, script_state, adv_state, current_chat_id, chats_state],
        outputs=stChatbot,
    ).then(_student_chat_enable, outputs=stChatInput)

    stClear.click(clearChat, outputs=stChatbot)

    stEndChat.click(
        student_end_chat,
        inputs=[
            stChatbot,
            docs_state,
            auth_state,
            current_chat_id,
            chats_state,
            student_selected_class,
            classrooms_state,
            stAssunto,
            stSubthemes,
            stObjetivo,
            stInteresses,
        ],
        outputs=[
            viewStudentSetup,
            viewStudentRooms,
            stCfgCol,
            stChatCol,
            stChatbot,
            docs_state,
            current_chat_id,
            chats_state,
        ],
    ).then(
        student_rooms_refresh,
        inputs=[auth_state, classrooms_state, subjects_state],
        outputs=[stRoomSelect, stRoomInfo, student_selected_class],
    ).then(
        student_history_dropdown,
        inputs=[auth_state, classrooms_state, stHistoryClass],
        outputs=stHistoryClass,
    ).then(
        student_history_refresh,
        inputs=[auth_state, stHistoryClass],
        outputs=[
            stHistoryTable,
            student_history_state,
            stHistoryChat,
            stHistoryInfo,
            student_history_selected,
        ],
    ).then(
        student_history_load_chat,
        inputs=[student_history_selected, student_history_state, student_download_path],
        outputs=[
            student_history_selected,
            stHistoryMetadata,
            stHistorySummary,
            stHistoryPreview,
            stHistoryEvaluation,
            stHistoryComments,
            student_history_transcript,
            student_download_path,
            stHistoryDownload,
            stHistoryNotice,
        ],
    )

    stBackToSetup.click(_student_chat_back_to_setup, outputs=[stCfgCol, stChatCol])
    stSetupBackRooms.click(
        lambda: (gr.update(visible=False), gr.update(visible=True)),
        inputs=None,
        outputs=[viewStudentSetup, viewStudentRooms],
    )

    stHistoryRefresh.click(
        student_history_refresh,
        inputs=[auth_state, stHistoryClass],
        outputs=[
            stHistoryTable,
            student_history_state,
            stHistoryChat,
            stHistoryInfo,
            student_history_selected,
        ],
    ).then(
        student_history_load_chat,
        inputs=[student_history_selected, student_history_state, student_download_path],
        outputs=[
            student_history_selected,
            stHistoryMetadata,
            stHistorySummary,
            stHistoryPreview,
            stHistoryEvaluation,
            stHistoryComments,
            student_history_transcript,
            student_download_path,
            stHistoryDownload,
            stHistoryNotice,
        ],
    )

    stHistoryClass.change(
        student_history_refresh,
        inputs=[auth_state, stHistoryClass],
        outputs=[
            stHistoryTable,
            student_history_state,
            stHistoryChat,
            stHistoryInfo,
            student_history_selected,
        ],
    ).then(
        student_history_load_chat,
        inputs=[student_history_selected, student_history_state, student_download_path],
        outputs=[
            student_history_selected,
            stHistoryMetadata,
            stHistorySummary,
            stHistoryPreview,
            stHistoryEvaluation,
            stHistoryComments,
            student_history_transcript,
            student_download_path,
            stHistoryDownload,
            stHistoryNotice,
        ],
    )

    stHistoryChat.change(
        student_history_load_chat,
        inputs=[stHistoryChat, student_history_state, student_download_path],
        outputs=[
            student_history_selected,
            stHistoryMetadata,
            stHistorySummary,
            stHistoryPreview,
            stHistoryEvaluation,
            stHistoryComments,
            student_history_transcript,
            student_download_path,
            stHistoryDownload,
            stHistoryNotice,
        ],
    )

    stHistoryLoad.click(
        student_history_load_chat,
        inputs=[stHistoryChat, student_history_state, student_download_path],
        outputs=[
            student_history_selected,
            stHistoryMetadata,
            stHistoryPreview,
            stHistoryEvaluation,
            stHistoryComments,
            student_history_transcript,
            student_download_path,
            stHistoryDownload,
            stHistoryNotice,
        ],
    )

    stHistoryDownload.click(
        student_history_prepare_download,
        inputs=[student_download_path],
        outputs=stHistoryDownload,
    )

    return StudentViews(
        rooms_view=viewStudentRooms,
        setup_view=viewStudentSetup,
        rooms_dropdown=stRoomSelect,
        rooms_refresh=stRoomsRefresh,
        rooms_info=stRoomInfo,
        enter_setup_button=stEnterRoomChatSetup,
        rooms_back_button=stRoomsBack,
        config_column=stCfgCol,
        chat_column=stChatCol,
        assunto=stAssunto,
        subthemes=stSubthemes,
        estilo=stEstilo,
        detalhamento=stDetalhamento,
        objetivo=stObjetivo,
        interesses=stInteresses,
        submit_button=stSubmit,
        provider_markdown=stProvider,
        chatbot=stChatbot,
        clear_button=stClear,
        back_to_setup_button=stBackToSetup,
        end_chat_button=stEndChat,
        chat_input=stChatInput,
        setup_back_button=stSetupBackRooms,
        history_class_dropdown=stHistoryClass,
        history_refresh_button=stHistoryRefresh,
        history_info=stHistoryInfo,
        history_table=stHistoryTable,
        history_chat_dropdown=stHistoryChat,
        history_load_button=stHistoryLoad,
        history_metadata=stHistoryMetadata,
        history_summary=stHistorySummary,
        history_preview=stHistoryPreview,
        history_download_button=stHistoryDownload,
        history_evaluation=stHistoryEvaluation,
        history_comments=stHistoryComments,
        history_notice=stHistoryNotice,
    )


__all__ = [
    "StudentViews",
    "build_student_views",
    "student_rooms_refresh",
    "student_on_select",
    "student_go_rooms",
    "student_rooms_back",
    "student_go_setup",
    "student_apply_setup",
    "student_end_chat",
    "student_setup_from_class",
    "student_history_dropdown",
    "student_history_refresh",
    "student_history_load_chat",
    "student_history_prepare_download",
    "_student_chat_back_to_setup",
    "_student_chat_enable",
    "_render_progress_md",
]
