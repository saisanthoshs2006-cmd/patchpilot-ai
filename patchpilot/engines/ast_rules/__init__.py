"""Importing the rule modules registers their rules in base.RULES."""

from patchpilot.engines.ast_rules import maintainability, performance, reliability, security  # noqa: F401
from patchpilot.engines.ast_rules.base import RULES

__all__ = ["RULES"]
