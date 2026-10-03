"""Workflow identity travels to financial reservation without client-supplied headers."""

from contextvars import ContextVar

active_workflow: ContextVar[tuple | None] = ContextVar("active_workflow", default=None)
