"""Pacote com integrações segmentadas do Supabase.

Perfis: usado por camadas de serviço internas; as regras de autorização
são aplicadas nos módulos especializados (admin, teacher, student e storage).
"""

from . import admin, common, storage, student, teacher

__all__ = ["admin", "common", "storage", "student", "teacher"]
