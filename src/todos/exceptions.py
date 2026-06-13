"""Hierarquia de exceções do servidor SEI.

Todas as subclasses carregam mensagem legível por humanos — nunca
exponha stack traces httpx ou strings técnicas diretamente ao agente.
"""


class SEIError(Exception):
    """Erro base do servidor SEI."""


class SEIAuthError(SEIError):
    """Sessão expirada, login recusado, 401/403."""


class SEINotFoundError(SEIError):
    """Processo ou documento não existe no SEI."""


class SEIPermissionError(SEIError):
    """Acesso negado — documento restrito/sigiloso sem credenciamento."""


class SEIConnectionError(SEIError):
    """Falha de rede, timeout, instância inacessível."""


class SEIParseError(SEIError):
    """HTML da resposta não tem a estrutura esperada."""


class SEIValidationError(SEIError):
    """Parâmetros inválidos detectados antes de qualquer chamada HTTP."""
