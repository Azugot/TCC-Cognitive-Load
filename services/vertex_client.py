# services/vertex_client.py
import os
import json
import re
from typing import List, Optional

try:
    import vertexai as _vertexai
    from vertexai.generative_models import GenerativeModel as _VertexGenerativeModel
    from google.oauth2.service_account import Credentials as _GcpCredentials
except Exception:
    _vertexai = None
    _VertexGenerativeModel = None
    _GcpCredentials = None

def _load_vertex_cfg_from_disk():
    try:
        from app.config import VERTEX_SERVICE_ACCOUNT as sa
    except Exception as e:
        raise RuntimeError(f"Falha ao importar credenciais de config.py: {e}")

    project = sa.get("project_id")
    if not project:
        raise ValueError("Campo 'project_id' ausente no JSON.")

    if not sa.get("private_key") or not sa.get("client_email"):
        raise ValueError(
            "Campos 'private_key' e 'client_email' são obrigatórios.")

    location = os.environ.get("GOOGLE_CLOUD_LOCATION", "us-central1")
    model = os.environ.get("VERTEX_MODEL", "gemini-2.5-flash")

    return {
        "project": project,
        "location": location,
        "model": model,
        "serviceAccount": sa,
        "source_path": "config.VERTEX_SERVICE_ACCOUNT",
    }


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
        combined = " ".join(
            t for t in texts if isinstance(t, str) and t.strip())
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
        "max_output_tokens": 4096,
    }

    response = model.generate_content(
        prompt, generation_config=generation_config)
    summary = _collect_response_text(response)
    limited = _limit_phrases(summary, max_phrases=max_phrases)
    return limited


def generate_chat_evaluation(
    transcript: str,
    cfg,
    *,
    subjects: Optional[List[str]] = None,
) -> str:
    """Generate a qualitative evaluation about the chat using Vertex AI."""

    if not transcript or not transcript.strip():
        return ""

    subject_text = ""
    if isinstance(subjects, (list, tuple, set)):
        normalized = [str(item).strip()
                      for item in subjects if str(item).strip()]
        subject_text = ", ".join(normalized)
    elif isinstance(subjects, str):
        subject_text = subjects.strip()

    if not subject_text:
        subject_text = "the discussed subjects"

    model = _vertex_init_or_raise(cfg)
    prompt = (
        f"""You are an intelligent educational evaluator.
			
			You will receive:
			- An ordered list of subjects covered in the chat.
			- A full transcript of the student's interactions, including chat messages and responses to learning activities.
			
			Your task is to analyze this content and assign a score from 0 to 100 for each topic, reflecting the student's current level of mastery.
			You should also write a small comment for each topic, explaining the score you assigned.
            Analyze the conversation transcript and provide a concise assessment of the student's progress on the subjects: {subject_text}. 
            Describe what they are doing well and where they struggle on the subjects studied, ignore any unused subject in scoring and detailing.
            The overall score should only take into account the studied subjects.
			
            Return the scores in the same order as the subjects.
			
			Scoring criteria:
			- 0: The student has shown no interaction or understanding of the topic.
			- 10: The student has engaged minimally, with superficial or incorrect answers.
			- 30: The student has shown partial understanding, with some correct answers and relevant questions.
			- 50: The student demonstrates a reasonable understanding, with consistent interaction and mostly correct answers.
			- 75: The student shows solid mastery, makes insightful contributions, and answers accurately.
			- 100: The student shows full mastery of the topic, with deep understanding, confident reasoning, and high accuracy in answers.
			- You can assign any value between 0 and 100, not just the ones listed above.
			- Be conservative in your scoring. The closer the score is to 100, the harder it should be to achieve.
			"""
        "Respond strictly as JSON with the following fields: {\n  \"subjects\": [\n    {\n      \"subject\": \"<Subject title>\",\n      \"grade\": \"<number between 0 and 100>\",\n      \"comment\": \"<detailed feedback>\"\n    }\n  ],\n  \"overview\": \"<detailed overview of the entire conversation>\",\n  \"overallGrade\": \"<number between 0 and 100>\"\n}"
        "Do not add any additional commentary outside the JSON.\n\n"
        "Conversation transcript:\n"
        f"{transcript}"
        "Always answer in Brazilian Portuguese(PT-BR)"
    )

    generation_config = {
        "temperature": 0.1,
        "top_p": 0.9,
        "max_output_tokens": 32768,
    }

    response = model.generate_content(
        prompt, generation_config=generation_config)
    raw_output = _collect_response_text(response).strip()

    cleaned = raw_output.strip()

    # Remove unwanted wrappers like ```json ... ```
    cleaned = re.sub(r"^```(?:json)?", "", cleaned,
                     flags=re.IGNORECASE).strip()
    cleaned = re.sub(r"```$", "", cleaned).strip()

    parsed = None
    if cleaned:
        try:
            parsed = json.loads(cleaned)
        except Exception:
            raise ValueError("Invalid JSON format")

    if not isinstance(parsed, dict) or "subjects" not in parsed:
        raise ValueError(
            "Expected JSON with 'subjects', 'overview', and 'overallGrade'")

    result = {
        "subjects": [],
        "overview": parsed.get("overview") or "",
        "overallGrade": None,
        "raw": raw_output,
    }

    # Parse subjects
    if isinstance(parsed["subjects"], list):
        for subj in parsed["subjects"]:
            subject_title = subj.get("subject") or "Unknown"
            grade_raw = subj.get("grade")
            comment = subj.get("comment") or ""

            try:
                grade_value = float(
                    grade_raw) if grade_raw is not None else None
            except (TypeError, ValueError):
                grade_value = None

            if grade_value is not None:
                grade_value = max(0.0, min(100.0, grade_value))

            result["subjects"].append({
                "subject": subject_title,
                "grade": grade_value,
                "comment": comment.strip(),
            })

    # Parse overall grade
    try:
        overall_raw = parsed.get("overallGrade")
        result["overallGrade"] = float(
            overall_raw) if overall_raw is not None else None
    except (TypeError, ValueError):
        result["overallGrade"] = None

    if result["overallGrade"] is not None:
        result["overallGrade"] = max(0.0, min(100.0, result["overallGrade"]))

    return result
