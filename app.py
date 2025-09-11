# app.py
import os  # usado em addMessage para basename
import gradio as gr

from services.vertex_client import VERTEX_CFG, _vertex_err, _streamFromVertex
from services.auth_store import _loadUsers, _saveUsers, _hashPw
from services.docs import extractPdfText, createChatPdf
from services.script_builder import buildCustomScript

# ======================== Chat callbacks UI (mesmo comportamento) ========================


def addMessage(history, message, docsState):
    files = message.get("files") if isinstance(message, dict) else []
    text = message.get("text") if isinstance(message, dict) else None
    if not isinstance(docsState, dict):
        docsState = {}
    for filePath in files or []:
        pathLow = (filePath or "").lower()
        if pathLow.endswith(".pdf"):
            try:
                textPdf = extractPdfText(filePath)
                name = os.path.basename(filePath)
                docsState[name] = textPdf
                history.append(
                    {"role": "user", "content": f"Documento ativo: {name} ✅"})
            except Exception as e:
                history.append(
                    {"role": "user", "content": f"Falha ao ler PDF: {os.path.basename(filePath)} ({e})"})
        else:
            history.append(
                {"role": "user", "content": f"Arquivo não suportado: {os.path.basename(filePath)}"})
    if text:
        history.append({"role": "user", "content": text})
    return history, gr.MultimodalTextbox(value=None, interactive=False), docsState


def bot(history, docsState, scriptState):
    docsPreview = ""
    if isinstance(docsState, dict) and docsState:
        joined = []
        for name, content in docsState.items():
            preview = content[:2000]
            joined.append(f"Documento '{name}':\n{preview}")
        docsPreview = "\n\n".join(joined)

    systemMsg = {
        "role": "system",
        "content": f"{scriptState}\n\nDocumentos ativos (amostras):\n{docsPreview}"
    }
    messages = [systemMsg] + history
    history.append({"role": "assistant", "content": ""})

    try:
        for delta in _streamFromVertex(messages, VERTEX_CFG):
            history[-1]["content"] += delta
            yield history
    except Exception as e:
        history[-1]["content"] = f"[ERRO Vertex: {e}]"
        yield history


def clearChat():
    return []


def updateCustomScript(assunto, estilo, detalhamento, objetivo, extras, scriptState):
    return buildCustomScript(assunto, estilo, detalhamento, objetivo, extras)


def validateFields(assunto, estilo, detalhamento, objetivo):
    ok = all([assunto and assunto.strip(), estilo,
             detalhamento, objetivo and objetivo.strip()])
    return gr.Button(interactive=bool(ok))


# ======================== App (Login → Home → Studio) ========================

with gr.Blocks(theme=gr.themes.Default(), fill_height=True) as demo:
    authState = gr.State({"isAuth": False, "username": None})
    docsState = gr.State({})
    scriptState = gr.State(
        "Você é um assistente pedagógico. Aguarde configuração do usuário.")

    # Header
    header_msg = "### 👋 Bem-vindo! Faça login para continuar."
    if _vertex_err:
        header_msg += f"\n\n> **Atenção**: {_vertex_err}"
    else:
        header_msg += (
            f"\n\n> ✅ Credenciais Vertex carregadas de: `{VERTEX_CFG.get('source_path')}` | "
            f"Projeto: `{VERTEX_CFG.get('project')}` | Região: `{VERTEX_CFG.get('location')}` | "
            f"Modelo: `{VERTEX_CFG.get('model')}`"
        )
    with gr.Row():
        header = gr.Markdown(header_msg, elem_id="hdr")

    # Login
    with gr.Column(visible=True) as viewLogin:
        gr.Markdown("## 🔐 Login / Registro")
        with gr.Row():
            username = gr.Textbox(label="Usuário", placeholder="ex: augusto")
            password = gr.Textbox(
                label="Senha", type="password", placeholder="••••••••")
        with gr.Row():
            btnLogin = gr.Button("Entrar", variant="primary")
            btnRegister = gr.Button("Registrar")
        loginMsg = gr.Markdown("")

    # Home
    with gr.Column(visible=False) as viewHome:
        homeGreet = gr.Markdown("## 🏠 Home")
        gr.Markdown("Escolha uma opção para continuar:")
        with gr.Row():
            btnGoCustomize = gr.Button(
                "⚙️ Personalizar o Chat", variant="primary")
            btnLogout1 = gr.Button("Sair")

    # Studio (customização + chat)
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
                with gr.Row():
                    submitBtn = gr.Button(
                        "Aplicar Configuração", variant="primary", interactive=False)
                    reconfigureBtn = gr.Button("Reconfigurar")
            with gr.Column(scale=2, visible=False) as chatCol:
                providerBadge = gr.Markdown(
                    f"**Provedor:** `vertex`  |  **Projeto:** `{(VERTEX_CFG or {}).get('project', '?')}`  |  "
                    f"**Modelo:** `{(VERTEX_CFG or {}).get('model', '?')}`"
                )
                chatbot = gr.Chatbot(label="Chat", type="messages", height=420)
                with gr.Row():
                    clearBtn = gr.Button("Limpar chat")
                    exportBtn = gr.Button("Exportar conversa (PDF)")
                chatInput = gr.MultimodalTextbox(
                    show_label=False, placeholder="Digite sua mensagem ou envie um PDF...", sources=["upload"], interactive=True
                )

        # Validar campos
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
            inputs=None,
            outputs=[configCol, chatCol, chatbot]
        )

        chatMsg = chatInput.submit(addMessage, inputs=[
                                   chatbot, chatInput, docsState], outputs=[chatbot, chatInput, docsState])
        botMsg = chatMsg.then(
            bot, inputs=[chatbot, docsState, scriptState], outputs=chatbot)
        botMsg.then(lambda: gr.MultimodalTextbox(
            interactive=True), outputs=chatInput)
        clearBtn.click(clearChat, outputs=chatbot)
        exportBtn.click(createChatPdf, inputs=[
                        chatbot, docsState], outputs=gr.File())

        btnBackHome.click(lambda: (gr.update(visible=False), gr.update(
            visible=True)), inputs=None, outputs=[viewStudio, viewHome])

    # Navegação (login -> home)
    def _goHomeAfterAuth(auth):
        user = (auth or {}).get("username") or ""
        return (
            gr.update(value=f"### 👋 Olá, **{user}**!", visible=True),
            gr.update(visible=False),
            gr.update(visible=True),
            gr.update(value=f"## 🏠 Home — bem-vindo, **{user}**")
        )

    def doRegister(username, password, authState):
        username = (username or "").strip()
        password = (password or "").strip()
        if not username or not password:
            return gr.update(value="⚠️ Informe usuário e senha."), authState
        db = _loadUsers()
        if username in db:
            return gr.update(value="⚠️ Usuário já existe."), authState
        db[username] = _hashPw(password)
        _saveUsers(db)
        authState = {"isAuth": True, "username": username}
        return gr.update(value=f"✅ Registrado e logado como **{username}**."), authState

    def doLogin(username, password, authState):
        username = (username or "").strip()
        password = (password or "").strip()
        if not username or not password:
            return gr.update(value="⚠️ Informe usuário e senha."), authState
        db = _loadUsers()
        if username not in db or db[username] != _hashPw(password):
            return gr.update(value="❌ Usuário ou senha incorretos."), authState
        authState = {"isAuth": True, "username": username}
        return gr.update(value=f"✅ Bem-vindo, **{username}**!"), authState

    def _doLogout():
        return (
            {"isAuth": False, "username": None},
            gr.update(
                value="### 👋 Bem-vindo! Faça login para continuar.", visible=True),
            gr.update(visible=True),
            gr.update(visible=False),
            gr.update(visible=False),
        )

    btnLogin.click(doLogin, inputs=[username, password, authState], outputs=[loginMsg, authState]).then(
        _goHomeAfterAuth, inputs=authState, outputs=[
            header, viewLogin, viewHome, homeGreet]
    )
    btnRegister.click(doRegister, inputs=[username, password, authState], outputs=[loginMsg, authState]).then(
        _goHomeAfterAuth, inputs=authState, outputs=[
            header, viewLogin, viewHome, homeGreet]
    )
    btnGoCustomize.click(lambda: (gr.update(visible=False), gr.update(
        visible=True)), inputs=None, outputs=[viewHome, viewStudio])
    btnLogout1.click(_doLogout, inputs=None, outputs=[
                     authState, header, viewLogin, viewHome, viewStudio])

demo.queue()

if __name__ == "__main__":
    demo.launch()
