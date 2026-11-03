"""Turn pasted code, a .py upload or a .zip upload into a Workspace on disk.

Engines only ever see the Workspace copy, never the user's originals, so patching
and test runs can't damage anything.
"""

from __future__ import annotations

import io
import shutil
import tempfile
import zipfile
from dataclasses import dataclass, field
from pathlib import Path, PurePosixPath

from patchpilot import config
from patchpilot.ingestion.language import detect


class IngestionError(ValueError):
    """Input rejected (too big, unsafe path, nothing to review). Message is user-facing."""


@dataclass
class SourceFile:
    path: str  # relative, posix
    content: str
    language: str | None
    syntax_error: SyntaxError | None = None

    @property
    def lines(self) -> list[str]:
        return self.content.splitlines()

    @property
    def loc(self) -> int:
        return sum(1 for line in self.lines if line.strip() and not line.strip().startswith("#"))

    @property
    def reviewable(self) -> bool:
        return self.language == "python" and self.syntax_error is None

    @property
    def is_test(self) -> bool:
        p = PurePosixPath(self.path)
        return (
            p.name.startswith("test_") or p.name.endswith("_test.py") or p.name == "conftest.py"
            or any(part in ("tests", "test") for part in p.parts[:-1])
        )


@dataclass
class Workspace:
    root: Path
    source_name: str
    files: list[SourceFile] = field(default_factory=list)
    skipped: list[str] = field(default_factory=list)  # unsupported files, reported to the user

    @property
    def python_files(self) -> list[SourceFile]:
        return [f for f in self.files if f.language == "python"]

    @property
    def reviewable_files(self) -> list[SourceFile]:
        return [f for f in self.files if f.reviewable]

    @property
    def source_files(self) -> list[SourceFile]:
        """What the engines review: reviewable non-test files. Tests are inputs to verification,
        not review targets (they're full of asserts and sys.path hacks that would just be noise).
        If the upload is *only* tests, review them anyway."""
        non_test = [f for f in self.reviewable_files if not f.is_test]
        return non_test or self.reviewable_files

    @property
    def loc(self) -> int:
        return sum(f.loc for f in self.source_files)

    def get(self, path: str) -> SourceFile | None:
        return next((f for f in self.files if f.path == path), None)

    def abspath(self, rel: str) -> Path:
        return self.root / rel

    def copy(self) -> Workspace:
        """Independent on-disk clone, used to apply a patch without touching the original."""
        new_root = Path(tempfile.mkdtemp(prefix="patchpilot-"))
        shutil.copytree(self.root, new_root, dirs_exist_ok=True)
        files = [SourceFile(f.path, f.content, f.language, f.syntax_error) for f in self.files]
        return Workspace(new_root, self.source_name, files, list(self.skipped))

    def write(self, rel: str, content: str) -> None:
        self.abspath(rel).write_text(content, encoding="utf-8")
        src = self.get(rel)
        if src is not None:
            d = detect(rel, content)
            src.content, src.language, src.syntax_error = content, d.language, d.syntax_error

    def cleanup(self) -> None:
        shutil.rmtree(self.root, ignore_errors=True)


def _safe_rel_path(name: str) -> str | None:
    """Reject absolute paths and '..' (zip-slip). Returns a normalized posix path or None."""
    p = PurePosixPath(name.replace("\\", "/"))
    if p.is_absolute() or ".." in p.parts or not p.parts:
        return None
    if any(part in config.IGNORED_DIR_NAMES for part in p.parts) or p.name in config.IGNORED_FILE_NAMES:
        return None
    return str(p)


def _build(source_name: str, entries: dict[str, bytes]) -> Workspace:
    if not entries:
        raise IngestionError("No files found to review.")
    if sum(len(b) for b in entries.values()) > config.MAX_TOTAL_BYTES:
        raise IngestionError(f"Upload exceeds {config.MAX_TOTAL_BYTES // 1000} KB limit.")

    root = Path(tempfile.mkdtemp(prefix="patchpilot-"))
    ws = Workspace(root=root, source_name=source_name)
    for rel, raw in sorted(entries.items()):
        # Non-Python files (fixtures, configs) are still copied so tests can use them.
        dest = root / rel
        dest.parent.mkdir(parents=True, exist_ok=True)
        dest.write_bytes(raw)
        if PurePosixPath(rel).suffix not in config.SUPPORTED_EXTENSIONS:
            ws.skipped.append(rel)
            continue
        if len(raw) > config.MAX_FILE_BYTES:
            ws.skipped.append(rel)
            continue
        try:
            text = raw.decode("utf-8")
        except UnicodeDecodeError:
            ws.skipped.append(rel)
            continue
        d = detect(rel, text)
        ws.files.append(SourceFile(rel, text, d.language, d.syntax_error))

    if not ws.python_files:
        ws.cleanup()
        raise IngestionError("No Python files found. PatchPilot currently reviews Python only.")
    if len(ws.python_files) > config.MAX_FILES:
        ws.cleanup()
        raise IngestionError(f"Too many Python files ({len(ws.python_files)}); limit is {config.MAX_FILES}.")
    return ws


def from_text(code: str, filename: str = "snippet.py") -> Workspace:
    rel = _safe_rel_path(filename) or "snippet.py"
    return _build(filename, {rel: code.encode("utf-8")})


def from_upload(filename: str, data: bytes) -> Workspace:
    """Accepts a single source file or a .zip archive."""
    if not filename.lower().endswith(".zip"):
        rel = _safe_rel_path(PurePosixPath(filename).name)
        if rel is None:
            raise IngestionError("Invalid file name.")
        return _build(filename, {rel: data})

    try:
        zf = zipfile.ZipFile(io.BytesIO(data))
    except zipfile.BadZipFile as exc:
        raise IngestionError("Not a valid .zip archive.") from exc

    entries: dict[str, bytes] = {}
    total = 0
    for info in zf.infolist():
        if info.is_dir():
            continue
        rel = _safe_rel_path(info.filename)
        if rel is None:
            continue
        total += info.file_size  # check declared size before reading: zip-bomb guard
        if total > config.MAX_TOTAL_BYTES:
            raise IngestionError(f"Archive exceeds {config.MAX_TOTAL_BYTES // 1000} KB uncompressed.")
        entries[rel] = zf.read(info)
    return _build(filename, _strip_common_root(entries))


def from_directory(path: str | Path) -> Workspace:
    """Used by the CLI and the benchmark runner."""
    base = Path(path)
    if base.is_file():
        return from_upload(base.name, base.read_bytes())
    entries: dict[str, bytes] = {}
    for p in base.rglob("*"):
        if p.is_file():
            rel = _safe_rel_path(p.relative_to(base).as_posix())
            if rel is not None:
                entries[rel] = p.read_bytes()
    return _build(base.name, entries)


def _strip_common_root(entries: dict[str, bytes]) -> dict[str, bytes]:
    """'project/src/a.py' -> 'src/a.py' when every entry shares the 'project/' prefix."""
    tops = {PurePosixPath(k).parts[0] for k in entries}
    if len(tops) == 1 and all(len(PurePosixPath(k).parts) > 1 for k in entries):
        return {str(PurePosixPath(*PurePosixPath(k).parts[1:])): v for k, v in entries.items()}
    return entries
