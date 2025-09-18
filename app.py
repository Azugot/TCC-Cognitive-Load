# app.py
import os
import uuid
import time
import gradio as gr

from services.vertex_client import VERTEX_CFG, _vertex_err, _streamFromVertex
from services.auth_store import _hashPw
from services.docs import extractPdfText, createChatPdf
from services.script_builder import buildCustomScript
from services.supabase_client import (
    SupabaseConfigurationError,
    SupabaseOperationError,
    SupabaseUserExistsError,
    create_classroom_record,
    create_user_record,
    create_subject_record,
    delete_classroom_record,
    fetch_classroom_domain,
    fetch_user_record,
    fetch_users_by_role,
    remove_classroom_student,
    remove_classroom_teacher,
    set_classroom_theme_config,
    update_classroom_record,
    update_subject_active,
    upload_file_to_bucket,
    upsert_classroom_student,
    upsert_classroom_teacher,
)

# ======================== Configuração Supabase ========================

# Substitua os valores abaixo pelas credenciais reais do projeto Supabase.
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

# ======================== Utilidades ========================


def _now_ts():
    return int(time.time())


def _mk_id(prefix="id"):
    return f"{prefix}_{str(uuid.uuid4())[:8]}"


def _normalize_username(value):
    return (value or "").strip().lower()


def _user_role(auth):
    return str((auth or {}).get("role", "")).strip().lower()


def _is_admin(auth):
    return _user_role(auth) == "admin"


def _is_teacher(auth):
    return _user_role(auth) == "professor"


def _auth_user_id(auth):
    return (auth or {}).get("user_id")


def _merge_notice(text: str, notice: str) -> str:
    if not notice:
        return text
    base = text or ""
    if not base:
        return notice
    return f"{notice}\n\n{base}"

# ======================== Chat: entrada e saída ========================


def addMessage(history, message, docsState, authState, currentChatId, chatsState):
    """Recebe msg do usuário, anexa em 'history' e registra no chatsState (in-memory) para Histórico/Avaliação/Progresso."""
    if not isinstance(docsState, dict):
        docsState = {}
    if not isinstance(chatsState, dict):
        chatsState = {}

    # cria sessão se não existir
    chat_id = currentChatId or _mk_id("chat")
    if chat_id not in chatsState:
        user = (authState or {}).get("username") or "anon"
        role = (authState or {}).get("role", "aluno").lower()
        chatsState[chat_id] = {
            "id": chat_id,
            "owner": user,
            "role": role,
            "created_at": _now_ts(),
            "title": f"Chat de {user}",
            "messages": [],
            "summary": "",
            "score": None,
            "rubric": None,
            "feedback": None,
        }

    files = message.get("files") if isinstance(message, dict) else []
    text = message.get("text") if isinstance(message, dict) else None

    for filePath in files or []:
        pathLow = (filePath or "").lower()
        if pathLow.endswith(".pdf"):
            try:
                textPdf = extractPdfText(filePath)
                name = os.path.basename(filePath)
                docsState[name] = textPdf
                part = f"Documento ativo: {name} ✅"
                history.append({"role": "user", "content": part})
                chatsState[chat_id]["messages"].append(
                    {"role": "user", "content": part, "ts": _now_ts()})
            except Exception as e:
                part = f"Falha ao ler PDF: {os.path.basename(filePath)} ({e})"
                history.append({"role": "user", "content": part})
                chatsState[chat_id]["messages"].append(
                    {"role": "user", "content": part, "ts": _now_ts()})
        else:
            part = f"Arquivo não suportado: {os.path.basename(filePath)}"
            history.append({"role": "user", "content": part})
            chatsState[chat_id]["messages"].append(
                {"role": "user", "content": part, "ts": _now_ts()})

    if text:
        history.append({"role": "user", "content": text})
        chatsState[chat_id]["messages"].append(
            {"role": "user", "content": text, "ts": _now_ts()})

    return (
        history,
        gr.MultimodalTextbox(value=None, interactive=False),
        docsState,
        chat_id,
        chatsState
    )


def bot(history, docsState, scriptState, advState, currentChatId, chatsState):
    """Stream da resposta do modelo + gravação em chatsState."""
    docsPreview = ""
    if isinstance(docsState, dict) and docsState:
        joined = []
        for name, content in docsState.items():
            preview = content[:2000]
            joined.append(f"Documento '{name}':\n{preview}")
        docsPreview = "\n\n".join(joined)

    systemMsg = {"role": "system",
                 "content": f"{scriptState}\n\nDocumentos ativos (amostras):\n{docsPreview}"}
    messages = [systemMsg] + history
    history.append({"role": "assistant", "content": ""})

    try:
        adv = advState if isinstance(advState, dict) else {}
        for delta in _streamFromVertex(messages, VERTEX_CFG, adv):
            history[-1]["content"] += delta
            yield history
        # salva no log do chat
        if currentChatId and isinstance(chatsState, dict) and currentChatId in chatsState:
            chatsState[currentChatId]["messages"].append(
                {"role": "assistant",
                    "content": history[-1]["content"], "ts": _now_ts()}
            )
            if not chatsState[currentChatId].get("title"):
                first_user_msg = next(
                    (m.get("content") for m in history if m.get("role") == "user"), "")
                if first_user_msg:
                    chatsState[currentChatId]["title"] = first_user_msg[:40]
    except Exception as e:
        history[-1]["content"] = f"[ERRO Vertex: {e}]"
        if currentChatId and isinstance(chatsState, dict) and currentChatId in chatsState:
            chatsState[currentChatId]["messages"].append(
                {"role": "assistant",
                    "content": history[-1]["content"], "ts": _now_ts()}
            )
        yield history


def clearChat():
    return []

# ======================== Script builder (pessoal) ========================


def updateCustomScript(assunto, estilo, detalhamento, objetivo, extras, scriptState):
    # Agora passamos parâmetros estruturados (subtemas/interesses) como None por compatibilidade
    return buildCustomScript(
        assunto, estilo, detalhamento, objetivo, extras,
        subtemas=None, interesses=None
    )


def updateAdvOptions(temperature, topP, topK, maxTokens, advState):
    try:
        new_state = {
            "temperature": float(temperature),
            "top_p": float(topP),
            "top_k": int(topK),
            "max_tokens": int(maxTokens),
        }
    except Exception:
        new_state = advState if isinstance(advState, dict) else {}
    return new_state


def validateFields(assunto, estilo, detalhamento, objetivo):
    ok = all([assunto and assunto.strip(), estilo,
             detalhamento, objetivo and objetivo.strip()])
    return gr.Button(interactive=bool(ok))

# ====================== Helpers de renderização / listas ======================


def _render_classrooms_md(classrooms):
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


def _render_history_md(chats_map, owner=None):
    if not chats_map:
        return "⚠️ Ainda não há conversas."
    rows = []
    for cid, chat in chats_map.items():
        if owner and chat.get("owner") != owner:
            continue
        ts = chat.get("created_at")
        score = chat.get("score")
        title = chat.get("title") or cid
        tag = f" (nota: {score})" if score is not None else ""
        rows.append(
            f"- **{title}** — id: `{cid}` — autor: `{chat.get('owner')}` — {time.strftime('%d/%m %H:%M', time.localtime(ts))}{tag}"
        )
    return "### Conversas registradas\n" + ("\n".join(rows) if rows else "⚠️ Nenhuma conversa para o filtro aplicado.")


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

# ========= Helpers de professor/aluno =========


def _teacher_username(auth):
    return _normalize_username((auth or {}).get("username"))


def _teacher_classes(auth, classrooms):
    me = _teacher_username(auth)
    out = []
    for c in (classrooms or []):
        teachers = (c.get("members", {}) or {}).get("teachers", []) or []
        if me and me in [t.strip().lower() for t in teachers]:
            out.append(c)
    return out


def _teacher_choices(auth, classrooms):
    my = _teacher_classes(auth, classrooms)
    return [(c["name"], c["id"]) for c in my]


def _render_teacher_members_md(cls_id, classrooms):
    c = next((x for x in (classrooms or []) if x["id"] == cls_id), None)
    if not c:
        return "⚠️ Selecione uma de suas salas."
    s = c["members"]["students"]
    return f"### Alunos da sala `{c['name']}`\n- 🎓 {len(s)} aluno(s): " + (', '.join(s) if s else "—")


def _subjects_choices_teacher(auth, classrooms, selected_id, subjects_by_class):
    dd = gr.update(choices=_teacher_choices(auth, classrooms),
                   value=selected_id if selected_id else None)
    if not selected_id:
        return dd, gr.update(choices=[], value=[]), "ℹ️ Selecione uma sala para gerenciar subtemas."
    lst = subjects_by_class.get(selected_id, [])
    all_names = [s["name"] for s in lst]
    active = [s["name"] for s in lst if s.get("active")]
    return dd, gr.update(choices=all_names, value=active), _render_subjects_md(subjects_by_class, selected_id, classrooms or [])


def _student_username(auth):
    return _normalize_username((auth or {}).get("username"))


def _student_classes(auth, classrooms):
    me = _student_username(auth)
    out = []
    for c in (classrooms or []):
        students = (c.get("members", {}) or {}).get("students", []) or []
        if me and me in [s.strip().lower() for s in students]:
            out.append(c)
    return out


def _render_class_details(cls_id, classrooms, subjects_by_class):
    c = next((x for x in (classrooms or []) if x["id"] == cls_id), None)
    if not c:
        return "⚠️ Selecione uma sala."
    teachers = ", ".join(c["members"]["teachers"]) or "—"
    students = ", ".join(c["members"]["students"]) or "—"
    theme = c.get("theme_name") or c["name"]
    subs = subjects_by_class.get(cls_id, [])
    subs_txt = ", ".join([s["name"]
                         for s in subs if s.get("active", True)]) or "—"
    return (
        f"## {c['name']}\n"
        f"- **Tema da sala:** _{theme}_\n"
        f"- 👩‍🏫 **Professores:** {teachers}\n"
        f"- 🎓 **Alunos:** {students}\n"
        f"- 🧩 **Subtemas disponíveis:** {subs_txt}\n"
    )


def _student_subtheme_choices(cls_id, subjects_by_class):
    lst = subjects_by_class.get(cls_id, [])
    return [s["name"] for s in lst if s.get("active", True)]


# ======================== UI helpers & callbacks ========================

# ----- Domain state sync -----
def _load_domain_state(current_classrooms=None, current_subjects=None):
    try:
        raw_classrooms, raw_subjects = fetch_classroom_domain(
            SUPABASE_URL,
            SUPABASE_SERVICE_ROLE_KEY,
            users_table=SUPABASE_USERS_TABLE,
        )
    except SupabaseConfigurationError:
        warn = (
            "⚠️ Configure SUPABASE_URL e SUPABASE_SERVICE_ROLE_KEY para gerenciar as salas."
        )
        return current_classrooms or [], current_subjects or {}, warn
    except SupabaseOperationError as err:
        warn = f"❌ Erro ao consultar salas: {err}"
        return current_classrooms or [], current_subjects or {}, warn

    normalized_classrooms = []
    for item in raw_classrooms:
        teachers = {
            _normalize_username(entry.get("login"))
            for entry in item.get("teachers", [])
            if entry.get("login")
        }
        students = {
            _normalize_username(entry.get("login"))
            for entry in item.get("students", [])
            if entry.get("login")
            and str(entry.get("status", "active")).lower() == "active"
        }
        owner_login = _normalize_username(item.get("owner_login"))
        if owner_login:
            teachers.add(owner_login)
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
                    "teachers": sorted(t for t in teachers if t),
                    "students": sorted(students),
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
        normalized_subjects[cid].sort(
            key=lambda item: (item.get("name") or "").lower()
        )

    return normalized_classrooms, normalized_subjects, ""

def _refresh_states(current_classrooms=None, current_subjects=None):
    classrooms, subjects, notice = _load_domain_state(
        current_classrooms, current_subjects
    )
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

def _teacher_classrooms_outputs(auth, classrooms, notice=""):
    relevant = _teacher_classes(auth, classrooms or [])
    md = _render_classrooms_md(relevant)
    md = _merge_notice(md, notice)
    dd = gr.update(choices=_teacher_choices(auth, classrooms or []))
    return md, dd

def student_setup_from_class(selected_id, classrooms, subjects_by_class, current_adv):
    c = _get_class_by_id(classrooms, selected_id)
    if not c:
        return (
            gr.update(value=""),  # stAssunto
            gr.update(choices=[], value=[]),  # stSubthemes
            gr.update(value="simples"), gr.update(value="detalhadas"),
            gr.update(value=""), gr.update(
                value=""),  # objetivo, interesses
            gr.update(visible=True), gr.update(
                visible=False),  # stCfgCol, stChatCol
            current_adv or {}, "⚠️ Sala não encontrada."
        )
    assunto = c.get("theme_name") or c.get("name") or ""
    # subtemas dessa sala:
    sbj = list(subjects_by_class.get(selected_id, []))
    st_choices = [s["name"] for s in sbj if s.get("name")]
    # configs da sala:
    cfg = c.get("theme_config") or {}
    script = cfg.get("script") or {}
    adv = cfg.get("adv") or {}
    # configura as opções avançadas para o chat da sala
    new_adv = {
        "temperature": float(adv.get("temperature", 0.7)),
        "top_p": float(adv.get("top_p", 0.95)),
        "top_k": int(adv.get("top_k", 40)),
        "max_tokens": int(adv.get("max_tokens", 1024)),
    }
    # retorna updates para a UI de setup + troca advState e mostra coluna do chat
    return (
        gr.update(value=assunto),
        gr.update(choices=st_choices, value=[]),
        gr.update(value=script.get("estilo", "simples")),
        gr.update(value=script.get("detalhamento", "detalhadas")),
        gr.update(value=""),  # objetivo livre do aluno
        gr.update(value=""),  # interesses livres do aluno
        gr.update(visible=True), gr.update(visible=False),
        new_adv, "✅ Parâmetros da sala aplicados."
    )

# ----- Auth & navigation -----
def _route_home(auth):
    is_auth = bool(auth and auth.get("isAuth")
                   is True and auth.get("username"))
    user = (auth or {}).get("username") or ""
    role = (auth or {}).get("role", "aluno")
    print(
        f"[NAV] _route_home: isAuth={is_auth} user='{user}' role='{role}'")

    if not is_auth:
        return (
            gr.update(
                value="### 👋 Bem-vindo! Faça login para continuar.", visible=True),
            gr.update(visible=True),   # viewLogin
            gr.update(visible=False),  # viewHome
            gr.update(visible=False),  # viewHomeAdmin
            gr.update(value="")        # homeGreet
        )
    role = str(role).lower()
    header_txt = f"### 👋 Olá, **{user}**! (perfil: {role})"
    if role == "admin":
        return (
            gr.update(value=header_txt, visible=True),
            gr.update(visible=False),
            gr.update(visible=False),
            gr.update(visible=True),
            gr.update(value=f"## 🧭 Home do Admin — bem-vindo, **{user}**")
        )
    else:
        return (
            gr.update(value=header_txt, visible=True),
            gr.update(visible=False),
            gr.update(visible=True),
            gr.update(visible=False),
            gr.update(value=f"## 🏠 Home — bem-vindo, **{user}**")
        )

def _teacherUi(auth):
    role = (auth or {}).get("role", "aluno")
    is_prof = (str(role).lower() == "professor")
    return gr.update(visible=is_prof), gr.update(value="")

def _studentUi(auth):
    role = (auth or {}).get("role", "aluno")
    is_student_or_admin = str(role).lower() in ("aluno", "admin")
    return gr.update(visible=is_student_or_admin)

def _back_home(auth):
    role = (auth or {}).get("role", "aluno")
    if str(role).lower() == "admin":
        return (
            gr.update(visible=False),
            gr.update(visible=True),
            gr.update(visible=False),
        )
    return (
        gr.update(visible=False),
        gr.update(visible=False),
        gr.update(visible=True),
    )

def switch_auth_mode(mode):
    is_register = str(mode or "").strip().lower() == "registrar"
    return (
        gr.update(visible=is_register),
        gr.update(visible=is_register),
        gr.update(visible=not is_register),
        gr.update(visible=is_register),
        gr.update(value=""),
    )

def doRegister(username, password, email, full_name, role, authState):
    raw_username = (username or "").strip()
    raw_email = (email or "").strip()
    login_email = raw_email.lower()
    name = (full_name or "").strip()
    pw = (password or "").strip()
    print(
        f"[AUTH] doRegister: username='{raw_username.lower()}' email='{login_email}' role='{role}'"
    )
    if not raw_username or not login_email or not name or not pw:
        return gr.update(value="⚠️ Informe usuário, e-mail, nome e senha."), authState

    role_pt = (role or "aluno").strip().lower() or "aluno"
    supabase_role = ROLE_PT_TO_DB.get(role_pt, "student")

    display_name = name or raw_username or login_email

    try:
        created = create_user_record(
            SUPABASE_URL,
            SUPABASE_SERVICE_ROLE_KEY,
            SUPABASE_USERS_TABLE,
            login=login_email,
            password_hash=_hashPw(pw),
            role=supabase_role,
            display_name=display_name,
        )
        print(f"[AUTH] doRegister: Supabase created -> {created}")
    except SupabaseConfigurationError:
        warn = (
            "⚠️ Configure SUPABASE_URL e SUPABASE_SERVICE_ROLE_KEY antes de registrar usuários."
        )
        print("[AUTH] doRegister: configuração Supabase ausente")
        return gr.update(value=warn), authState
    except SupabaseUserExistsError:
        print(
            f"[AUTH] doRegister: usuário já existe no Supabase -> {login_email}"
        )
        return gr.update(value="⚠️ Usuário já existe."), authState
    except SupabaseOperationError as err:
        print(f"[AUTH] doRegister: erro Supabase -> {err}")
        return gr.update(value=f"❌ Erro ao registrar usuário: {err}"), authState
    except Exception as exc:  # pragma: no cover - caminhos não determinísticos
        print(f"[AUTH] doRegister: erro inesperado -> {exc}")
        return gr.update(value=f"❌ Erro inesperado ao registrar usuário: {exc}"), authState

    mapped_role = ROLE_DB_TO_PT.get(
        (created.role or supabase_role or "student").strip().lower(),
        role_pt,
    )
    auth_username = login_email or created.email or raw_username.lower()
    authState = {
        "isAuth": True,
        "username": auth_username,
        "role": mapped_role,
        "display_name": display_name,
        "user_id": created.id,
    }
    print(f"[AUTH] doRegister: registrado e logado -> {authState}")
    return gr.update(
        value=(
            f"✅ Registrado e logado como **{display_name}** "
            f"(`{auth_username}`) (perfil: {mapped_role})."
        )
    ), authState

def doLogin(username, password, authState):
    raw_username = (username or "").strip()
    uname = raw_username.lower()
    pw = (password or "").strip()
    print(f"[AUTH] doLogin: uname='{uname}'")
    if not uname or not pw:
        return gr.update(value="⚠️ Informe usuário e senha."), authState

    try:
        entry = fetch_user_record(
            SUPABASE_URL,
            SUPABASE_SERVICE_ROLE_KEY,
            SUPABASE_USERS_TABLE,
            uname,
        )
    except SupabaseConfigurationError:
        warn = (
            "⚠️ Configure SUPABASE_URL e SUPABASE_SERVICE_ROLE_KEY antes de realizar o login."
        )
        print("[AUTH] doLogin: configuração Supabase ausente")
        return gr.update(value=warn), authState
    except SupabaseOperationError as err:
        print(f"[AUTH] doLogin: erro Supabase -> {err}")
        return gr.update(value=f"❌ Erro ao consultar usuário: {err}"), authState
    except Exception as exc:  # pragma: no cover - caminhos não determinísticos
        print(f"[AUTH] doLogin: erro inesperado -> {exc}")
        return gr.update(value=f"❌ Erro inesperado: {exc}"), authState

    if not entry:
        print(f"[AUTH] doLogin: usuário não encontrado -> {uname}")
        return gr.update(value="❌ Usuário ou senha incorretos."), authState

    expected_hash = entry.password_hash or ""
    if expected_hash != _hashPw(pw):
        print(f"[AUTH] doLogin: senha incorreta -> {uname}")
        return gr.update(value="❌ Usuário ou senha incorretos."), authState

    mapped_role = ROLE_DB_TO_PT.get(
        (entry.role or "student").strip().lower(),
        "aluno",
    )
    authState = {
        "isAuth": True,
        "username": uname,
        "role": mapped_role,
        "user_id": entry.id,
        "display_name": entry.name or entry.email,
    }
    print(f"[AUTH] doLogin: sucesso -> {authState}")
    return gr.update(
        value=f"✅ Bem-vindo, **{uname}** (perfil: {mapped_role})."
    ), authState

def _doLogout():
    print("[AUTH] logout")
    return (
        {"isAuth": False, "username": None},
        gr.update(
            value="### 👋 Bem-vindo! Faça login para continuar.", visible=True),
        gr.update(visible=True),   # viewLogin
        gr.update(visible=False),  # viewHome
        gr.update(visible=False),  # viewHomeAdmin
        gr.update(visible=False),  # viewStudio
        gr.update(visible=False),  # viewClassrooms
        gr.update(visible=False),  # viewHistory
        gr.update(visible=False),  # viewEvaluate
        gr.update(visible=False),  # viewProgress
        gr.update(visible=False),  # viewAdminPg
    )

def listStudents(auth):
    role = (auth or {}).get("role", "aluno")
    if str(role).lower() not in ("professor", "admin"):
        return "⚠️ Apenas professores/admin podem visualizar a lista de alunos."
    try:
        records = fetch_users_by_role(
            SUPABASE_URL,
            SUPABASE_SERVICE_ROLE_KEY,
            SUPABASE_USERS_TABLE,
            ROLE_PT_TO_DB.get("aluno", "student"),
        )
    except SupabaseConfigurationError:
        return (
            "⚠️ Configure SUPABASE_URL e SUPABASE_SERVICE_ROLE_KEY para visualizar os alunos."
        )
    except SupabaseOperationError as err:
        print(f"[AUTH] listStudents: erro Supabase -> {err}")
        return f"❌ Erro ao consultar alunos: {err}"
    except Exception as exc:  # pragma: no cover - caminhos não determinísticos
        print(f"[AUTH] listStudents: erro inesperado -> {exc}")
        return f"❌ Erro inesperado ao consultar alunos: {exc}"

    students = []
    for record in records:
        label = record.name or record.email or record.id
        if label:
            students.append(label)

    if not students:
        return "Nenhum aluno cadastrado ainda."
    students.sort(key=lambda x: x.lower())
    bullet = "\n".join([f"- {s}" for s in students])
    return f"### Alunos cadastrados ({len(students)})\n\n{bullet}"

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

# ----- Admin: classrooms -----
def _refresh_cls_dropdown(classrooms):
    choices = [(c["name"], c["id"]) for c in (classrooms or [])]
    return gr.update(choices=choices), gr.update(choices=choices)

def add_classroom(name, theme, desc, locked, classrooms, subjects, auth):
    role = _user_role(auth)
    if role not in ("admin", "professor"):
        md, dd1, dd2 = _admin_classrooms_outputs(
            classrooms, "⛔ Apenas professores ou admins podem criar salas."
        )
        return classrooms, subjects, md, dd1, dd2

    creator_id = _auth_user_id(auth)
    if not creator_id:
        md, dd1, dd2 = _admin_classrooms_outputs(
            classrooms, "⚠️ Faça login para criar salas."
        )
        return classrooms, subjects, md, dd1, dd2

    name = (name or "").strip()
    theme = (theme or "").strip() or name
    description = (desc or "").strip() or ""
    if not name:
        md, dd1, dd2 = _admin_classrooms_outputs(
            classrooms, "⚠️ Informe um nome para a sala."
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
            "⚠️ Configure SUPABASE_URL e SUPABASE_SERVICE_ROLE_KEY para cadastrar salas.",
        )
        return classrooms, subjects, md, dd1, dd2
    except SupabaseOperationError as err:
        md, dd1, dd2 = _admin_classrooms_outputs(
            classrooms, f"❌ Erro ao criar sala: {err}"
        )
        return classrooms, subjects, md, dd1, dd2

    classroom_id = (created or {}).get("id")
    if role == "professor" and classroom_id and creator_id:
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
    md, dd1, dd2 = _admin_classrooms_outputs(
        classes, notice or "✅ Sala criada."
    )
    return classes, subjects_map, md, dd1, dd2

def refresh_classrooms(classrooms, subjects):
    classes, subjects_map, notice = _refresh_states(classrooms, subjects)
    md, dd1, dd2 = _admin_classrooms_outputs(classes, notice)
    return classes, subjects_map, md, dd1, dd2

def load_cls_for_edit(cls_id, classrooms):
    c = next((x for x in (classrooms or []) if x["id"] == cls_id), None)
    if not c:
        return gr.update(value=""), gr.update(value=""), gr.update(value=""), gr.update(value=True), gr.update(value=False), "⚠️ Sala não encontrada."
    return gr.update(value=c["name"]), gr.update(value=c["theme_name"]), gr.update(value=c["description"]), gr.update(value=c["theme_locked"]), gr.update(value=c["is_archived"]), ""

def save_cls(cls_id, name, theme, desc, locked, archived, classrooms, subjects):
    if not cls_id:
        md = _merge_notice(
            _render_classrooms_md(classrooms or []), "⚠️ Selecione uma sala."
        )
        return classrooms, subjects, md

    original = next((c for c in (classrooms or []) if c.get("id") == cls_id), None)
    if not original:
        md = _merge_notice(
            _render_classrooms_md(classrooms or []), "⚠️ Sala não encontrada."
        )
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
    md = _merge_notice(
        _render_classrooms_md(classes), notice or "✅ Sala atualizada."
    )
    return classes, subjects_map, md

def delete_cls(cls_id, classrooms, subjects):
    if not cls_id:
        md = _merge_notice(
            _render_classrooms_md(classrooms or []), "⚠️ Selecione uma sala."
        )
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
            "⚠️ Configure SUPABASE_URL e SUPABASE_SERVICE_ROLE_KEY para remover salas.",
        )
        return classrooms, subjects, md
    except SupabaseOperationError as err:
        md = _merge_notice(
            _render_classrooms_md(classrooms or []),
            f"❌ Erro ao remover sala: {err}",
        )
        return classrooms, subjects, md

    classes, subjects_map, notice = _refresh_states(classrooms, subjects)
    md = _merge_notice(
        _render_classrooms_md(classes), notice or "✅ Sala removida."
    )
    return classes, subjects_map, md

# ----- Admin: membership -----
def _render_members_md(cls_id, classrooms):
    c = next((x for x in (classrooms or []) if x["id"] == cls_id), None)
    if not c:
        return "⚠️ Selecione uma sala."
    t = c["members"]["teachers"]
    s = c["members"]["students"]
    lines = [f"### Membros da sala `{c['name']}`", f"- 👩‍🏫 Professores ({len(t)}): " + (', '.join(t) if t else "—"),
             f"- 🎓 Alunos ({len(s)}): " + (', '.join(s) if s else "—")]
    return "\n".join(lines)

def add_teacher(cls_id, uname, classrooms, subjects, auth):
    uname_norm = _normalize_username(uname)
    if not cls_id or not uname_norm:
        return classrooms, subjects, "⚠️ Informe sala e username."
    if not _is_admin(auth):
        return (
            classrooms,
            subjects,
            "⛔ Apenas administradores podem gerenciar professores por aqui.",
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

    classroom = next((c for c in (classrooms or []) if c.get("id") == cls_id), None)
    role_label = None
    if classroom and not _normalize_username(classroom.get("owner")):
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
    md = _merge_notice(_render_members_md(cls_id, classes), notice or "✅ Professor adicionado.")
    return classes, subjects_map, md

def add_student(cls_id, uname, classrooms, subjects):
    uname_norm = _normalize_username(uname)
    if not cls_id or not uname_norm:
        return classrooms, subjects, "⚠️ Informe sala e username."

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
    md = _merge_notice(
        _render_members_md(cls_id, classes), notice or "✅ Aluno adicionado."
    )
    return classes, subjects_map, md

def remove_member(cls_id, uname, classrooms, subjects):
    uname_norm = _normalize_username(uname)
    if not cls_id or not uname_norm:
        return classrooms, subjects, "⚠️ Informe sala e username."

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
            "⚠️ Configure SUPABASE_URL e SUPABASE_SERVICE_ROLE_KEY para gerenciar integrantes.",
        )
    except SupabaseOperationError as err:
        return classrooms, subjects, f"❌ Erro ao buscar usuário: {err}"

    if not record or not record.id:
        return classrooms, subjects, "⚠️ Usuário não encontrado."

    status_messages = []
    try:
        remove_classroom_teacher(
            SUPABASE_URL,
            SUPABASE_SERVICE_ROLE_KEY,
            classroom_id=cls_id,
            teacher_id=record.id,
        )
    except SupabaseConfigurationError:
        return (
            classrooms,
            subjects,
            "⚠️ Configure SUPABASE_URL e SUPABASE_SERVICE_ROLE_KEY para gerenciar integrantes.",
        )
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
        return (
            classrooms,
            subjects,
            "⚠️ Configure SUPABASE_URL e SUPABASE_SERVICE_ROLE_KEY para gerenciar integrantes.",
        )
    except SupabaseOperationError as err:
        status_messages.append(f"Aluno: {err}")

    classes, subjects_map, notice = _refresh_states(classrooms, subjects)
    base_md = _render_members_md(cls_id, classes)
    message = (
        "⚠️ " + "; ".join(status_messages)
        if status_messages
        else "✅ Usuário removido."
    )
    result = _merge_notice(base_md, message)
    result = _merge_notice(result, notice)
    return classes, subjects_map, result

# ----- Teacher helpers -----
def _get_class_by_id(classrooms, cls_id):
    return next((x for x in (classrooms or []) if x.get("id") == cls_id), None)

def teacher_load_params(cls_id, classrooms):
    c = _get_class_by_id(classrooms, cls_id)
    if not c:
        return (gr.update(value=0.7), gr.update(value=0.95), gr.update(value=40),
                gr.update(value=1024), gr.update(value="simples"),
                gr.update(value="detalhadas"), gr.update(value=""), "⚠️ Sala não encontrada.")
    cfg = (c.get("theme_config") or {})
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
        "✅ Parâmetros carregados."
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
    me = _teacher_username(auth)
    c = _get_class_by_id(classrooms, cls_id)
    if not c:
        return classrooms, subjects, "⚠️ Sala não encontrada."
    teachers = [t.strip().lower() for t in c["members"]["teachers"]]
    if me not in teachers and not _is_admin(auth):
        return classrooms, subjects, "⛔ Você não é professor desta sala."

    cfg = {
        "adv": {
            "temperature": float(temp),
            "top_p": float(top_p),
            "top_k": int(top_k),
            "max_tokens": int(max_t),
        },
        "script": {
            "estilo": (estilo or "simples"),
            "detalhamento": (detalhamento or "detalhadas"),
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

# ----- Teacher membership & subthemes -----
def teacher_add_teacher(cls_id, uname, classrooms, subjects, auth):
    me = _teacher_username(auth)
    uname_norm = _normalize_username(uname)
    if not cls_id or not uname_norm:
        return classrooms, subjects, "⚠️ Informe sala e username."
    if not me and not _is_admin(auth):
        return classrooms, subjects, "⚠️ Faça login."

    classroom = _get_class_by_id(classrooms, cls_id)
    if not classroom:
        return classrooms, subjects, "⚠️ Sala não encontrada."

    normalized = [_normalize_username(t) for t in classroom["members"]["teachers"]]
    if me not in normalized and not _is_admin(auth):
        return classrooms, subjects, "⛔ Você não é professor desta sala."

    owner = _normalize_username(classroom.get("owner"))
    if owner and me != owner and not _is_admin(auth):
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
        return (
            classrooms,
            subjects,
            "⚠️ Configure SUPABASE_URL e SUPABASE_SERVICE_ROLE_KEY para gerenciar professores.",
        )
    except SupabaseOperationError as err:
        return classrooms, subjects, f"❌ Erro ao adicionar professor: {err}"

    classes, subjects_map, notice = _refresh_states(classrooms, subjects)
    md = _merge_notice(
        _render_teacher_members_md(cls_id, classes), notice or "✅ Professor adicionado."
    )
    return classes, subjects_map, md

def teacher_add_classroom(name, theme, desc, locked, classrooms, subjects, auth):
    name = (name or "").strip()
    theme = (theme or "").strip() or name
    me = _teacher_username(auth)
    role = _user_role(auth)

    if role not in ("professor", "admin"):
        md, dd = _teacher_classrooms_outputs(
            auth, classrooms, "⛔ Apenas professores ou admins podem criar salas."
        )
        return classrooms, subjects, md, dd, dd, "⛔ Apenas professores ou admins podem criar salas."
    if not me and not _is_admin(auth):
        md, dd = _teacher_classrooms_outputs(
            auth, classrooms, "⚠️ Faça login."
        )
        return classrooms, subjects, md, dd, dd, "⚠️ Faça login."
    if not name:
        md, dd = _teacher_classrooms_outputs(
            auth, classrooms, "⚠️ Informe um nome para a sala."
        )
        return classrooms, subjects, md, dd, dd, "⚠️ Informe um nome para a sala."

    creator_id = _auth_user_id(auth)
    if not creator_id and not _is_admin(auth):
        md, dd = _teacher_classrooms_outputs(
            auth, classrooms, "⚠️ Não foi possível identificar o usuário logado."
        )
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
            auth, classrooms,
            "⚠️ Configure SUPABASE_URL e SUPABASE_SERVICE_ROLE_KEY para cadastrar salas.",
        )
        return classrooms, subjects, md, dd, dd, "⚠️ Configure SUPABASE_URL e SUPABASE_SERVICE_ROLE_KEY para cadastrar salas."
    except SupabaseOperationError as err:
        md, dd = _teacher_classrooms_outputs(
            auth, classrooms, f"❌ Erro ao criar sala: {err}"
        )
        return classrooms, subjects, md, dd, dd, f"❌ Erro ao criar sala: {err}"

    classroom_id = (created or {}).get("id")
    if _is_teacher(auth) and me and creator_id and classroom_id:
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
    me = _teacher_username(auth)
    uname_norm = _normalize_username(uname)
    if not cls_id or not uname_norm:
        return classrooms, subjects, "⚠️ Informe sala e username."

    classroom = _get_class_by_id(classrooms, cls_id)
    if not classroom:
        return classrooms, subjects, "⚠️ Sala não encontrada."

    teachers = [t.strip().lower() for t in classroom["members"]["teachers"]]
    if me not in teachers and not _is_admin(auth):
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
    md = _merge_notice(
        _render_teacher_members_md(cls_id, classes), notice or "✅ Aluno adicionado."
    )
    return classes, subjects_map, md

def teacher_rm_user(cls_id, uname, classrooms, subjects, auth):
    me = _teacher_username(auth)
    uname_norm = _normalize_username(uname)
    if not cls_id or not uname_norm:
        return classrooms, subjects, "⚠️ Informe sala e username."

    classroom = _get_class_by_id(classrooms, cls_id)
    if not classroom:
        return classrooms, subjects, "⚠️ Sala não encontrada."

    teachers = [t.strip().lower() for t in classroom["members"]["teachers"]]
    if me not in teachers and not _is_admin(auth):
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
    md = _merge_notice(
        _render_teacher_members_md(cls_id, classes), notice or "✅ Aluno removido."
    )
    return classes, subjects_map, md

def teacher_subjects_refresh(auth, classrooms, selected_id, subjects_by_class):
    return _subjects_choices_teacher(auth, classrooms, selected_id, subjects_by_class)

def teacher_add_subject(auth, selected_id, subj, subjects_by_class, classrooms):
    me = _teacher_username(auth)
    if not selected_id:
        return classrooms, subjects_by_class, gr.update(), gr.update(), "ℹ️ Selecione uma sala."
    classroom = _get_class_by_id(classrooms, selected_id)
    if not classroom:
        return classrooms, subjects_by_class, gr.update(), gr.update(), "⚠️ Sala não encontrada."
    if me not in [t.strip().lower() for t in classroom["members"]["teachers"]] and not _is_admin(auth):
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
    me = _teacher_username(auth)
    if me not in [t.strip().lower() for t in classroom["members"]["teachers"]] and not _is_admin(auth):
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

def _teacher_on_select(auth, classrooms, selected_id, subjects_by_class):
    md_members = _render_teacher_members_md(selected_id, classrooms)
    dd, chk, md_subjects = _subjects_choices_teacher(
        auth, classrooms, selected_id, subjects_by_class)
    return md_members, dd, chk, md_subjects

# ----- Admin: subthemes -----
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
    chk, md = _admin_subjects_ui(
        classes,
        subjects_map,
        cls_id,
        notice or "✅ Subtema adicionado.",
    )
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
    chk, md = _admin_subjects_ui(
        classes,
        subjects_map,
        cls_id,
        notice or "✅ Subtemas atualizados.",
    )
    return classes, subjects_map, chk, md

# ----- Evaluation & reporting -----
def refresh_history(chats_map, mine_only, auth):
    user = (auth or {}).get("username")
    return _render_history_md(chats_map, owner=user if mine_only else None)
def eval_refresh_dropdown(chats_map):
    ids = []
    for cid, chat in (chats_map or {}).items():
        if any(m for m in chat["messages"] if m["role"] == "user"):
            ids.append((chat.get("title") or cid, cid))
    default_val = ids[0][1] if ids else None
    return gr.update(choices=ids, value=default_val)

def eval_load(chat_id, chats_map=None):
    print(
        f"[EVAL] eval_load: chat_id={chat_id!r} has_map={bool(chats_map)}")
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
    user = (auth or {}).get("username")
    return _render_progress_md(chats_map, user_filter=user if mine_only else None)

# ----- Student flows -----
def student_rooms_refresh(auth, classrooms, subjects_by_class):
    my = _student_classes(auth, classrooms or [])
    choices = [(c["name"], c["id"]) for c in my]
    default = choices[0][1] if choices else None
    info = _render_class_details(
        default, classrooms, subjects_by_class or {}) if default else "⚠️ Você ainda não está em nenhuma sala."
    return gr.update(choices=choices, value=default), info, default

def student_on_select(cid, classrooms, subjects_by_class):
    return _render_class_details(cid, classrooms, subjects_by_class), cid

def student_go_rooms():
    return gr.update(visible=False), gr.update(visible=True)

def student_rooms_back():
    return gr.update(visible=True), gr.update(visible=False)

def student_go_setup(auth, cid, classrooms, subjects_by_class):
    c = next((x for x in (classrooms or []) if x["id"] == cid), None)
    if not c:
        return gr.update(visible=True), gr.update(visible=False), gr.update(value=""), gr.update(choices=[])
    theme = c.get("theme_name") or c["name"]
    subs = _student_subtheme_choices(cid, subjects_by_class or {})
    return gr.update(visible=False), gr.update(visible=True), gr.update(value=theme), gr.update(choices=subs, value=subs)

def _build_extras_from_classroom(theme, subthemes, interesses):
    # Preferimos manter "extras" enxuto; os subtemas e interesses também vão como variáveis para o builder
    subs_txt = ", ".join(subthemes or [])
    intr_txt = (interesses or "").strip()
    parts = []
    if subs_txt:
        parts.append(f"Priorize os subtemas: {subs_txt}.")
    if intr_txt:
        parts.append(
            f"Correlacione com temas de interesse do aluno: {intr_txt}.")
    parts.append("Adote o tom motivador e pedagógico.")
    return " ".join(parts)

def student_apply_setup(cid, theme, estilo, detalhamento, objetivo, interesses, subthemes, scriptState):
    # Monta extras (texto) e chama builder com variáveis estruturadas
    extras = _build_extras_from_classroom(theme, subthemes, interesses)
    new_script = buildCustomScript(
        theme, estilo, detalhamento, objetivo, extras,
        subtemas=subthemes, interesses=interesses
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


def student_end_chat(history, docsState, authState, currentChatId, chatsState, selectedClass):
    chat_history = history if isinstance(history, list) else []
    docs = docsState if isinstance(docsState, dict) else {}
    chats_map = chatsState if isinstance(chatsState, dict) else {}
    active_chat_id = currentChatId if isinstance(currentChatId, str) else None
    storage_chat_id = active_chat_id or _mk_id("chat")
    pdf_path = None

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

    try:
        pdf_path = createChatPdf(chat_history, docs)
    except Exception as exc:  # pragma: no cover - depende de I/O
        return _failure(f"Erro ao gerar PDF do chat: {exc}")

    student_id = (authState or {}).get("user_id")
    owner_segment = _normalize_username((authState or {}).get("username")) or "anon"
    class_segment = _sanitize_storage_segment(selectedClass) or "sem_sala"
    student_segment = _sanitize_storage_segment(student_id) or owner_segment
    prefix_segment = _sanitize_storage_segment(SUPABASE_CHAT_STORAGE_PREFIX)
    filename = f"{storage_chat_id}_{_now_ts()}.pdf"
    path_parts = [segment for segment in (prefix_segment, class_segment, student_segment) if segment]
    storage_path = "/".join(path_parts + [filename])

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

    entry = chats_map.get(storage_chat_id)
    if not entry:
        entry = {
            "id": storage_chat_id,
            "owner": owner_segment,
            "role": _user_role(authState) or "aluno",
            "created_at": _now_ts(),
            "messages": chat_history,
        }
        chats_map[storage_chat_id] = entry

    entry["classroom_id"] = selectedClass
    entry["ended_at"] = _now_ts()
    entry["storage_bucket"] = SUPABASE_CHAT_BUCKET
    entry["storage_path"] = stored_path
    attachments = entry.setdefault("attachments", [])
    attachments.append(
        {
            "bucket": SUPABASE_CHAT_BUCKET,
            "path": stored_path,
            "type": "pdf",
        }
    )

    gr.Info("Chat encerrado! O PDF foi enviado para o Supabase.")
    print(
        f"[CHAT] PDF enviado para Storage -> bucket='{SUPABASE_CHAT_BUCKET}' path='{stored_path}' chat='{storage_chat_id}'"
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


# ================================== APP / UI ==================================
with gr.Blocks(theme=gr.themes.Default(), fill_height=True) as demo:
    # Estados principais
    authState = gr.State({"isAuth": False, "username": None})
    docsState = gr.State({})
    scriptState = gr.State(
        "Você é um assistente pedagógico. Aguarde configuração do usuário.")
    advState = gr.State({"temperature": 0.7, "top_p": 0.95,
                        "top_k": 40, "max_tokens": 1024})

    # Estados de domínio (in-memory)
    # [{id, name, description, theme_name, theme_locked, is_archived, members:{teachers:[],students:[]}}]
    classroomsState = gr.State([])
    # {class_id: [{"name":str, "active":bool}, ...]}
    subjectsState = gr.State({})
    chatsState = gr.State({})          # {chat_id: {...}}
    currentChatId = gr.State(None)     # chat_id atual
    adminNavState = gr.State({"page": "home"})
    studentSelectedClass = gr.State(None)   # id da sala escolhida pelo aluno

    # Header
    header_msg = "### 👋 Bem-vindo! Faça login para continuar."
    if _vertex_err:
        header_msg += f"\n\n> **Atenção**: {_vertex_err}"
    else:
        header_msg += (
            f"\n\n> ✅ Credenciais Vertex carregadas de: `{(VERTEX_CFG or {}).get('source_path', '?')}`"
            f" | Projeto: `{(VERTEX_CFG or {}).get('project', '?')}` | Região: `{(VERTEX_CFG or {}).get('location', '?')}`"
            f" | Modelo: `{(VERTEX_CFG or {}).get('model', '?')}`"
        )
    header = gr.Markdown(header_msg, elem_id="hdr")

    # ===== Login =====
    with gr.Column(visible=True) as viewLogin:
        gr.Markdown("## 🔐 Login / Registro")
        authMode = gr.Radio(
            ["Login", "Registrar"], value="Login", label="Modo de acesso")
        with gr.Row():
            username = gr.Textbox(label="Usuário", placeholder="ex: augusto")
            password = gr.Textbox(
                label="Senha", type="password", placeholder="••••••••")
        with gr.Row(visible=False) as registerRow:
            email = gr.Textbox(
                label="E-mail", placeholder="ex: nome@dominio.com")
            fullName = gr.Textbox(
                label="Nome completo", placeholder="ex: Maria Silva")
        with gr.Row(visible=False) as registerRoleRow:
            roleRadio = gr.Radio(
                choices=["Aluno", "Professor", "Admin"], label="Perfil", value="Aluno")
        with gr.Row():
            btnLogin = gr.Button("Entrar", variant="primary", visible=True)
            btnRegister = gr.Button("Registrar", visible=False)
        loginMsg = gr.Markdown("")

    # ===== Home (Aluno/Professor) =====
    with gr.Column(visible=False) as viewHome:
        homeGreet = gr.Markdown("## 🏠 Home")
        gr.Markdown("Escolha uma opção para continuar:")
        with gr.Row():
            btnGoCustomize = gr.Button(
                "⚙️ Personalizar o Chat", variant="primary")
            btnLogout1 = gr.Button("Sair")
        # visível só para aluno/admin
        with gr.Row(visible=False) as studentRow:
            btnStudentRooms = gr.Button("🎒 Minhas Salas", variant="secondary")
        # visível só para professor
        with gr.Row(visible=False) as profRow:
            btnViewStudents = gr.Button(
                "👥 Ver alunos cadastrados", variant="secondary")
            btnTeacherClassrooms = gr.Button(
                "🏫 Minhas Salas", variant="primary")
        studentsOut = gr.Markdown("")

    # ===== Página do Professor (salas + subtemas) =====
    with gr.Column(visible=False) as viewTeacher:
        gr.Markdown("## 🏫 Gerenciar Salas")
        teacherNotice = gr.Markdown("")
        with gr.Group():
            with gr.Row():
                tClsName = gr.Textbox(
                    label="Nome da sala", placeholder="Ex.: Algoritmos e ED")
                tClsTheme = gr.Textbox(
                    label="Tema (exibição)", placeholder="Ex.: Algoritmos")
            tClsDesc = gr.Textbox(label="Descrição (opcional)")
            with gr.Row():
                tClsLocked = gr.Checkbox(value=True, label="Tema travado")
                btnTeacherAddClass = gr.Button(
                    "➕ Criar sala", variant="primary")
        with gr.Accordion("Selecionar sala", open=False):
            with gr.Row():
                tSelectClass = gr.Dropdown(
                    choices=[], label="Minhas salas", value=None)
                btnTeacherRefresh = gr.Button("🔄")
        with gr.Accordion("Membros (Professores/Alunos)", open=False):
            with gr.Row():
                tAddTeacher = gr.Textbox(
                    label="Adicionar professor (username)")
                btnTeacherAddTeacher = gr.Button("👩‍🏫 Adicionar")
            tTeachersMd = gr.Markdown("")  # opcional: mensagem/eco
            with gr.Row():
                tAddStudent = gr.Textbox(label="Adicionar aluno (username)")
                btnTeacherAddStudent = gr.Button("🎓 Adicionar")
            with gr.Row():
                tRmUser = gr.Textbox(label="Remover usuário (username)")
                btnTeacherRmUser = gr.Button("🗑️ Remover")
            tMembersMd = gr.Markdown("")
        with gr.Accordion("Subtemas da sala", open=False):
            with gr.Row():
                tSubjName = gr.Textbox(
                    label="Novo subtema", placeholder="Ex.: Ponteiros")
                btnTeacherAddSubj = gr.Button("➕ Adicionar subtema")
            with gr.Row():
                tActiveList = gr.CheckboxGroup(
                    choices=[], label="Ativar/desativar subtemas", value=[])
                btnTeacherApplyActive = gr.Button("✅ Aplicar ativações")
            tSubjectsMd = gr.Markdown("")
        with gr.Accordion("Parâmetros do Chat da Sala", open=False):
            with gr.Row():
                tTemp = gr.Slider(0.0, 1.5, value=0.7,
                                  step=0.05, label="temperature")
                tTopP = gr.Slider(0.0, 1.0, value=0.95,
                                  step=0.01, label="top_p")
            with gr.Row():
                tTopK = gr.Slider(1, 100, value=40, step=1, label="top_k")
                tMaxT = gr.Slider(128, 4096, value=1024,
                                  step=64, label="max_tokens")
            with gr.Row():
                tEstilo = gr.Radio(
                    choices=["técnicas", "simples"], value="simples", label="Estilo de linguagem")
                tDetalhamento = gr.Radio(
                    choices=["detalhadas", "curtas"], value="detalhadas", label="Nível de detalhamento")
            tExtras = gr.Textbox(label="Preferências adicionais (extras)",
                                 placeholder="Ex.: evitar jargões, passo-a-passo sempre, etc.")
            with gr.Row():
                btnTeacherSaveParams = gr.Button(
                    "💾 Salvar parâmetros da sala", variant="primary")
                btnTeacherLoadParams = gr.Button(
                    "🔄 Carregar da sala selecionada")
            tParamsMsg = gr.Markdown("")
        tClassroomsMd = gr.Markdown("")
        with gr.Row():
            btnTeacherBack = gr.Button("← Voltar à Home")

    # ===== Home (Admin) + Nav =====
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
                btnAdminAsStudent = gr.Button(
                    "⚙️ Abrir Personalização do Chat (Aluno)")
            with gr.Column():
                gr.Markdown("### 👩‍🏫 Área do Professor (demonstração)")
                btnAdminListStudents = gr.Button("👥 Ver alunos cadastrados")

    # ===== Página: Administração (placeholder) =====
    with gr.Column(visible=False) as viewAdminPg:
        gr.Markdown("## 🛠️ Administração (Placeholder)")
        gr.Markdown(
            "- Gerenciar usuários/roles (futuro)\n"
            "- Parâmetros globais do sistema (futuro)\n"
            "- Logs/telemetria (futuro)\n"
        )
        with gr.Row():
            adminPgBack = gr.Button("← Voltar à Home do Admin")

    # ===== PÁGINA: Gerenciar Salas (Admin) =====
    with gr.Column(visible=False) as viewClassrooms:
        gr.Markdown("## 🏫 Gerenciar Salas")
        with gr.Group():
            with gr.Row():
                clsName = gr.Textbox(
                    label="Nome da sala", placeholder="Ex.: Algoritmos e Estruturas de Dados")
                clsTheme = gr.Textbox(
                    label="Tema (exibição)", placeholder="Ex.: Algoritmos e ED")
            clsDesc = gr.Textbox(label="Descrição (opcional)")
            with gr.Row():
                clsLocked = gr.Checkbox(value=True, label="Tema travado")
                btnAddClass = gr.Button("➕ Criar sala", variant="primary")
        with gr.Accordion("Editar/Arquivar/Excluir", open=False):
            with gr.Row():
                clsSelect = gr.Dropdown(
                    choices=[], label="Selecione a sala", value=None)
                btnRefreshCls = gr.Button("🔄")
            with gr.Row():
                eName = gr.Textbox(label="Nome")
                eTheme = gr.Textbox(label="Tema")
            eDesc = gr.Textbox(label="Descrição")
            with gr.Row():
                eLocked = gr.Checkbox(value=True, label="Tema travado")
                eArchived = gr.Checkbox(value=False, label="Arquivada")
            with gr.Row():
                btnSaveCls = gr.Button(
                    "💾 Salvar alterações", variant="primary")
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
                clsSubjName = gr.Textbox(
                    label="Novo subtema", placeholder="Ex.: Ponteiros")
                btnClsAddSubj = gr.Button("➕ Adicionar subtema")
            with gr.Row():
                clsActiveList = gr.CheckboxGroup(
                    choices=[], label="Ativar/desativar subtemas", value=[])
                btnClsApplyActive = gr.Button("✅ Aplicar ativações")
            clsSubjectsMd = gr.Markdown("")
        classroomsMd = gr.Markdown("")
        with gr.Row():
            clsBackAdminHome = gr.Button("← Voltar à Home do Admin")
    # ===== PÁGINA: Histórico =====
    with gr.Column(visible=False) as viewHistory:
        gr.Markdown("## 🗂️ Histórico de Chats")
        with gr.Row():
            histMineOnly = gr.Checkbox(
                value=False, label="Mostrar apenas meus chats")
            btnHistoryRefresh = gr.Button("🔄 Atualizar")
        historyMd = gr.Markdown("")
        with gr.Row():
            histBack = gr.Button("← Voltar à Home do Admin")

    # ===== PÁGINA: Avaliação =====
    with gr.Column(visible=False) as viewEvaluate:
        gr.Markdown("## 📝 Avaliar Chats")
        with gr.Row():
            evalChatId = gr.Dropdown(
                choices=[], label="Chat para avaliar", value=None)
            btnEvalRefresh = gr.Button("🔄")
        evalCurrent = gr.Markdown("")
        with gr.Row():
            evalScore = gr.Slider(0, 10, value=8, step=1, label="Nota")
            evalRubric = gr.Textbox(
                label="Rubrica (curta)", placeholder="Ex.: Clareza, Correção, Raciocínio")
        evalFeedback = gr.Textbox(
            label="Feedback para o aluno", placeholder="Escreva um comentário objetivo")
        btnSaveEval = gr.Button("💾 Salvar avaliação", variant="primary")
        with gr.Row():
            evalBack = gr.Button("← Voltar à Home do Admin")

    # ===== PÁGINA: Progresso =====
    with gr.Column(visible=False) as viewProgress:
        gr.Markdown("## 📊 Progresso e Relatórios")
        with gr.Row():
            progMineOnly = gr.Checkbox(
                value=False, label="Restringir aos meus chats")
            btnProgRefresh = gr.Button("🔄 Atualizar")
        progressMd = gr.Markdown("")
        with gr.Row():
            progBack = gr.Button("← Voltar à Home do Admin")

    # ===== PÁGINA: Studio (chat pessoal) =====
    with gr.Column(visible=False) as viewStudio:
        with gr.Row():
            gr.Markdown("## 🧩 Customização do Chat")
            btnBackHome = gr.Button("← Voltar à Home")
        with gr.Row():
            with gr.Column(scale=1) as configCol:
                assunto = gr.Textbox(label="1) Assunto",
                                     placeholder="Ex: Biologia marinha")
                estilo = gr.Radio(label="2) Estilo de linguagem", choices=[
                                  "técnicas", "simples"], value="simples")
                detalhamento = gr.Radio(label="3) Nível de detalhamento", choices=[
                                        "detalhadas", "curtas"], value="detalhadas")
                objetivo = gr.Textbox(label="4) Objetivo",
                                      placeholder="Ex: Preparar para uma prova")
                extras = gr.Textbox(label="5) Preferências extras (opcional)",
                                    placeholder="Ex: Evitar termos em inglês")
                with gr.Tabs():
                    with gr.Tab("Opções avançadas"):
                        temperature = gr.Slider(
                            0.0, 2.0, value=0.7, step=0.1, label="Temperatura")
                        topP = gr.Slider(0.0, 1.0, value=0.95,
                                         step=0.01, label="Top-p")
                        topK = gr.Slider(
                            0, 50, value=40, step=1, label="Top-k")
                        maxTokens = gr.Slider(
                            64, 4096, value=1024, step=32, label="Max tokens (saída)")
                        applyAdv = gr.Button("Aplicar opções avançadas")
                        applyAdv.click(updateAdvOptions, inputs=[
                                       temperature, topP, topK, maxTokens, advState], outputs=advState)
                submitBtn = gr.Button(
                    "Aplicar Configuração", variant="primary", interactive=False)
                reconfigureBtn = gr.Button("Reconfigurar")
            with gr.Column(scale=2, visible=False) as chatCol:
                providerBadge = gr.Markdown(
                    f"**Provedor:** `vertex`  |  **Projeto:** `{(VERTEX_CFG or {}).get('project', '?')}`"
                    f"  |  **Modelo:** `{(VERTEX_CFG or {}).get('model', '?')}`"
                )
                chatbot = gr.Chatbot(label="Chat", type="messages", height=420)
                with gr.Row():
                    clearBtn = gr.Button("Limpar chat")
                    exportBtn = gr.Button("Exportar conversa (PDF)")
                backToConfigBtn = gr.Button("⬅️ Voltar para customização")
                chatInput = gr.MultimodalTextbox(
                    show_label=False, placeholder="Digite sua mensagem ou envie um PDF...", sources=["upload"], interactive=True
                )

        for c in (assunto, estilo, detalhamento, objetivo):
            c.change(validateFields, inputs=[
                     assunto, estilo, detalhamento, objetivo], outputs=submitBtn)

        submitBtn.click(
            updateCustomScript,
            inputs=[assunto, estilo, detalhamento,
                    objetivo, extras, scriptState],
            outputs=scriptState
        ).then(
            lambda: (gr.update(visible=False), gr.update(visible=True)),
            inputs=None,
            outputs=[configCol, chatCol]
        )

        reconfigureBtn.click(
            lambda: (gr.update(visible=True), gr.update(visible=False), []),
            inputs=None, outputs=[configCol, chatCol, chatbot]
        )
        backToConfigBtn.click(lambda: (gr.update(visible=True), gr.update(visible=False)),
                              inputs=None, outputs=[configCol, chatCol])

        chatMsg = chatInput.submit(
            addMessage,
            inputs=[chatbot, chatInput, docsState,
                    authState, currentChatId, chatsState],
            outputs=[chatbot, chatInput, docsState, currentChatId, chatsState]
        )
        botMsg = chatMsg.then(
            bot,
            inputs=[chatbot, docsState, scriptState,
                    advState, currentChatId, chatsState],
            outputs=chatbot
        )
        botMsg.then(lambda: gr.update(interactive=True), outputs=chatInput)

        clearBtn.click(clearChat, outputs=chatbot)
        exportBtn.click(createChatPdf, inputs=[
                        chatbot, docsState], outputs=gr.File())

        btnBackHome.click(_back_home, inputs=authState, outputs=[
                          viewStudio, viewHomeAdmin, viewHome])

    # ===== PÁGINA: Aluno → Minhas Salas =====
    with gr.Column(visible=False) as viewStudentRooms:
        gr.Markdown("## 🎒 Minhas Salas")
        with gr.Row():
            stRoomSelect = gr.Dropdown(
                choices=[], label="Selecione uma sala", value=None)
            stRoomsRefresh = gr.Button("🔄")
        stRoomInfo = gr.Markdown("")
        with gr.Row():
            stEnterRoomChatSetup = gr.Button(
                "💬 Entrar no chat da sala", variant="primary")
            stRoomsBack = gr.Button("← Voltar à Home")

    # ===== PÁGINA: Aluno → Setup do Chat da Sala =====
    with gr.Column(visible=False) as viewStudentSetup:
        stSetupTitle = gr.Markdown("## 🧩 Configurar Chat da Sala")
        with gr.Row():
            stSetupBackRooms = gr.Button("← Voltar às minhas salas")
        with gr.Row():
            with gr.Column(scale=1) as stCfgCol:
                stAssunto = gr.Textbox(
                    label="1) Assunto (tema da sala)", interactive=False)
                stSubthemes = gr.CheckboxGroup(choices=[], label="2) Subtemas")
                stEstilo = gr.Radio(label="3) Estilo de linguagem", choices=[
                                    "técnicas", "simples"], value="simples")
                stDetalhamento = gr.Radio(label="4) Nível de detalhamento", choices=[
                                          "detalhadas", "curtas"], value="detalhadas")
                stObjetivo = gr.Textbox(
                    label="5) Objetivo", placeholder="Ex: Revisar para prova de estruturas de dados")
                stInteresses = gr.Textbox(
                    label="6) Temas de interesse", placeholder="Ex: futebol, games, música")
                gr.Markdown(
                    "_Aprenda melhor e de forma mais intuitiva correlacionando com um assunto que você conhece!_")
                stSubmit = gr.Button(
                    "Aplicar Configuração da Sala", variant="primary", interactive=True)
            with gr.Column(scale=2, visible=False) as stChatCol:
                stProvider = gr.Markdown(
                    "**Chat da Sala** — usa seu tema, subtemas e interesses.")
                stChatbot = gr.Chatbot(
                    label="Chat (Sala)", type="messages", height=420)
                with gr.Row():
                    stClear = gr.Button("Limpar chat")
                    stExport = gr.Button("Exportar conversa (PDF)")
                with gr.Row():
                    stBackToSetup = gr.Button(
                        "⬅️ Voltar para configuração da sala")
                    stEndChat = gr.Button("Encerrar Chat", variant="stop")
                stChatInput = gr.MultimodalTextbox(
                    show_label=False, placeholder="Digite sua mensagem ou envie um PDF...", sources=["upload"], interactive=True
                )

    # ======================== Navegação / Autenticação ========================

    # ======== Auth ========

    # ======== Admin: Nav entre páginas ========
    navClassrooms.click(lambda: _go_admin("classrooms"),
                        outputs=[adminNavState, viewHomeAdmin, viewClassrooms, viewHistory, viewEvaluate, viewProgress, viewAdminPg])
    navHistory.click(lambda: _go_admin("history"),
                     outputs=[adminNavState, viewHomeAdmin, viewClassrooms, viewHistory, viewEvaluate, viewProgress, viewAdminPg])
    navEvaluate.click(lambda: _go_admin("evaluate"),
                      outputs=[adminNavState, viewHomeAdmin, viewClassrooms, viewHistory, viewEvaluate, viewProgress, viewAdminPg])
    navProgress.click(lambda: _go_admin("progress"),
                      outputs=[adminNavState, viewHomeAdmin, viewClassrooms, viewHistory, viewEvaluate, viewProgress, viewAdminPg])
    navAdmin.click(lambda: _go_admin("admin"),
                   outputs=[adminNavState, viewHomeAdmin, viewClassrooms, viewHistory, viewEvaluate, viewProgress, viewAdminPg])

    # ======== Admin: Salas (CRUD) ========
    btnAddClass.click(
        add_classroom,
        inputs=[clsName, clsTheme, clsDesc, clsLocked, classroomsState, subjectsState, authState],
        outputs=[classroomsState, subjectsState, classroomsMd, clsSelect, membClass],
    ).then(
        admin_refresh_subjects,
        inputs=[classroomsState, subjectsState, clsSelect],
        outputs=[clsActiveList, clsSubjectsMd],
    )
    btnRefreshCls.click(
        refresh_classrooms,
        inputs=[classroomsState, subjectsState],
        outputs=[classroomsState, subjectsState, classroomsMd, clsSelect, membClass],
    ).then(
        admin_refresh_subjects,
        inputs=[classroomsState, subjectsState, clsSelect],
        outputs=[clsActiveList, clsSubjectsMd],
    )
    clsSelect.change(load_cls_for_edit, inputs=[clsSelect, classroomsState],
                     outputs=[eName, eTheme, eDesc, eLocked, eArchived, classroomsMd]).then(
        admin_refresh_subjects,
        inputs=[classroomsState, subjectsState, clsSelect],
        outputs=[clsActiveList, clsSubjectsMd],
    )
    btnSaveCls.click(
        save_cls,
        inputs=[clsSelect, eName, eTheme, eDesc, eLocked, eArchived, classroomsState, subjectsState],
        outputs=[classroomsState, subjectsState, classroomsMd],
    ).then(
        admin_refresh_subjects,
        inputs=[classroomsState, subjectsState, clsSelect],
        outputs=[clsActiveList, clsSubjectsMd],
    )
    btnDeleteCls.click(delete_cls, inputs=[clsSelect, classroomsState, subjectsState],
                       outputs=[classroomsState, subjectsState, classroomsMd]).then(
        admin_refresh_subjects,
        inputs=[classroomsState, subjectsState, clsSelect],
        outputs=[clsActiveList, clsSubjectsMd],
    )
    membClass.change(
        lambda cid, cls: _render_members_md(cid, cls),
        inputs=[membClass, classroomsState],
        outputs=[membersMd],
    )
    btnAddTeacher.click(
        add_teacher,
        inputs=[membClass, addTeacher, classroomsState, subjectsState, authState],
        outputs=[classroomsState, subjectsState, membersMd],
    )
    btnAddStudent.click(
        add_student,
        inputs=[membClass, addStudent, classroomsState, subjectsState],
        outputs=[classroomsState, subjectsState, membersMd],
    )
    btnRmUser.click(
        remove_member,
        inputs=[membClass, rmUser, classroomsState, subjectsState],
        outputs=[classroomsState, subjectsState, membersMd],
    )
    btnClsAddSubj.click(
        admin_add_subject,
        inputs=[clsSelect, clsSubjName, subjectsState, classroomsState, authState],
        outputs=[classroomsState, subjectsState, clsActiveList, clsSubjectsMd],
    )
    btnClsApplyActive.click(
        admin_apply_active,
        inputs=[clsSelect, clsActiveList, subjectsState, classroomsState],
        outputs=[classroomsState, subjectsState, clsActiveList, clsSubjectsMd],
    )
    clsBackAdminHome.click(lambda: _go_admin("home"),
                           outputs=[adminNavState, viewHomeAdmin, viewClassrooms, viewHistory, viewEvaluate, viewProgress, viewAdminPg])

    # ======== Professor: Navegação e ações ========
    btnTeacherClassrooms.click(lambda: (gr.update(visible=False), gr.update(visible=True)),
                               outputs=[viewHome, viewTeacher])
    btnTeacherBack.click(lambda: (gr.update(visible=True), gr.update(visible=False)),
                         outputs=[viewHome, viewTeacher])

    # Encadeamentos do Professor
    btnTeacherAddTeacher.click(
        teacher_add_teacher,
        inputs=[tSelectClass, tAddTeacher, classroomsState, subjectsState, authState],
        outputs=[classroomsState, subjectsState, tTeachersMd],
    )

    btnTeacherLoadParams.click(
        teacher_load_params,
        inputs=[tSelectClass, classroomsState],
        outputs=[tTemp, tTopP, tTopK, tMaxT, tEstilo,
                 tDetalhamento, tExtras, tParamsMsg]
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
            classroomsState,
            subjectsState,
            authState,
        ],
        outputs=[classroomsState, subjectsState, tParamsMsg],
    )

    btnTeacherAddClass.click(
        teacher_add_classroom,
        inputs=[
            tClsName,
            tClsTheme,
            tClsDesc,
            tClsLocked,
            classroomsState,
            subjectsState,
            authState,
        ],
        outputs=[classroomsState, subjectsState, tClassroomsMd, tSelectClass, tSelectClass, teacherNotice],
    )
    btnTeacherRefresh.click(
        teacher_refresh,
        inputs=[authState, classroomsState, subjectsState],
        outputs=[classroomsState, subjectsState, tClassroomsMd, tSelectClass],
    )

    tSelectClass.change(_teacher_on_select, inputs=[authState, classroomsState, tSelectClass, subjectsState],
                        outputs=[tMembersMd, tSelectClass, tActiveList, tSubjectsMd])

    btnTeacherAddStudent.click(
        teacher_add_student,
        inputs=[tSelectClass, tAddStudent, classroomsState, subjectsState, authState],
        outputs=[classroomsState, subjectsState, tMembersMd],
    )
    btnTeacherRmUser.click(
        teacher_rm_user,
        inputs=[tSelectClass, tRmUser, classroomsState, subjectsState, authState],
        outputs=[classroomsState, subjectsState, tMembersMd],
    )
    btnTeacherAddSubj.click(
        teacher_add_subject,
        inputs=[authState, tSelectClass, tSubjName, subjectsState, classroomsState],
        outputs=[classroomsState, subjectsState, tSelectClass, tActiveList, tSubjectsMd],
    )
    btnTeacherApplyActive.click(
        teacher_apply_active,
        inputs=[authState, tSelectClass, tActiveList, subjectsState, classroomsState],
        outputs=[classroomsState, subjectsState, tSubjectsMd],
    )

    # ======== Admin: Subtemas (na página de salas) ========
    # ======== PÁGINA: Histórico ========
    btnHistoryRefresh.click(refresh_history, inputs=[
                            chatsState, histMineOnly, authState], outputs=[historyMd])
    histBack.click(lambda: _go_admin("home"),
                   outputs=[adminNavState, viewHomeAdmin, viewClassrooms, viewHistory, viewEvaluate, viewProgress, viewAdminPg])

    # ======== PÁGINA: Avaliação (com handlers tolerantes) ========
    btnEvalRefresh.click(eval_refresh_dropdown, inputs=[
                         chatsState], outputs=[evalChatId])
    evalChatId.change(eval_load, inputs=[
                      evalChatId, chatsState], outputs=[evalCurrent])
    btnSaveEval.click(eval_save, inputs=[evalChatId, evalScore, evalRubric, evalFeedback, chatsState],
                      outputs=[chatsState, evalCurrent])
    evalBack.click(lambda: _go_admin("home"),
                   outputs=[adminNavState, viewHomeAdmin, viewClassrooms, viewHistory, viewEvaluate, viewProgress, viewAdminPg])

    # ======== PÁGINA: Progresso ========
    btnProgRefresh.click(refresh_progress, inputs=[
                         chatsState, progMineOnly, authState], outputs=[progressMd])
    progBack.click(lambda: _go_admin("home"),
                   outputs=[adminNavState, viewHomeAdmin, viewClassrooms, viewHistory, viewEvaluate, viewProgress, viewAdminPg])

    # ======== Home Admin: atalhos ========
    btnAdminAsStudent.click(lambda: (gr.update(visible=False), gr.update(visible=True)),
                            outputs=[viewHomeAdmin, viewStudio])
    btnAdminListStudents.click(
        listStudents, inputs=authState, outputs=[studentsOut])

    # ======== Encadeamentos de Login/Registro ========
    authMode.change(
        switch_auth_mode,
        inputs=[authMode],
        outputs=[registerRow, registerRoleRow, btnLogin, btnRegister, loginMsg],
    )

    btnLogin.click(
        doLogin, inputs=[username, password,
                         authState], outputs=[loginMsg, authState]
    ).then(
        _route_home, inputs=authState, outputs=[
            header, viewLogin, viewHome, viewHomeAdmin, homeGreet]
    ).then(
        _teacherUi, inputs=authState, outputs=[profRow, studentsOut]
    ).then(
        _studentUi, inputs=authState, outputs=[studentRow]
    ).then(
        _sync_domain_after_auth,
        inputs=[authState, classroomsState, subjectsState],
        outputs=[classroomsState, subjectsState],
    )

    btnRegister.click(
        doRegister, inputs=[username, password, email, fullName, roleRadio,
                            authState], outputs=[loginMsg, authState]
    ).then(
        _route_home, inputs=authState, outputs=[
            header, viewLogin, viewHome, viewHomeAdmin, homeGreet]
    ).then(
        _teacherUi, inputs=authState, outputs=[profRow, studentsOut]
    ).then(
        _studentUi, inputs=authState, outputs=[studentRow]
    ).then(
        _sync_domain_after_auth,
        inputs=[authState, classroomsState, subjectsState],
        outputs=[classroomsState, subjectsState],
    )

    # ======== Navegação básica e Logout ========
    btnGoCustomize.click(lambda: (gr.update(visible=False), gr.update(visible=True)),
                         outputs=[viewHome, viewStudio])
    btnViewStudents.click(listStudents, inputs=authState,
                          outputs=[studentsOut])

    btnLogout1.click(_doLogout, outputs=[
        authState, header, viewLogin, viewHome, viewHomeAdmin, viewStudio, viewClassrooms, viewHistory, viewEvaluate, viewProgress, viewAdminPg
    ])
    btnLogoutAdmin.click(_doLogout, outputs=[
        authState, header, viewLogin, viewHome, viewHomeAdmin, viewStudio, viewClassrooms, viewHistory, viewEvaluate, viewProgress, viewAdminPg
    ])

    # ======== ALUNO: Ações / Encadeamentos ========
    # Home (Aluno) → Minhas Salas
    stEnterRoomChatSetup.click(
        student_setup_from_class,
        inputs=[stRoomSelect, classroomsState, subjectsState, advState],
        outputs=[stAssunto, stSubthemes, stEstilo, stDetalhamento,
                 stObjetivo, stInteresses, stCfgCol, stChatCol, advState, stProvider]
    )

    btnStudentRooms.click(student_go_rooms, outputs=[viewHome, viewStudentRooms]).then(
        student_rooms_refresh, inputs=[authState, classroomsState, subjectsState],
        outputs=[stRoomSelect, stRoomInfo, studentSelectedClass]
    )
    stRoomsRefresh.click(student_rooms_refresh, inputs=[authState, classroomsState, subjectsState],
                         outputs=[stRoomSelect, stRoomInfo, studentSelectedClass])
    stRoomSelect.change(student_on_select, inputs=[stRoomSelect, classroomsState, subjectsState],
                        outputs=[stRoomInfo, studentSelectedClass])

    stEnterRoomChatSetup.click(
        student_go_setup,
        inputs=[authState, studentSelectedClass,
                classroomsState, subjectsState],
        outputs=[viewStudentRooms, viewStudentSetup, stAssunto, stSubthemes]
    )
    stRoomsBack.click(student_rooms_back, outputs=[viewHome, viewStudentRooms])

    stSubmit.click(
        student_apply_setup,
        inputs=[studentSelectedClass, stAssunto, stEstilo, stDetalhamento,
                stObjetivo, stInteresses, stSubthemes, scriptState],
        outputs=[scriptState, stCfgCol, stChatCol]
    )
    stChatInput.submit(
        addMessage,
        inputs=[stChatbot, stChatInput, docsState,
                authState, currentChatId, chatsState],
        outputs=[stChatbot, stChatInput, docsState, currentChatId, chatsState]
    ).then(
        bot,
        inputs=[stChatbot, docsState, scriptState,
                advState, currentChatId, chatsState],
        outputs=stChatbot
    ).then(_student_chat_enable, outputs=stChatInput)
    stClear.click(clearChat, outputs=stChatbot)
    stExport.click(createChatPdf, inputs=[
                   stChatbot, docsState], outputs=gr.File())
    stEndChat.click(
        student_end_chat,
        inputs=[
            stChatbot,
            docsState,
            authState,
            currentChatId,
            chatsState,
            studentSelectedClass,
        ],
        outputs=[
            viewStudentSetup,
            viewStudentRooms,
            stCfgCol,
            stChatCol,
            stChatbot,
            docsState,
            currentChatId,
            chatsState,
        ],
    ).then(
        student_rooms_refresh,
        inputs=[authState, classroomsState, subjectsState],
        outputs=[stRoomSelect, stRoomInfo, studentSelectedClass],
    )
    stBackToSetup.click(_student_chat_back_to_setup,
                        outputs=[stCfgCol, stChatCol])
    stSetupBackRooms.click(lambda: (gr.update(visible=False), gr.update(visible=True)),
                           outputs=[viewStudentSetup, viewStudentRooms])

demo.queue()

if __name__ == "__main__":
    demo.launch()
