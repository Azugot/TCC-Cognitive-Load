-- Migration: add classroom_documents table for storing classroom assets
CREATE TABLE IF NOT EXISTS public.classroom_documents (
  id            uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  classroom_id  uuid NOT NULL REFERENCES public.classrooms(id) ON DELETE CASCADE,
  uploaded_by   uuid NOT NULL REFERENCES public.users(id) ON DELETE RESTRICT,
  file_name     text NOT NULL,
  storage_path  text NOT NULL,
  description   text,
  created_at    timestamptz NOT NULL DEFAULT now(),
  updated_at    timestamptz NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS ix_classroom_documents_classroom_id
  ON public.classroom_documents (classroom_id, created_at DESC);
