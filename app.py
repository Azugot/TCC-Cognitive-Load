# app.py
import os
import uuid
import time
import gradio as gr

from services.vertex_client import VERTEX_CFG, _vertex_err, _streamFromVertex
from services.auth_store import _loadUsers, _saveUsers, _hashPw, _getUserEntry, _setUserEntry
from services.docs import extractPdfText, createChatPdf
from services.script_builder import buildCustomScript

# =============== Chat callbacks e utilidades básicas (inalterado/estendido) ===============


def _now_ts():
    return int(time.time())


def _mk_id(prefix="id"):
    return f"{prefix}_{str(uuid.uuid4())[:8]}"


def addMessage(history, message, docsState, authState, currentChatId, chatsState):
    """Recebe msg do usuário, anexa em 'history' e registra no chatsState (in-memory) para Histórico/Avaliação/Progresso."""
    # Garantir estruturas
    if not isinstance(docsState, dict):
        docsState = {}
    if not isinstance(chatsState, dict):
        chatsState = {}

    # Criar uma sessão de chat se não existir
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
            # cada item: {"role": "...", "content": "...", "ts": ...}
            "messages": [],
            "summary": "",      # pode ser preenchido futuramente
            "score": None,      # avaliação (professor)
            "rubric": None,
            "feedback": None,
        }

    # Processar uploads + texto
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
        # Ao final do stream, salva mensagem no log do chat
        if currentChatId and isinstance(chatsState, dict) and currentChatId in chatsState:
            chatsState[currentChatId]["messages"].append(
                {"role": "assistant", "content": history[-1]["content"], "ts": _now_ts()})
            # título simples a partir da primeira user msg
            if not chatsState[currentChatId].get("title") and len(history) >= 2:
                first_user_msg = next(
                    (m.get("content") for m in history if m.get("role") == "user"), "")
                if first_user_msg:
                    chatsState[currentChatId]["title"] = first_user_msg[:40]
    except Exception as e:
        history[-1]["content"] = f"[ERRO Vertex: {e}]"
        if currentChatId and isinstance(chatsState, dict) and currentChatId in chatsState:
            chatsState[currentChatId]["messages"].append(
                {"role": "assistant", "content": history[-1]["content"], "ts": _now_ts()})
        yield history


def clearChat():
    return []


def updateCustomScript(assunto, estilo, detalhamento, objetivo, extras, scriptState):
    return buildCustomScript(assunto, estilo, detalhamento, objetivo, extras)


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

# ====================== Render helpers para páginas do Admin ======================


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
            f"- **{c['name']}** ({status}) — tema: _{c.get('theme_name', '?')}_ {lock} — id: `{c['id']}` — 👩‍🏫 {tcount} | 🎓 {scount}")
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
            f"- **{title}** — id: `{cid}` — autor: `{chat.get('owner')}` — {time.strftime('%d/%m %H:%M', time.localtime(ts))}{tag}")
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


def _render_progress_md(chats_map, user_filter=None):
    total = 0
    answered = 0
    evaluated = 0
    for chat in (chats_map or {}).values():
        if user_filter and chat.get("owner") != user_filter:
            continue
        total += 1
        if any(m for m in chat["messages"] if m["role"] == "assistant"):
            answered += 1
        if chat.get("score") is not None:
            evaluated += 1
    lines = [
        "### Indicadores gerais",
        f"- Conversas totais: **{total}**",
        f"- Conversas com resposta da IA: **{answered}**",
        f"- Conversas avaliadas: **{evaluated}**",
    ]
    return "\n".join(lines)


# ================================== APP / UI ==================================
with gr.Blocks(theme=gr.themes.Default(), fill_height=True) as demo:
    # Estados principais
    # role adicionado ao logar
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
    chatsState = gr.State({})              # {chat_id: {...}}
    currentChatId = gr.State(None)         # chat_id atual
    # home|classrooms|subjects|history|evaluate|progress|admin
    adminNavState = gr.State({"page": "home"})

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
        with gr.Row():
            username = gr.Textbox(label="Usuário", placeholder="ex: augusto")
            password = gr.Textbox(
                label="Senha", type="password", placeholder="••••••••")
        with gr.Row():
            roleRadio = gr.Radio(
                choices=["Aluno", "Professor", "Admin"], label="Perfil", value="Aluno")
        with gr.Row():
            btnLogin = gr.Button("Entrar", variant="primary")
            btnRegister = gr.Button("Registrar")
        loginMsg = gr.Markdown("")

    # ===== Home (Aluno/Professor) =====
    with gr.Column(visible=False) as viewHome:
        homeGreet = gr.Markdown("## 🏠 Home")
        gr.Markdown("Escolha uma opção para continuar:")
        with gr.Row():
            btnGoCustomize = gr.Button(
                "⚙️ Personalizar o Chat", variant="primary")
            btnLogout1 = gr.Button("Sair")
        with gr.Row(visible=False) as profRow:
            btnViewStudents = gr.Button(
                "👥 Ver alunos cadastrados", variant="secondary")
        studentsOut = gr.Markdown("")

    # ===== Home (Admin) + Nav superior =====
    with gr.Column(visible=False) as viewHomeAdmin:
        adminGreet = gr.Markdown("## 🧭 Home do Admin")
        with gr.Row():
            navClassrooms = gr.Button("🏫 Salas")
            navSubjects = gr.Button("🧩 Subtemas")
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

    # ===== PÁGINA: Gerenciar Salas =====
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
        classroomsMd = gr.Markdown("")
        with gr.Row():
            clsBackAdminHome = gr.Button("← Voltar à Home do Admin")

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

    # ===== PÁGINA: Subtemas =====
    with gr.Column(visible=False) as viewSubjects:
        gr.Markdown("## 🧩 Gerenciar Subtemas por Sala")
        with gr.Row():
            selectedClass = gr.Dropdown(choices=[], label="Sala", value=None)
            btnSubjectsRefresh = gr.Button("🔄")
        with gr.Row():
            subjName = gr.Textbox(label="Novo subtema",
                                  placeholder="Ex.: Ponteiros")
            btnAddSubj = gr.Button("➕ Adicionar subtema")
        with gr.Row():
            activeList = gr.CheckboxGroup(
                choices=[], label="Ativar/desativar subtemas", value=[])
            btnApplyActive = gr.Button("✅ Aplicar ativações")
        subjectsMd = gr.Markdown("")
        with gr.Row():
            subjBackAdminHome = gr.Button("← Voltar à Home do Admin")

    # ===== PÁGINA: Histórico/Resumo de Chats =====
    with gr.Column(visible=False) as viewHistory:
        gr.Markdown("## 🗂️ Histórico de Chats")
        with gr.Row():
            histMineOnly = gr.Checkbox(
                value=False, label="Mostrar apenas meus chats")
            btnHistoryRefresh = gr.Button("🔄 Atualizar")
        historyMd = gr.Markdown("")
        with gr.Row():
            histBack = gr.Button("← Voltar à Home do Admin")

    # ===== PÁGINA: Avaliação de Chats (Professores) =====
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

    # ===== PÁGINA: Progresso/Relatórios =====
    with gr.Column(visible=False) as viewProgress:
        gr.Markdown("## 📊 Progresso e Relatórios")
        with gr.Row():
            progMineOnly = gr.Checkbox(
                value=False, label="Restringir aos meus chats")
            btnProgRefresh = gr.Button("🔄 Atualizar")
        progressMd = gr.Markdown("")
        with gr.Row():
            progBack = gr.Button("← Voltar à Home do Admin")

    # ===== Studio (customização + chat) =====
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

        # Chat flow (agora registra também em chatsState)
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

        def _back_home(auth):
            role = (auth or {}).get("role", "aluno")
            if str(role).lower() == "admin":
                return (gr.update(visible=False), gr.update(visible=True), gr.update(visible=False))
            else:
                return (gr.update(visible=False), gr.update(visible=False), gr.update(visible=True))

        btnBackHome.click(_back_home, inputs=authState, outputs=[
                          viewStudio, viewHomeAdmin, viewHome])

    # ======================== Navegação / Autenticação ========================

    def _route_home(auth):
        is_auth = bool(auth and auth.get("isAuth")
                       is True and auth.get("username"))
        user = (auth or {}).get("username") or ""
        role = (auth or {}).get("role", "aluno")
        print(
            f"[NAV] _route_home: isAuth={is_auth} user='{user}' role='{role}'")

        if not is_auth:
            # mantém na tela de login
            return (
                # header
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

    def doRegister(username, password, role, authState):
        uname = (username or "").strip().lower()
        pw = (password or "").strip()
        print(f"[AUTH] doRegister: uname='{uname}' role='{role}'")
        if not uname or not pw:
            return gr.update(value="⚠️ Informe usuário e senha."), authState

        db = _loadUsers()
        if uname in db:
            print(f"[AUTH] doRegister: usuário já existe -> {uname}")
            return gr.update(value="⚠️ Usuário já existe."), authState

        role = (role or "aluno").strip().lower()
        _setUserEntry(db, uname, _hashPw(pw), role)
        _saveUsers(db)

        authState = {"isAuth": True, "username": uname, "role": role}
        print(f"[AUTH] doRegister: registrado e logado -> {authState}")
        return gr.update(value=f"✅ Registrado e logado como **{uname}** (perfil: {role})."), authState

    def doLogin(username, password, authState):
        uname = (username or "").strip().lower()
        pw = (password or "").strip()
        print(f"[AUTH] doLogin: uname='{uname}'")
        if not uname or not pw:
            return gr.update(value="⚠️ Informe usuário e senha."), authState

        db = _loadUsers()
        entry = _getUserEntry(db, uname)
        if not entry:
            print(f"[AUTH] doLogin: usuário não encontrado -> {uname}")
            return gr.update(value="❌ Usuário ou senha incorretos."), authState

        if entry.get("pw") != _hashPw(pw):
            print(f"[AUTH] doLogin: senha incorreta -> {uname}")
            return gr.update(value="❌ Usuário ou senha incorretos."), authState

        role = (entry.get("role") or "aluno").lower()
        authState = {"isAuth": True, "username": uname, "role": role}
        print(f"[AUTH] doLogin: sucesso -> {authState}")
        return gr.update(value=f"✅ Bem-vindo, **{uname}** (perfil: {role})."), authState

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
            gr.update(visible=False),  # viewSubjects
            gr.update(visible=False),  # viewHistory
            gr.update(visible=False),  # viewEvaluate
            gr.update(visible=False),  # viewProgress
            gr.update(visible=False),  # viewAdminPg
        )

    def listStudents(auth):
        role = (auth or {}).get("role", "aluno")
        if str(role).lower() not in ("professor", "admin"):
            return "⚠️ Apenas professores/admin podem visualizar a lista de alunos."
        db = _loadUsers()
        students = [
            u for u, e in (db or {}).items()
            if (isinstance(e, dict) and (e.get("role", "aluno") == "aluno")) or isinstance(e, str)
        ]
        if not students:
            return "Nenhum aluno cadastrado ainda."
        students.sort(key=lambda x: x.lower())
        bullet = "\n".join([f"- {s}" for s in students])
        return f"### Alunos cadastrados ({len(students)})\n\n{bullet}"

    # ======== Admin: Navegação entre páginas ========
    def _go_admin(page):
        vis = {
            "home":     (True,  False, False, False, False, False, False),
            "classrooms": (False, True,  False, False, False, False, False),
            "subjects": (False, False, True,  False, False, False, False),
            "history":  (False, False, False, True,  False, False, False),
            "evaluate": (False, False, False, False, True,  False, False),
            "progress": (False, False, False, False, False, True,  False),
            "admin":    (False, False, False, False, False, False, True),
        }.get(page, (True, False, False, False, False, False, False))
        (homeV, clsV, subV, histV, evalV, progV, admV) = vis
        return (
            {"page": page},
            gr.update(visible=homeV), gr.update(
                visible=clsV), gr.update(visible=subV),
            gr.update(visible=histV), gr.update(visible=evalV), gr.update(
                visible=progV), gr.update(visible=admV)
        )

    navClassrooms.click(lambda: _go_admin("classrooms"),
                        outputs=[adminNavState, viewHomeAdmin, viewClassrooms, viewSubjects, viewHistory, viewEvaluate, viewProgress, viewAdminPg])
    navSubjects.click(lambda: _go_admin("subjects"),
                      outputs=[adminNavState, viewHomeAdmin, viewClassrooms, viewSubjects, viewHistory, viewEvaluate, viewProgress, viewAdminPg])
    navHistory.click(lambda: _go_admin("history"),
                     outputs=[adminNavState, viewHomeAdmin, viewClassrooms, viewSubjects, viewHistory, viewEvaluate, viewProgress, viewAdminPg])
    navEvaluate.click(lambda: _go_admin("evaluate"),
                      outputs=[adminNavState, viewHomeAdmin, viewClassrooms, viewSubjects, viewHistory, viewEvaluate, viewProgress, viewAdminPg])
    navProgress.click(lambda: _go_admin("progress"),
                      outputs=[adminNavState, viewHomeAdmin, viewClassrooms, viewSubjects, viewHistory, viewEvaluate, viewProgress, viewAdminPg])
    navAdmin.click(lambda: _go_admin("admin"),
                   outputs=[adminNavState, viewHomeAdmin, viewClassrooms, viewSubjects, viewHistory, viewEvaluate, viewProgress, viewAdminPg])

    # ======== PÁGINA: Salas (CRUD + membros em memória) ========
    def _refresh_cls_dropdown(classrooms):
        choices = [(c["name"], c["id"]) for c in (classrooms or [])]
        return gr.update(choices=choices), gr.update(choices=choices)

    def add_classroom(name, theme, desc, locked, classrooms):
        name = (name or "").strip()
        theme = (theme or "").strip()
        if not name:
            return classrooms, "⚠️ Informe um nome para a sala.", gr.update(), gr.update()
        classroom = {
            "id": _mk_id("cls"),
            "name": name,
            "description": desc or "",
            "theme_name": theme or name,
            "theme_locked": bool(locked),
            "is_archived": False,
            "members": {"teachers": [], "students": []},
        }
        new_list = list(classrooms or []) + [classroom]
        md = _render_classrooms_md(new_list)
        dd1, dd2 = _refresh_cls_dropdown(new_list)
        return new_list, md, dd1, dd2

    def refresh_classrooms(classrooms):
        md = _render_classrooms_md(classrooms or [])
        dd1, dd2 = _refresh_cls_dropdown(classrooms or [])
        return md, dd1, dd2

    def load_cls_for_edit(cls_id, classrooms):
        c = next((x for x in (classrooms or []) if x["id"] == cls_id), None)
        if not c:
            return gr.update(value=""), gr.update(value=""), gr.update(value=""), gr.update(value=True), gr.update(value=False), "⚠️ Sala não encontrada."
        return gr.update(value=c["name"]), gr.update(value=c["theme_name"]), gr.update(value=c["description"]), gr.update(value=c["theme_locked"]), gr.update(value=c["is_archived"]), ""

    def save_cls(cls_id, name, theme, desc, locked, archived, classrooms):
        if not cls_id:
            return classrooms, "⚠️ Selecione uma sala."
        new = []
        found = False
        for c in (classrooms or []):
            if c["id"] == cls_id:
                found = True
                c = dict(c)
                c["name"] = (name or "").strip() or c["name"]
                c["theme_name"] = (theme or "").strip() or c["theme_name"]
                c["description"] = desc or ""
                c["theme_locked"] = bool(locked)
                c["is_archived"] = bool(archived)
            new.append(c)
        if not found:
            return classrooms, "⚠️ Sala não encontrada."
        return new, _render_classrooms_md(new)

    def delete_cls(cls_id, classrooms, subjects_by_class):
        if not cls_id:
            return classrooms, subjects_by_class, "⚠️ Selecione uma sala."
        new = [c for c in (classrooms or []) if c["id"] != cls_id]
        sbj = dict(subjects_by_class or {})
        sbj.pop(cls_id, None)
        return new, sbj, _render_classrooms_md(new)

    def _render_members_md(cls_id, classrooms):
        c = next((x for x in (classrooms or []) if x["id"] == cls_id), None)
        if not c:
            return "⚠️ Selecione uma sala."
        t = c["members"]["teachers"]
        s = c["members"]["students"]
        lines = [f"### Membros da sala `{c['name']}`", f"- 👩‍🏫 Professores ({len(t)}): " + (', '.join(t) if t else "—"),
                 f"- 🎓 Alunos ({len(s)}): " + (', '.join(s) if s else "—")]
        return "\n".join(lines)

    def add_teacher(cls_id, uname, classrooms):
        if not cls_id or not uname:
            return "⚠️ Informe sala e username."
        for c in (classrooms or []):
            if c["id"] == cls_id:
                if uname not in c["members"]["teachers"]:
                    c["members"]["teachers"].append(uname)
                return _render_members_md(cls_id, classrooms)
        return "⚠️ Sala não encontrada."

    def add_student(cls_id, uname, classrooms):
        if not cls_id or not uname:
            return "⚠️ Informe sala e username."
        for c in (classrooms or []):
            if c["id"] == cls_id:
                if uname not in c["members"]["students"]:
                    c["members"]["students"].append(uname)
                return _render_members_md(cls_id, classrooms)
        return "⚠️ Sala não encontrada."

    def remove_member(cls_id, uname, classrooms):
        if not cls_id or not uname:
            return "⚠️ Informe sala e username."
        for c in (classrooms or []):
            if c["id"] == cls_id:
                c["members"]["teachers"] = [
                    u for u in c["members"]["teachers"] if u != uname]
                c["members"]["students"] = [
                    u for u in c["members"]["students"] if u != uname]
                return _render_members_md(cls_id, classrooms)
        return "⚠️ Sala não encontrada."

    btnAddClass.click(add_classroom,
                      inputs=[clsName, clsTheme, clsDesc,
                              clsLocked, classroomsState],
                      outputs=[classroomsState, classroomsMd, clsSelect, membClass])
    btnRefreshCls.click(refresh_classrooms, inputs=[classroomsState], outputs=[
                        classroomsMd, clsSelect, membClass])
    clsSelect.change(load_cls_for_edit, inputs=[clsSelect, classroomsState], outputs=[
                     eName, eTheme, eDesc, eLocked, eArchived, classroomsMd])
    btnSaveCls.click(save_cls, inputs=[clsSelect, eName, eTheme, eDesc, eLocked,
                     eArchived, classroomsState], outputs=[classroomsState, classroomsMd])
    btnDeleteCls.click(delete_cls, inputs=[clsSelect, classroomsState, subjectsState], outputs=[
                       classroomsState, subjectsState, classroomsMd])
    membClass.change(lambda cid, cls: _render_members_md(cid, cls), inputs=[
                     membClass, classroomsState], outputs=[membersMd])
    btnAddTeacher.click(add_teacher, inputs=[
                        membClass, addTeacher, classroomsState], outputs=[membersMd])
    btnAddStudent.click(add_student, inputs=[
                        membClass, addStudent, classroomsState], outputs=[membersMd])
    btnRmUser.click(remove_member, inputs=[
                    membClass, rmUser, classroomsState], outputs=[membersMd])
    clsBackAdminHome.click(lambda: _go_admin("home"),
                           outputs=[adminNavState, viewHomeAdmin, viewClassrooms, viewSubjects, viewHistory, viewEvaluate, viewProgress, viewAdminPg])

    # ======== PÁGINA: Subtemas (lista + ativação) ========
    def _subjects_choices(classrooms, selected_id, subjects_by_class):
        # Atualiza dropdown e lista + checkbox de ativos
        dd = gr.update(choices=[(c["name"], c["id"]) for c in (
            classrooms or [])], value=selected_id if selected_id else None)
        if not selected_id:
            return dd, gr.update(choices=[], value=[]), _render_subjects_md(subjects_by_class, None, classrooms or [])
        lst = subjects_by_class.get(selected_id, [])
        all_names = [s["name"] for s in lst]
        active = [s["name"] for s in lst if s.get("active")]
        return dd, gr.update(choices=all_names, value=active), _render_subjects_md(subjects_by_class, selected_id, classrooms or [])

    def subjects_refresh(classrooms, selected_id, subjects_by_class):
        return _subjects_choices(classrooms, selected_id, subjects_by_class)

    def add_subject(selected_id, subj, subjects_by_class, classrooms):
        if not classrooms:
            return subjects_by_class, gr.update(), gr.update(), "⚠️ Cadastre uma sala primeiro."
        if not selected_id:
            return subjects_by_class, gr.update(), gr.update(), "ℹ️ Selecione uma sala para adicionar subtemas."
        subj = (subj or "").strip()
        if not subj:
            return subjects_by_class, gr.update(), gr.update(), "⚠️ Informe o nome do subtema."
        lst = list(subjects_by_class.get(selected_id, []))
        if any(s["name"] == subj for s in lst):
            return subjects_by_class, gr.update(), gr.update(), "⚠️ Esse subtema já existe."
        lst.append({"name": subj, "active": True})
        new_map = dict(subjects_by_class)
        new_map[selected_id] = lst
        dd, chk, md = _subjects_choices(classrooms, selected_id, new_map)
        return new_map, dd, chk, md

    def apply_active(selected_id, actives, subjects_by_class, classrooms):
        if not selected_id:
            return subjects_by_class, _render_subjects_md(subjects_by_class, None, classrooms or [])
        lst = list(subjects_by_class.get(selected_id, []))
        names = set(actives or [])
        for s in lst:
            s["active"] = s["name"] in names
        new_map = dict(subjects_by_class)
        new_map[selected_id] = lst
        return new_map, _render_subjects_md(new_map, selected_id, classrooms or [])

    btnSubjectsRefresh.click(subjects_refresh, inputs=[
        classroomsState, selectedClass, subjectsState], outputs=[selectedClass, activeList, subjectsMd])
    selectedClass.change(subjects_refresh, inputs=[
        classroomsState, selectedClass, subjectsState], outputs=[selectedClass, activeList, subjectsMd])
    btnAddSubj.click(add_subject, inputs=[selectedClass, subjName, subjectsState, classroomsState], outputs=[
        subjectsState, selectedClass, activeList, subjectsMd])
    btnApplyActive.click(apply_active, inputs=[
        selectedClass, activeList, subjectsState, classroomsState], outputs=[subjectsState, subjectsMd])
    subjBackAdminHome.click(lambda: _go_admin("home"),
                            outputs=[adminNavState, viewHomeAdmin, viewClassrooms, viewSubjects, viewHistory, viewEvaluate, viewProgress, viewAdminPg])

    # ======== PÁGINA: Histórico ========
    def refresh_history(chats_map, mine_only, auth):
        user = (auth or {}).get("username")
        return _render_history_md(chats_map, owner=user if mine_only else None)

    btnHistoryRefresh.click(refresh_history, inputs=[
                            chatsState, histMineOnly, authState], outputs=[historyMd])
    histBack.click(lambda: _go_admin("home"),
                   outputs=[adminNavState, viewHomeAdmin, viewClassrooms, viewSubjects, viewHistory, viewEvaluate, viewProgress, viewAdminPg])

    # ======== PÁGINA: Avaliação ========
    def eval_refresh_dropdown(chats_map):
        # apenas chats que têm ao menos uma msg de aluno
        ids = []
        for cid, chat in (chats_map or {}).items():
            if any(m for m in chat["messages"] if m["role"] == "user"):
                ids.append((chat.get("title") or cid, cid))
        return gr.update(choices=ids)

    def eval_load(chat_id, chats_map):
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

    btnEvalRefresh.click(eval_refresh_dropdown, inputs=[
                         chatsState], outputs=[evalChatId])
    evalChatId.change(eval_load, inputs=[
                      evalChatId, chatsState], outputs=[evalCurrent])
    btnSaveEval.click(eval_save, inputs=[
                      evalChatId, evalScore, evalRubric, evalFeedback, chatsState], outputs=[chatsState, evalCurrent])
    evalBack.click(lambda: _go_admin("home"),
                   outputs=[adminNavState, viewHomeAdmin, viewClassrooms, viewSubjects, viewHistory, viewEvaluate, viewProgress, viewAdminPg])

    # ======== PÁGINA: Progresso ========
    def refresh_progress(chats_map, mine_only, auth):
        user = (auth or {}).get("username")
        return _render_progress_md(chats_map, user_filter=user if mine_only else None)

    btnProgRefresh.click(refresh_progress, inputs=[
                         chatsState, progMineOnly, authState], outputs=[progressMd])
    progBack.click(lambda: _go_admin("home"),
                   outputs=[adminNavState, viewHomeAdmin, viewClassrooms, viewSubjects, viewHistory, viewEvaluate, viewProgress, viewAdminPg])

    # ======== Home Admin: atalhos ========
    btnAdminAsStudent.click(lambda: (gr.update(visible=False), gr.update(visible=True)),
                            inputs=None, outputs=[viewHomeAdmin, viewStudio])
    btnAdminListStudents.click(
        listStudents, inputs=authState, outputs=[studentsOut])

    # ======== Encadeamentos de Login/Registro ========
    btnLogin.click(
        doLogin, inputs=[username, password,
                         authState], outputs=[loginMsg, authState]
    ).then(
        _route_home, inputs=authState, outputs=[
            header, viewLogin, viewHome, viewHomeAdmin, homeGreet]
    ).then(
        _teacherUi, inputs=authState, outputs=[profRow, studentsOut]
    )

    btnRegister.click(
        doRegister, inputs=[username, password, roleRadio,
                            authState], outputs=[loginMsg, authState]
    ).then(
        _route_home, inputs=authState, outputs=[
            header, viewLogin, viewHome, viewHomeAdmin, homeGreet]
    ).then(
        _teacherUi, inputs=authState, outputs=[profRow, studentsOut]
    )

    # ======== Navegação básica e Logout ========
    btnGoCustomize.click(lambda: (gr.update(visible=False), gr.update(
        visible=True)), inputs=None, outputs=[viewHome, viewStudio])
    btnViewStudents.click(listStudents, inputs=authState,
                          outputs=[studentsOut])

    btnLogout1.click(_doLogout, inputs=None, outputs=[
        authState, header, viewLogin, viewHome, viewHomeAdmin, viewStudio, viewClassrooms, viewSubjects, viewHistory, viewEvaluate, viewProgress, viewAdminPg
    ])
    btnLogoutAdmin.click(_doLogout, inputs=None, outputs=[
        authState, header, viewLogin, viewHome, viewHomeAdmin, viewStudio, viewClassrooms, viewSubjects, viewHistory, viewEvaluate, viewProgress, viewAdminPg
    ])

demo.queue()

if __name__ == "__main__":
    demo.launch()
