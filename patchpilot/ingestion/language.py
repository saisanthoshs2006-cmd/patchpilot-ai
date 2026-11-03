"""Language detection. Python only, and we say so instead of pretending otherwise."""

from __future__ import annotations

import ast
from dataclasses import dataclass
from pathlib import PurePosixPath

from patchpilot import config


@dataclass(frozen=True)
class Detection:
    language: str | None  # "python" or None if unsupported
    syntax_error: SyntaxError | None = None

    @property
    def supported(self) -> bool:
        return self.language is not None


def detect(path: str, content: str) -> Detection:
    if PurePosixPath(path).suffix not in config.SUPPORTED_EXTENSIONS:
        return Detection(language=None)
    try:
        ast.parse(content, filename=path)
    except SyntaxError as exc:
        return Detection(language="python", syntax_error=exc)
    return Detection(language="python")
