# services/vertex_client.py
import os
import json
import re

try:
    import vertexai as _vertexai
    from vertexai.generative_models import GenerativeModel as _VertexGenerativeModel
    from google.oauth2.service_account import Credentials as _GcpCredentials
except Exception:
    _vertexai = None
    _VertexGenerativeModel = None
    _GcpCredentials = None

DEFAULT_LOCATIONS = [
    "./acquired-router-470921-a3-a19e3f67c416.json"
]


def _load_vertex_cfg_from_disk():
    preset_path = os.environ.get("VERTEX_SA_PATH", "").strip()
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


def _streamFromVertex(messages, cfg, adv):
    model = _vertex_init_or_raise(cfg)
    transcript = _messagesToTranscript(messages)
    gen_cfg = {}
    try:
        gen_cfg = {
            "temperature": float(adv.get("temperature", 0.7)),
            "top_p": float(adv.get("top_p", 0.95)),
            "top_k": int(adv.get("top_k", 40)),
            "max_output_tokens": int(adv.get("max_tokens", 1024)),
        }
    except Exception:
        gen_cfg = {}
    resp = model.generate_content(
        transcript, generation_config=gen_cfg, stream=True)
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


def _collect_response_text(response):
    """Extract plain text from a non-streaming Vertex response."""

    if response is None:
        return ""

    direct_text = getattr(response, "text", None)
    if isinstance(direct_text, str) and direct_text.strip():
        return direct_text

    candidates = getattr(response, "candidates", None) or []
    for candidate in candidates:
        content = getattr(candidate, "content", None)
        if content is None:
            continue
        parts = getattr(content, "parts", None) or []
        texts = [getattr(part, "text", None) for part in parts]
        combined = " ".join(t for t in texts if isinstance(t, str) and t.strip())
        if combined.strip():
            return combined

    try:
        serialized = str(response)
    except Exception:  # pragma: no cover - fallback defensivo
        serialized = ""
    return serialized


def _limit_phrases(text: str, max_phrases: int = 2) -> str:
    """Reduce the generated summary to the requested number of phrases."""

    if not text:
        return ""

    normalized = re.sub(r"\s+", " ", text).strip()
    if not normalized:
        return ""

    segments = re.findall(r"[^.!?\n]+[.!?]?", normalized)
    phrases = []
    for segment in segments:
        cleaned = segment.strip(" -•;:\u2022")
        if not cleaned:
            continue
        phrases.append(cleaned)
        if len(phrases) >= max_phrases:
            break

    if not phrases:
        return normalized

    result = " ".join(phrases[:max_phrases])
    return result.strip()


def summarize_chat_history(messages, cfg, *, max_phrases: int = 2) -> str:
    """Summarize a chat transcript using Vertex in up to `max_phrases` phrases."""

    history = messages or []
    if not history:
        return ""

    transcript = _messagesToTranscript(history)
    if not transcript.strip():
        return ""

    model = _vertex_init_or_raise(cfg)
    prompt = (
        "Resuma a conversa a seguir em até duas frases curtas. "
        "Use linguagem objetiva, sem listas ou tópicos.\n\n"
        "Conversa:\n"
        f"{transcript}"
    )

    generation_config = {
        "temperature": 0.2,
        "top_p": 0.9,
        "max_output_tokens": 256,
    }

    response = model.generate_content(prompt, generation_config=generation_config)
    summary = _collect_response_text(response)
    limited = _limit_phrases(summary, max_phrases=max_phrases)
    return limited
