"""Absolute research.db path + read-only connection for the analysis layer.

The skills (.claude/skills/*) run under Claude Code, whose CWD is NOT
guaranteed to be the project root — when driven via the Lark bridge the CWD is
the bridge workspace, one level above crypto-msgflow. A bare
``duckdb.connect("research.db")`` would then miss the DB (or silently create an
empty one). Resolve the path from THIS file's location so it works regardless
of CWD, and hand out read-only connections so the analysis layer can never
pollute the append-only store or contend with cron writers.
"""
from pathlib import Path

import duckdb

from collectors.config import get_config


def research_db_path() -> Path:
    """Absolute path to research.db, independent of the process CWD."""
    # collectors/dbpath.py -> project root is one dir up from collectors/.
    root = Path(__file__).resolve().parent.parent
    return root / get_config().database.path


def connect_ro() -> duckdb.DuckDBPyConnection:
    """Read-only connection to research.db. The only connection the analysis
    layer should ever open. Raises if the DB does not exist (fail loud, don't
    auto-create an empty one at the wrong path)."""
    path = research_db_path()
    if not path.exists():
        raise FileNotFoundError(
            f"research.db not found at {path} — has init_db.py run?"
        )
    return duckdb.connect(str(path), read_only=True)
