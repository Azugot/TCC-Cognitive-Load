-- =============================================================================
-- Base & tipos
-- =============================================================================
-- Supabase recomenda gen_random_uuid() (pgcrypto) para UUIDs
CREATE EXTENSION IF NOT EXISTS "pgcrypto";

-- Enums
DO $$
BEGIN
  IF NOT EXISTS (SELECT 1 FROM pg_type WHERE typname = 'role_type') THEN
    CREATE TYPE role_type AS ENUM ('student','teacher','admin');
  END IF;
END
$$;

-- =============================================================================
-- Tabelas
-- =============================================================================

-- 0) Users
CREATE TABLE IF NOT EXISTS public.users (
  id             uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  name           text NOT NULL,
  email          text NOT NULL UNIQUE,
  password_hash  text NOT NULL,
  role           role_type NOT NULL,
  created_at     timestamptz NOT NULL DEFAULT now()
);

-- 1) Classrooms e membros
CREATE TABLE IF NOT EXISTS public.classrooms (
  id            uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  name          text NOT NULL,
  description   text,
  theme_name    text NOT NULL,                          -- ex.: "Algoritmos e Estruturas de Dados"
  theme_config  jsonb,                                  -- parâmetros extras (temperatura, fontes, etc.)
  theme_locked  boolean NOT NULL DEFAULT true,          -- travado/imutável após criação/uso
  created_by    uuid NOT NULL REFERENCES public.users(id) ON DELETE RESTRICT,
  created_at    timestamptz NOT NULL DEFAULT now(),
  is_archived   boolean NOT NULL DEFAULT false
);

-- professores por sala (M:N)
CREATE TABLE IF NOT EXISTS public.classroom_teachers (
  classroom_id  uuid NOT NULL REFERENCES public.classrooms(id) ON DELETE CASCADE,
  teacher_id    uuid NOT NULL REFERENCES public.users(id)       ON DELETE CASCADE,
  added_at      timestamptz NOT NULL DEFAULT now(),
  role_label    text,  -- opcional: 'owner' | 'co_teacher' (texto livre c/ CHECK, se quiser)
  CONSTRAINT pk_classroom_teachers PRIMARY KEY (classroom_id, teacher_id)
);

-- alunos por sala (M:N)
CREATE TABLE IF NOT EXISTS public.classroom_students (
  classroom_id  uuid NOT NULL REFERENCES public.classrooms(id) ON DELETE CASCADE,
  student_id    uuid NOT NULL REFERENCES public.users(id)       ON DELETE CASCADE,
  status        text NOT NULL DEFAULT 'active',                 -- 'active' | 'invited' | 'removed'
  joined_at     timestamptz NOT NULL DEFAULT now(),
  CONSTRAINT pk_classroom_students PRIMARY KEY (classroom_id, student_id),
  CONSTRAINT chk_classroom_students_status CHECK (status IN ('active','invited','removed'))
);

-- 2) Subtemas pré-definidos por sala
CREATE TABLE IF NOT EXISTS public.classroom_subjects (
  id            uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  classroom_id  uuid NOT NULL REFERENCES public.classrooms(id) ON DELETE CASCADE,
  name          text NOT NULL,
  is_active     boolean NOT NULL DEFAULT true,
  created_by    uuid NOT NULL REFERENCES public.users(id) ON DELETE RESTRICT,
  created_at    timestamptz NOT NULL DEFAULT now()
);

-- Único por sala (case-insensitive)
CREATE UNIQUE INDEX IF NOT EXISTS uq_classroom_subjects_cid_name
  ON public.classroom_subjects (classroom_id, lower(name));

-- 3) Chats (apenas dentro de sala) e avaliações
CREATE TABLE IF NOT EXISTS public.chats (
  id                uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  student_id        uuid NOT NULL REFERENCES public.users(id)       ON DELETE RESTRICT,
  classroom_id      uuid NOT NULL REFERENCES public.classrooms(id)  ON DELETE CASCADE,
  subject_id        uuid REFERENCES public.classroom_subjects(id)    ON DELETE SET NULL,
  subject_free_text text,
  topic_source      text NOT NULL,
  content           jsonb,     -- histórico resumido/trechos/refs
  summary           text,
  started_at        timestamptz NOT NULL DEFAULT now(),
  ended_at          timestamptz
);

-- Avaliações de chat (por professor)
CREATE TABLE IF NOT EXISTS public.chat_evaluations (
  id            uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  chat_id       uuid NOT NULL REFERENCES public.chats(id)   ON DELETE CASCADE,
  evaluator_id  uuid NOT NULL REFERENCES public.users(id)   ON DELETE RESTRICT,
  overall_score numeric(5,2) NOT NULL,        -- 0..100 (ajuste conforme sua escala)
  comments      text        NOT NULL,
  created_at    timestamptz NOT NULL DEFAULT now()
);

CREATE TABLE IF NOT EXISTS public.automated_chat_evaluations (
  id              uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  chat_id         uuid NOT NULL REFERENCES public.chats(id) ON DELETE CASCADE,
  bot_evaluations jsonb NOT NULL,
  created_at      timestamptz NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS idx_automated_chat_evals_chat
  ON public.automated_chat_evaluations (chat_id, created_at DESC);

-- (Opcional) chat_messages
-- CREATE TABLE IF NOT EXISTS public.chat_messages (
--   id         uuid PRIMARY KEY DEFAULT gen_random_uuid(),
--   chat_id    uuid NOT NULL REFERENCES public.chats(id) ON DELETE CASCADE,
--   sender     text NOT NULL CHECK (sender IN ('student','assistant')),
--   content    text NOT NULL,
--   created_at timestamptz NOT NULL DEFAULT now(),
--   token_count int
-- );

-- 4) Progresso (séries temporais e notas)
CREATE TABLE IF NOT EXISTS public.progress_scores (
  id                uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  student_id        uuid NOT NULL REFERENCES public.users(id)       ON DELETE RESTRICT,
  classroom_id      uuid NOT NULL REFERENCES public.classrooms(id)  ON DELETE CASCADE,
  subject_id        uuid REFERENCES public.classroom_subjects(id)    ON DELETE SET NULL,
  subject_free_text text,
  topic_source      text NOT NULL,
  metric            text NOT NULL,           -- ex.: 'overall','logica','ponteiros','poo'
  score             numeric(5,2) NOT NULL,   -- 0..100 (ajuste a escala)
  recorded_at       timestamptz NOT NULL DEFAULT now()
);

-- Preferências do aluno (opcional)
CREATE TABLE IF NOT EXISTS public.student_subject_preferences (
  id            uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  student_id    uuid NOT NULL REFERENCES public.users(id)       ON DELETE CASCADE,
  classroom_id  uuid NOT NULL REFERENCES public.classrooms(id)  ON DELETE CASCADE,
  subject_id    uuid REFERENCES public.classroom_subjects(id)    ON DELETE CASCADE,
  free_text     text,
  priority      int
);

-- Pelo menos uma fonte de assunto (predefinido OU texto livre), mas não ambos
ALTER TABLE public.student_subject_preferences
  ADD CONSTRAINT chk_ssp_source_oneof
  CHECK (
    (subject_id IS NOT NULL AND (free_text IS NULL OR length(trim(free_text)) = 0))
    OR
    (subject_id IS NULL     AND free_text IS NOT NULL AND length(trim(free_text)) > 0)
  );

-- Evitar duplicatas por aluno/sala/assunto
CREATE UNIQUE INDEX IF NOT EXISTS uq_ssp_student_classroom_subject
  ON public.student_subject_preferences (student_id, classroom_id, subject_id)
  WHERE subject_id IS NOT NULL;

CREATE UNIQUE INDEX IF NOT EXISTS uq_ssp_student_classroom_freetext
  ON public.student_subject_preferences (student_id, classroom_id, lower(free_text))
  WHERE free_text IS NOT NULL;

-- 5) Anexos (opcional)
CREATE TABLE IF NOT EXISTS public.attachments (
  id           uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  owner_id     uuid NOT NULL REFERENCES public.users(id) ON DELETE CASCADE,
  scope        text NOT NULL CHECK (scope IN ('chat','evaluation')),
  chat_id      uuid REFERENCES public.chats(id)            ON DELETE CASCADE,
  evaluation_id uuid REFERENCES public.chat_evaluations(id) ON DELETE CASCADE,
  storage_path text NOT NULL,
  meta         jsonb,
  created_at   timestamptz NOT NULL DEFAULT now()
);

-- Coerência com o escopo
ALTER TABLE public.attachments
  ADD CONSTRAINT chk_attachments_scope_link
  CHECK (
    (scope = 'chat'      AND chat_id IS NOT NULL AND evaluation_id IS NULL)
    OR
    (scope = 'evaluation' AND evaluation_id IS NOT NULL AND chat_id IS NULL)
  );

-- =============================================================================
-- Índices recomendados de performance
-- =============================================================================

-- Classrooms: criador
CREATE INDEX IF NOT EXISTS ix_classrooms_created_by ON public.classrooms (created_by);

-- Classroom subjects: busca por sala
CREATE INDEX IF NOT EXISTS ix_classroom_subjects_cid ON public.classroom_subjects (classroom_id);

-- Chats: principais filtragens/gráficos
CREATE INDEX IF NOT EXISTS ix_chats_student_classroom_started
  ON public.chats (student_id, classroom_id, started_at);

-- Progress: séries temporais
CREATE INDEX IF NOT EXISTS ix_progress_student_classroom_recorded
  ON public.progress_scores (student_id, classroom_id, recorded_at);

-- Avaliações: por chat e data
CREATE INDEX IF NOT EXISTS ix_chat_eval_chat_created
  ON public.chat_evaluations (chat_id, created_at);

-- Members quick lookups
CREATE INDEX IF NOT EXISTS ix_ct_teachers_by_teacher ON public.classroom_teachers (teacher_id);
CREATE INDEX IF NOT EXISTS ix_cs_students_by_student ON public.classroom_students (student_id);

-- Filtro de texto livre (só crie se realmente precisar de busca textual frequente)
-- CREATE INDEX IF NOT EXISTS gin_chats_subject_free_text
--   ON public.chats USING gin (to_tsvector('simple', coalesce(subject_free_text, '')));
-- CREATE INDEX IF NOT EXISTS gin_progress_subject_free_text
--   ON public.progress_scores USING gin (to_tsvector('simple', coalesce(subject_free_text, '')));

-- =============================================================================
-- Comentários úteis (opcional)
-- =============================================================================
COMMENT ON TYPE role_type IS 'Perfis de usuário: student, teacher, admin (admin acumula permissões de ambos).';
COMMENT ON COLUMN public.users.role IS 'student | teacher | admin';

COMMENT ON COLUMN public.classrooms.theme_locked IS 'Quando true, o tema não pode mais ser alterado.';

COMMENT ON COLUMN public.chats.topic_source IS 'Tema associado ao chat (nome da sala ou texto informado pelo aluno).';
COMMENT ON COLUMN public.progress_scores.topic_source IS 'Tema associado ao registro de progresso (sala ou texto livre).';
