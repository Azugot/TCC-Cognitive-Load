"""Compatibilidade legada para integrações com Supabase.

Preferir importar de ``services.supabase`` (admin, teacher, student, storage,
common). Este módulo mantém os nomes históricos para não quebrar clientes
existentes durante a transição.
"""

from services.supabase.admin import *  # noqa: F401,F403
from services.supabase.common import *  # noqa: F401,F403
from services.supabase.storage import *  # noqa: F401,F403
from services.supabase.student import *  # noqa: F401,F403
from services.supabase.teacher import *  # noqa: F401,F403

__all__ = []  # será preenchido abaixo

# Combina os __all__ exportados pelos submódulos, quando disponíveis.
for _module in (
    "services.supabase.common",
    "services.supabase.admin",
    "services.supabase.teacher",
    "services.supabase.student",
    "services.supabase.storage",
):
    module = __import__(_module, fromlist=["__all__"])
    exported = getattr(module, "__all__", None)
    if exported:
        __all__.extend(exported)
