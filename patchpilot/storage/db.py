"""Review history in SQLite: one row per review (full JSON payload) plus normalized findings.

The payload is the source of truth (load() round-trips a Review exactly); the findings
table exists so history can be queried without parsing JSON ("which rules keep coming
back?"). Saving the same review again (e.g. after fixes) replaces it.
"""

from __future__ import annotations

import sqlite3
from contextlib import closing
from dataclasses import dataclass
from pathlib import Path

from patchpilot import config
from patchpilot.models import Review

_SCHEMA = """
CREATE TABLE IF NOT EXISTS reviews (
    id           TEXT PRIMARY KEY,
    created_at   TEXT NOT NULL,
    source_name  TEXT NOT NULL,
    mode         TEXT NOT NULL,
    loc          INTEGER NOT NULL,
    n_findings   INTEGER NOT NULL,
    overall      REAL NOT NULL,
    projected    REAL,
    n_proven     INTEGER NOT NULL DEFAULT 0,
    payload      TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS findings (
    review_id     TEXT NOT NULL REFERENCES reviews(id) ON DELETE CASCADE,
    finding_id    TEXT NOT NULL,
    file          TEXT NOT NULL,
    line          INTEGER NOT NULL,
    severity      TEXT NOT NULL,
    category      TEXT NOT NULL,
    rule_id       TEXT NOT NULL,
    evidence      TEXT NOT NULL,
    verification  TEXT NOT NULL,
    PRIMARY KEY (review_id, finding_id)
);
CREATE INDEX IF NOT EXISTS reviews_by_source ON reviews(source_name, created_at);
"""


@dataclass
class ReviewRow:
    id: str
    created_at: str
    source_name: str
    mode: str
    loc: int
    n_findings: int
    overall: float
    projected: float | None
    n_proven: int


def _connect(path: Path | None) -> sqlite3.Connection:
    path = Path(path or config.DB_PATH)
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(path)
    conn.execute("PRAGMA foreign_keys = ON")
    conn.executescript(_SCHEMA)
    return conn


def save(rev: Review, path: Path | None = None) -> None:
    proven = int(rev.metrics.get("fix_proven", 0))
    with closing(_connect(path)) as conn, conn:
        conn.execute("DELETE FROM reviews WHERE id = ?", (rev.id,))  # cascades to findings
        conn.execute(
            "INSERT INTO reviews VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (rev.id, rev.created_at.isoformat(), rev.source_name, rev.mode, rev.loc, len(rev.findings),
             rev.scores.overall, rev.metrics.get("projected_overall"), proven, rev.model_dump_json()),
        )
        conn.executemany(
            "INSERT OR REPLACE INTO findings VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
            [(rev.id, f.id, f.file, f.line, f.severity.value, f.category.value, f.rule_id,
              f.evidence_level.value, f.verification_status.value) for f in rev.findings],
        )


def list_reviews(limit: int = 50, source_name: str | None = None, path: Path | None = None) -> list[ReviewRow]:
    sql = "SELECT id, created_at, source_name, mode, loc, n_findings, overall, projected, n_proven FROM reviews"
    args: tuple = ()
    if source_name:
        sql += " WHERE source_name = ?"
        args = (source_name,)
    sql += " ORDER BY created_at DESC LIMIT ?"
    with closing(_connect(path)) as conn:
        return [ReviewRow(*row) for row in conn.execute(sql, (*args, limit))]


def load(review_id: str, path: Path | None = None) -> Review | None:
    with closing(_connect(path)) as conn:
        row = conn.execute("SELECT payload FROM reviews WHERE id = ?", (review_id,)).fetchone()
    return Review.model_validate_json(row[0]) if row else None


def recurring_rules(limit: int = 10, path: Path | None = None) -> list[tuple[str, int, int]]:
    """(rule_id, occurrences, reviews it appeared in), most frequent first."""
    with closing(_connect(path)) as conn:
        return list(conn.execute(
            "SELECT rule_id, COUNT(*), COUNT(DISTINCT review_id) FROM findings "
            "GROUP BY rule_id ORDER BY COUNT(*) DESC LIMIT ?", (limit,),
        ))
