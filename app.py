
import os
import json
import hashlib
from datetime import datetime

import fitz  # PyMuPDF
import gradio as gr

# Vertex AI + creds
try:
    import vertexai as _vertexai
    from vertexai.generative_models import GenerativeModel as _VertexGenerativeModel
    from google.oauth2.service_account import Credentials as _GcpCredentials
except Exception:
    _vertexai = None
    _VertexGenerativeModel = None
    _GcpCredentials = None

from reportlab.lib.pagesizes import letter
from reportlab.platypus import SimpleDocTemplate, Paragraph, Spacer, PageBreak
from reportlab.lib.styles import getSampleStyleSheet, ParagraphStyle
from reportlab.lib.units import inch

HF_USERS_DB = "/tmp/users.json"  # demo only

# ======================== Vertex auto-config (no UI) ========================
DEFAULT_LOCATIONS = [
    "./acquired-router-470921-a3-a19e3f67c416.json"
]


def _load_vertex_cfg_from_disk():
    """Load service account JSON from disk without any UI. Returns cfg dict or raises."""
    # If user set a specific path via constant below, honor it.
    preset_path = os.environ.get(
        "VERTEX_SA_PATH", "").strip()  # optional escape hatch
    candidate_paths = ([preset_path] if preset_path else []
                       ) + DEFAULT_LOCATIONS
    last_err = None
    for p in candidate_paths:
        try:
            if not p:
                continue
            if os.path.exists(p):
                with open(p, "r", encoding="utf-8") as f:
                    sa = json.load(f)
                project = sa.get("project_id")
                if not project:
                    raise ValueError("Campo 'project_id' ausente no JSON.")
                if not sa.get("private_key") or not sa.get("client_email"):
                    raise ValueError(
                        "Campos 'private_key' e 'client_email' são obrigatórios.")
                # Defaults
                location = os.environ.get(
                    "GOOGLE_CLOUD_LOCATION", "us-central1")
                model = os.environ.get("VERTEX_MODEL", "gemini-2.5-flash")
                return {
                    "project": project,
                    "location": location,
                    "model": model,
                    "serviceAccount": sa,
                    "source_path": p,
                }
        except Exception as e:
            last_err = e
            continue
    if last_err:
        raise RuntimeError(
            f"Não foi possível carregar as credenciais do Vertex de disco. Último erro: {last_err}")
    raise RuntimeError(
        "Arquivo service-account.json não encontrado. Coloque-o ao lado do app ou em /mnt/data/.")


try:
    VERTEX_CFG = _load_vertex_cfg_from_disk()
    _vertex_err = None
except Exception as _e:
    VERTEX_CFG = None
    _vertex_err = str(_e)


def _vertex_init_or_raise(cfg):
    if _vertexai is None or _VertexGenerativeModel is None or _GcpCredentials is None:
        raise RuntimeError(
            "Dependências Google ausentes. Instale 'google-cloud-aiplatform'.")
    if not cfg:
        raise RuntimeError(
            "Configuração do Vertex indisponível. Verifique o arquivo de credenciais no disco.")
    creds = _GcpCredentials.from_service_account_info(cfg["serviceAccount"])
    _vertexai.init(project=cfg["project"],
                   location=cfg["location"], credentials=creds)
    return _VertexGenerativeModel(cfg["model"])

# ======================== Auth helpers ========================


def _loadUsers():
    try:
        with open(HF_USERS_DB, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return {}


def _saveUsers(db):
    try:
        with open(HF_USERS_DB, "w", encoding="utf-8") as f:
            json.dump(db, f)
    except Exception:
        pass


def _hashPw(pw: str) -> str:
    return hashlib.sha256(pw.encode("utf-8")).hexdigest()

# ======================== Utils ========================


def extractPdfText(filePath: str) -> str:
    textParts = []
    with fitz.open(filePath) as doc:
        for page in doc:
            textParts.append(page.get_text("text"))
    text = "\n".join(textParts)
    return text[:20000]


def buildCustomScript(assunto, estilo, detalhamento, objetivo, extras):
    lines = [
        "Você é um assistente pedagógico que gera respostas sob medida para o usuário.",
        "Papel principal: Tutor de Programacao",
        f"Tópico: {assunto}.",
        f"Objetivo principal: {objetivo}.",
        f"""Orientacoes Base:
        Nunca passe direto de uma explicacao para exemplos praticos de uma vez, garanta que o usuário entendeu o conceito antes de mostrar qualquer exemplo real.
        Exemplos devem ser didáticos e seguirem o conceito da Teoria da Carga Cognitiva de Sweller.
        Utilize de linguagem objetiva e simples, correlacione os topicos com algo que o usuário conhece ou tem interesse.
        Caso apresente confusão mude a abordagem da explicacao, sempre de exemplos antes de aplicar qualquer validacao de connhecimento. 
        Não utlize exemplos complexos com textos e informacoes 'irrelevantes', não queremos sobrecarregar o cérebro do usuário de informacões desnecessarias.
        Caso o usuário queira iniciar de um tópico em especifico, garanta que ele possui os fundamentos necessarios para executa-lo, aplicando uma validacao de conceito e conhecimento para garantir capacidade de entendimento.
        Caso nao seja apresentada a proficiencia necessaria em algum topico que seja um "pré-requisito", comece por ele e desenvolva os conhecimentos necessarios
        Ao perceber que o usuário compreendeu o conceito, aplique uma 'Validacao de conceito' onde o usuario é requisitado á resolver um problema relacionado ao conceito em questao
        Na proxima etapa, passe para algo tecnico, incorporando elementos mais complexos a medida que o usuário demonstra conhecimento e entendimento, sempre levando em conta que não queremos sobrecarregar a memoria do usuario.
        Conforme o conforto sobre o assunto aumenta, caso chegue em um nível onde se julga "proficiente" no topico em questão, aplique uma "Validacao de conhecimento", requisitando que o usuário resolva uma questão tecnica.
        Instrua o usuário de forma clara e concisa, evite detalhes desnecessarios no enunciado da Validacao de Conhecimento.
        De 3 chances ao usuário ao executar a validacao, NUNCA HIPOTESE ALGUMA DE O RESULTADO ANTES DA 3 TENTATIVA, caso ele esgote as 3 tentativas, ofereca para criar um exemplo mais simples ou rever o conceito, caso o usuário não queira, ofereca para guia-lo para chegar na solucão.
        Caso o usuário insista em tentar resolver o problema sem sucesso, mude a abordagem, revisando o conteúdo e auxiliando conforme necessario.
        Apenas de a resposta em ultimo caso! Nao queremos que ele desista do aprendizado, mas tambem nao é permitido simplesmente dar a resposta. Queremos evitar frustracao por dificuldade de resolver o exercicio.
        Caso o usuáro tenha sucesso, considere-o com conhecimento validado! 
        Após ter o conhecimento validado, pergunte ao usuário se deseja seguir para um conceito mais complexo ou algum outro topico em especifico.
        Caso esteja em um cenario onde está seguindo um topico em especifico, siga-o executando as etapas corretamente até o usuário estiver satisfeito.
        GARANTA QUE O USUARIO CONCORDOU COM O SEGUIMENTO ESCOLHIDO! Caso haja qualquer ambiguidade confirme-a antes de prosseguir.
        """
        "Regras:",
        "- Não invente fatos; se não souber, explique a limitação e proponha passos para descobrir.",
        "- Mencione suposições quando necessário.",
        "- Use exemplos quando isso ajudar a clarear.",
    ]
    if detalhamento == "detalhadas":
        lines.append(
            "- Respostas detalhadas, com passos e exemplos quando possível.")
    else:
        lines.append("- Respostas curtas e diretas ao ponto.")
    if estilo == "técnicas":
        lines.append(
            "- Linguagem técnica com termos específicos quando pertinente.")
    else:
        lines.append("- Linguagem simples e acessível para iniciantes.")
    if extras:
        lines.append(f"- Preferências adicionais: {extras}")
    return "\n".join(lines)


def createChatPdf(history, docsState):
    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    outPath = f"/mnt/data/chat_{timestamp}.pdf"

    doc = SimpleDocTemplate(outPath, pagesize=letter, leftMargin=36,
                            rightMargin=36, topMargin=36, bottomMargin=36)
    styles = getSampleStyleSheet()
    body = []

    titleStyle = ParagraphStyle(
        "title", parent=styles["Heading1"], alignment=1, spaceAfter=12)
    metaStyle = ParagraphStyle(
        "meta", parent=styles["Normal"], fontSize=10, textColor="#555555", spaceAfter=6)
    userStyle = ParagraphStyle(
        "user", parent=styles["Normal"], spaceBefore=6, spaceAfter=6)
    asstStyle = ParagraphStyle(
        "asst", parent=styles["Normal"], textColor="#1E3A8A", spaceBefore=6, spaceAfter=12)

    body.append(Paragraph("Relatório de Conversa", titleStyle))
    body.append(Paragraph(
        f"Gerado em: {datetime.now().strftime('%d/%m/%Y %H:%M:%S')}", metaStyle))
    body.append(Spacer(1, 0.2 * inch))

    if isinstance(docsState, dict) and docsState:
        body.append(
            Paragraph("<b>Documentos ativos (amostra):</b>", styles["Heading3"]))
        for name, content in docsState.items():
            preview = (content[:600] +
                       "...") if len(content) > 600 else content
            body.append(Paragraph(
                f"<b>{name}</b><br/>{preview.replace(chr(10), '<br/>')}", styles["Normal"]))
            body.append(Spacer(1, 0.1 * inch))
        body.append(PageBreak())

    body.append(Paragraph("<b>Conversa:</b>", styles["Heading3"]))
    if isinstance(history, list):
        for msg in history:
            role = msg.get("role")
            content = msg.get("content")
            if content is None:
                continue
            content = str(content).replace("\n", "<br/>")
            if role == "user":
                body.append(Paragraph(f"<b>Usuário</b>: {content}", userStyle))
            elif role == "assistant":
                body.append(
                    Paragraph(f"<b>Assistente</b>: {content}", asstStyle))
            else:
                body.append(
                    Paragraph(f"<b>{role or 'mensagem'}</b>: {content}", styles["Normal"]))

    doc.build(body)
    return outPath

# ======================== Vertex-only generation ========================


def _messagesToTranscript(messages):
    lines = []
    for m in messages:
        role = (m.get("role") or "").lower()
        content = m.get("content")
        if isinstance(content, (dict, list)):
            content = str(content)
        tag = {"system": "SYSTEM", "user": "USER",
               "assistant": "ASSISTANT"}.get(role, role.upper() or "MSG")
        lines.append(f"{tag}: {content}")
    return "\n\n".join(lines)


def _streamFromVertex(messages, cfg):
    model = _vertex_init_or_raise(cfg)
    transcript = _messagesToTranscript(messages)
    resp = model.generate_content(transcript, stream=True)
    for chunk in resp:
        try:
            cands = getattr(chunk, "candidates", None) or []
            if not cands:
                continue
            parts = getattr(cands[0].content, "parts", []) or []
            for p in parts:
                text = getattr(p, "text", None)
                if text:
                    yield text
        except Exception:
            text = getattr(chunk, "text", None)
            if text:
                yield text

# ======================== Chat callbacks ========================


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
        header_msg += f"\n\n> ✅ Credenciais Vertex carregadas de: `{VERTEX_CFG.get('source_path')}` | Projeto: `{VERTEX_CFG.get('project')}` | Região: `{VERTEX_CFG.get('location')}` | Modelo: `{VERTEX_CFG.get('model')}`"
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

    # Home (sem qualquer UI de config)
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
                    f"**Provedor:** `vertex`  |  **Projeto:** `{(VERTEX_CFG or {}).get('project', '?')}`  |  **Modelo:** `{(VERTEX_CFG or {}).get('model', '?')}`")
                chatbot = gr.Chatbot(label="Chat", type="messages", height=420)
                with gr.Row():
                    clearBtn = gr.Button("Limpar chat")
                    exportBtn = gr.Button("Exportar conversa (PDF)")
                chatInput = gr.MultimodalTextbox(
                    show_label=False, placeholder="Digite sua mensagem ou envie um PDF...", sources=["upload"], interactive=True)

        # Validar campos
        for c in (assunto, estilo, detalhamento, objetivo):
            c.change(validateFields, inputs=[
                     assunto, estilo, detalhamento, objetivo], outputs=submitBtn)

        submitBtn.click(updateCustomScript, inputs=[assunto, estilo, detalhamento, objetivo, extras, scriptState], outputs=scriptState).then(
            lambda: (gr.update(visible=False), gr.update(visible=True)),
            inputs=None, outputs=[configCol, chatCol]
        )

        reconfigureBtn.click(lambda: (gr.update(visible=True), gr.update(
            visible=False), []), inputs=None, outputs=[configCol, chatCol, chatbot])

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

    # Navigation (login -> home direto)
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
