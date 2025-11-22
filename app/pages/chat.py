"""Chat flows (personal studio) shared logic and UI helpers."""

from __future__ import annotations

import os
from dataclasses import dataclass

import gradio as gr

from services.docs import extractPdfText
from services.script_builder import buildCustomScript
from services.vertex_client import VERTEX_CFG, _streamFromVertex, _vertex_err

from app.utils import _mk_id, _now_ts


@dataclass
class StudioView:
    container: gr.Column
    config_column: gr.Column
    chat_column: gr.Column
    back_button: gr.Button
    chat_input: gr.MultimodalTextbox
    chatbot: gr.Chatbot
    clear_button: gr.Button


def addMessage(history, message, docsState, authState, currentChatId, chatsState):
    """Handle user submissions, ingest PDF files and append to state."""
    if not isinstance(docsState, dict):
        docsState = {}
    if not isinstance(chatsState, dict):
        chatsState = {}

    chat_id = currentChatId or _mk_id("chat")
    if chat_id not in chatsState:
        login = (authState or {}).get("username") or "anon"
        display_name = (authState or {}).get("full_name") or (authState or {}).get("display_name") or login
        role = (authState or {}).get("role", "aluno").lower()
        chatsState[chat_id] = {
            "id": chat_id,
            "owner": login,
            "role": role,
            "created_at": _now_ts(),
            "title": f"Chat de {display_name}",
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
                part = f"Documento ativo: {name} OK:"
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
        chatsState,
    )


def bot(history, docsState, scriptState, advState, currentChatId, chatsState):
    """Stream model responses and persist them into the chat map."""
    docsPreview = ""
    if isinstance(docsState, dict) and docsState:
        joined = []
        for name, content in docsState.items():
            preview = content[:2000]
            joined.append(f"Documento '{name}':\n{preview}")
        docsPreview = "\n\n".join(joined)

    systemMsg = {
        "role": "system",
        "content": f"{scriptState}\n\nDocumentos ativos (amostras):\n{docsPreview}",
    }
    messages = [systemMsg] + history
    history.append({"role": "assistant", "content": ""})

    try:
        adv = advState if isinstance(advState, dict) else {}
        for delta in _streamFromVertex(messages, VERTEX_CFG, adv):
            history[-1]["content"] += delta
            yield history
        if currentChatId and isinstance(chatsState, dict) and currentChatId in chatsState:
            chatsState[currentChatId]["messages"].append(
                {
                    "role": "assistant",
                    "content": history[-1]["content"],
                    "ts": _now_ts(),
                }
            )
            if not chatsState[currentChatId].get("title"):
                first_user_msg = next(
                    (m.get("content") for m in history if m.get("role") == "user"),
                    "",
                )
                if first_user_msg:
                    chatsState[currentChatId]["title"] = first_user_msg[:40]
    except Exception as e:
        history[-1]["content"] = f"[ERRO Vertex: {e}]"
        if currentChatId and isinstance(chatsState, dict) and currentChatId in chatsState:
            chatsState[currentChatId]["messages"].append(
                {
                    "role": "assistant",
                    "content": history[-1]["content"],
                    "ts": _now_ts(),
                }
            )
        yield history


def clearChat():
    return []


def updateCustomScript(assunto, estilo, detalhamento, objetivo, extras, scriptState):
    return buildCustomScript(
        assunto,
        estilo,
        detalhamento,
        objetivo,
        extras,
        subtemas=None,
        interesses=None,
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
    ok = all([assunto and assunto.strip(), estilo, detalhamento, objetivo and objetivo.strip()])
    return gr.Button(interactive=bool(ok))


def build_studio_page(
    *,
    blocks: gr.Blocks,
    script_state: gr.State,
    adv_state: gr.State,
    docs_state: gr.State,
    auth_state: gr.State,
    current_chat_id: gr.State,
    chats_state: gr.State,
) -> StudioView:
    """Create the personal chat (studio) interface."""
    with gr.Column(visible=False) as viewStudio:
        with gr.Row():
            gr.Markdown("## 🧩 Customização do Chat")
            btnBackHome = gr.Button("← Voltar à Home")
        with gr.Row():
            with gr.Column(scale=1) as configCol:
                assunto = gr.Textbox(label="1) Assunto", placeholder="Ex: Biologia marinha")
                estilo = gr.Radio(
                    label="2) Estilo de linguagem",
                    choices=["técnicas", "simples"],
                    value="simples",
                )
                detalhamento = gr.Radio(
                    label="3) Nível de detalhamento",
                    choices=["detalhadas", "curtas"],
                    value="detalhadas",
                )
                objetivo = gr.Textbox(
                    label="4) Objetivo",
                    placeholder="Ex: Preparar para uma prova",
                )
                extras = gr.Textbox(
                    label="5) Preferências extras (opcional)",
                    placeholder="Ex: Evitar termos em inglês",
                )
                with gr.Tabs():
                    with gr.Tab("Opções avançadas"):
                        temperature = gr.Slider(0.0, 2.0, value=0.7, step=0.1, label="Temperatura")
                        topP = gr.Slider(0.0, 1.0, value=0.95, step=0.01, label="Top-p")
                        topK = gr.Slider(0, 50, value=40, step=1, label="Top-k")
                        maxTokens = gr.Slider(64, 16384, value=1024, step=32, label="Max tokens (saída)")
                        applyAdv = gr.Button("Aplicar opções avançadas")
                        gr.Markdown(
                            "**Notas sobre os hiperparâmetros**\n"
                            "¹ Temperatura: controla a aleatoriedade das respostas. Valores maiores geram respostas mais criativas, enquanto valores menores priorizam segurança e consistência.\n"
                            "² Top-p: define a fração acumulada de probabilidade considerada na geração. Reduzir o valor limita o vocabulário às palavras mais prováveis.\n"
                            "³ Top-k: especifica quantas palavras mais prováveis podem ser escolhidas a cada passo. Valores pequenos tornam a saída mais previsível.\n"
                            "⁴ Max tokens (saída): limita o tamanho máximo da resposta do modelo. Aumentar o valor permite respostas mais longas."
                        )
                        applyAdv.click(
                            updateAdvOptions,
                            inputs=[temperature, topP, topK, maxTokens, adv_state],
                            outputs=adv_state,
                        )
                submitBtn = gr.Button("Aplicar Configuração", variant="primary", interactive=False)
                reconfigureBtn = gr.Button("Reconfigurar")
            with gr.Column(scale=2, visible=False) as chatCol:
                providerBadge = gr.Markdown(
                    f"**Provedor:** `vertex`  |  **Projeto:** `{(VERTEX_CFG or {}).get('project', '?')}`"
                    f"  |  **Modelo:** `{(VERTEX_CFG or {}).get('model', '?')}`"
                )
                chatbot = gr.Chatbot(label="Chat", type="messages", height=420)
                with gr.Row():
                    clearBtn = gr.Button("Limpar chat")
                backToConfigBtn = gr.Button("⬅️ Voltar para customização")
                allow_file_upload = bool(VERTEX_CFG and not _vertex_err)
                chat_placeholder = (
                    "Digite sua mensagem ou envie um PDF..."
                    if allow_file_upload
                    else "Digite sua mensagem..."
                )
                chat_sources = ["upload"] if allow_file_upload else []

                chatInput = gr.MultimodalTextbox(
                    show_label=False,
                    placeholder=chat_placeholder,
                    sources=chat_sources,
                    interactive=True,
                )

        for c in (assunto, estilo, detalhamento, objetivo):
            c.change(
                validateFields,
                inputs=[assunto, estilo, detalhamento, objetivo],
                outputs=submitBtn,
            )

        submitBtn.click(
            updateCustomScript,
            inputs=[assunto, estilo, detalhamento, objetivo, extras, script_state],
            outputs=script_state,
        ).then(
            lambda: (gr.update(visible=False), gr.update(visible=True)),
            inputs=None,
            outputs=[configCol, chatCol],
        )

        reconfigureBtn.click(
            lambda: (gr.update(visible=True), gr.update(visible=False), []),
            inputs=None,
            outputs=[configCol, chatCol, chatbot],
        )
        backToConfigBtn.click(
            lambda: (gr.update(visible=True), gr.update(visible=False)),
            inputs=None,
            outputs=[configCol, chatCol],
        )

        chatMsg = chatInput.submit(
            addMessage,
            inputs=[chatbot, chatInput, docs_state, auth_state, current_chat_id, chats_state],
            outputs=[chatbot, chatInput, docs_state, current_chat_id, chats_state],
        )
        botMsg = chatMsg.then(
            bot,
            inputs=[chatbot, docs_state, script_state, adv_state, current_chat_id, chats_state],
            outputs=chatbot,
        )
        botMsg.then(lambda: gr.update(interactive=True), outputs=chatInput)

        clearBtn.click(clearChat, outputs=chatbot)

    return StudioView(
        container=viewStudio,
        config_column=configCol,
        chat_column=chatCol,
        back_button=btnBackHome,
        chat_input=chatInput,
        chatbot=chatbot,
        clear_button=clearBtn,
    )
