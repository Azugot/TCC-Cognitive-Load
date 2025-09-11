# services/vertex_client.py
import os
import json

# Imports opcionais (mantêm comportamento do original)
try:
    import vertexai as _vertexai
    from vertexai.generative_models import GenerativeModel as _VertexGenerativeModel
    from google.oauth2.service_account import Credentials as _GcpCredentials
except Exception:
    _vertexai = None
    _VertexGenerativeModel = None
    _GcpCredentials = None

# Locais padrão onde o SA pode existir (mesmo do app original)
DEFAULT_LOCATIONS = [
    "./acquired-router-470921-a3-a19e3f67c416.json"
]


def _load_vertex_cfg_from_disk():
    """Carrega o service account JSON do disco, sem UI. Retorna dict cfg ou lança exceção."""
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
