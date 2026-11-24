"""Storage for the lanes view: one SQLite file under the helm directory.

The registry stays the source of truth for conversations; this database is the
derived, queryable picture (status, trail of events, topics) that the lanes TUI
and the `lane` CLI both read. Every function commits for itself unless called
inside `transaction()`.
"""
import contextlib
import json
import os
import sqlite3
import time

TRAIL_LEN = 8
LOG_LEN = 30
TITLE_MAX = 90
DETAIL_MAX = 400

SCHEMA = """
CREATE TABLE IF NOT EXISTS lanes (
  id TEXT PRIMARY KEY,
  kind TEXT NOT NULL,
  title TEXT NOT NULL,
  parent TEXT,
  topic TEXT,
  topic_source TEXT,
  source TEXT,
  created INTEGER, last_activity INTEGER,
  status TEXT,
  status_detail TEXT,
  stale_since INTEGER,
  archived_at INTEGER DEFAULT 0,
  goal TEXT,
  goal_source TEXT,
  recap TEXT,
  recap_at INTEGER DEFAULT 0
);
CREATE TABLE IF NOT EXISTS events (
  id INTEGER PRIMARY KEY,
  lane TEXT NOT NULL REFERENCES lanes(id),
  at INTEGER NOT NULL,
  kind TEXT NOT NULL,
  title TEXT NOT NULL,
  detail TEXT,
  dedupe TEXT UNIQUE
);
CREATE INDEX IF NOT EXISTS events_lane_at ON events(lane, at);
CREATE TABLE IF NOT EXISTS meta (k TEXT PRIMARY KEY, v TEXT);
"""

LANE_COLUMNS = (
    "id", "kind", "title", "parent", "topic", "topic_source", "source",
    "created", "last_activity", "status", "status_detail", "stale_since",
    "archived_at", "goal", "goal_source", "recap", "recap_at",
)


def helm_dir():
    return os.environ.get("LANES_STATE_DIR") or os.path.expanduser("~/.claude/helm")


def db_path():
    return os.environ.get("LANES_DB") or os.path.join(helm_dir(), "helm.db")


def connect(path=None):
    path = path or db_path()
    os.makedirs(os.path.dirname(os.path.abspath(path)), exist_ok=True)
    # Autocommit: explicit BEGIN/COMMIT only through transaction(), so a
    # reader thread never holds a write lock it did not ask for.
    con = sqlite3.connect(path, timeout=3, isolation_level=None)
    con.row_factory = sqlite3.Row
    con.execute("PRAGMA journal_mode=WAL")
    con.execute("PRAGMA busy_timeout=3000")
    con.execute("PRAGMA foreign_keys=ON")
    con.executescript(SCHEMA)
    _add_missing_columns(con)
    return con


# Columns added after a db was first created; CREATE TABLE IF NOT EXISTS
# leaves an older table as it was.
ADDED_COLUMNS = (("goal", "TEXT"), ("goal_source", "TEXT"), ("recap", "TEXT"),
                 ("recap_at", "INTEGER DEFAULT 0"))


def _add_missing_columns(con):
    have = {r["name"] for r in con.execute("PRAGMA table_info(lanes)")}
    for name, decl in ADDED_COLUMNS:
        if name not in have:
            try:
                con.execute(f"ALTER TABLE lanes ADD COLUMN {name} {decl}")
            except sqlite3.OperationalError:
                pass  # another process added it first


def set_brief(con, id, goal=None, recap=None, at=None):
    """The big picture (goal) and where it stands (recap), set by an agent or
    the user. A set goal sticks; a recap holds until the conversation speaks."""
    if goal:
        con.execute("UPDATE lanes SET goal=?, goal_source='you' WHERE id=?",
                    (goal, id))
    if recap:
        con.execute("UPDATE lanes SET recap=?, recap_at=? WHERE id=?",
                    (recap, int(at or time.time()), id))


@contextlib.contextmanager
def transaction(con):
    """One write lock for a whole sync, instead of a commit per row."""
    con.execute("BEGIN IMMEDIATE")
    try:
        yield con
    except BaseException:
        con.execute("ROLLBACK")
        raise
    con.execute("COMMIT")


def upsert_lane(con, force_topic=False, **fields):
    lane_id = fields.get("id")
    if not lane_id:
        raise ValueError("upsert_lane needs an id")
    unknown = set(fields) - set(LANE_COLUMNS)
    if unknown:
        raise ValueError("unknown lane fields: " + ", ".join(sorted(unknown)))
    if not force_topic and _topic_is_pinned(con, lane_id):
        fields.pop("topic", None)
        fields.pop("topic_source", None)
    cols = list(fields)
    marks = ", ".join("?" for _ in cols)
    updates = ", ".join(f"{c}=excluded.{c}" for c in cols if c != "id")
    sql = f"INSERT INTO lanes ({', '.join(cols)}) VALUES ({marks})"
    sql += f" ON CONFLICT(id) DO UPDATE SET {updates}" if updates else \
        " ON CONFLICT(id) DO NOTHING"
    con.execute(sql, [fields[c] for c in cols])


def _topic_is_pinned(con, lane_id):
    row = con.execute(
        "SELECT topic_source FROM lanes WHERE id=?", (lane_id,)).fetchone()
    return bool(row) and row["topic_source"] == "you"


def add_event(con, lane, at, kind, title, detail=None, dedupe=None):
    cur = con.execute(
        "INSERT OR IGNORE INTO events (lane, at, kind, title, detail, dedupe)"
        " VALUES (?, ?, ?, ?, ?, ?)",
        (lane, int(at), kind, _clip(title, TITLE_MAX),
         _clip(detail, DETAIL_MAX), dedupe))
    return cur.rowcount > 0


def _clip(text, limit):
    if text is None:
        return None
    text = " ".join(str(text).split())
    return text if len(text) <= limit else text[:limit - 1] + "…"


def _event_dicts(rows):
    return [dict(r) for r in rows]


def _trail(con, lane_id):
    rows = con.execute(
        "SELECT * FROM events WHERE lane=? ORDER BY at DESC, id DESC LIMIT ?",
        (lane_id, TRAIL_LEN)).fetchall()
    return _event_dicts(reversed(rows))


def lanes(con, include_archived=False):
    sql = "SELECT * FROM lanes"
    if not include_archived:
        sql += " WHERE COALESCE(archived_at, 0) = 0"
    sql += " ORDER BY COALESCE(last_activity, 0) DESC, id"
    out = []
    for row in con.execute(sql).fetchall():
        item = dict(row)
        item["trail"] = _trail(con, item["id"])
        out.append(item)
    return out


def lane(con, id):
    row = con.execute("SELECT * FROM lanes WHERE id=?", (id,)).fetchone()
    if row is None:
        return None
    item = dict(row)
    item["trail"] = _trail(con, id)
    item["events"] = _event_dicts(con.execute(
        "SELECT * FROM events WHERE lane=? ORDER BY at DESC, id DESC LIMIT ?",
        (id, LOG_LEN)).fetchall())
    return item


def set_topic(con, id, topic, source="you"):
    con.execute(
        "UPDATE lanes SET topic=?, topic_source=? WHERE id=?",
        (topic, source, id))


def archive(con, id, at=None):
    con.execute(
        "UPDATE lanes SET archived_at=?, stale_since=NULL WHERE id=?",
        (int(at or time.time()), id))


def get_meta(con, k):
    row = con.execute("SELECT v FROM meta WHERE k=?", (k,)).fetchone()
    return row["v"] if row else None


def set_meta(con, k, v):
    con.execute(
        "INSERT INTO meta (k, v) VALUES (?, ?)"
        " ON CONFLICT(k) DO UPDATE SET v=excluded.v", (k, v))


def selection_path():
    return os.path.join(helm_dir(), "selected-lane")


def write_selection(lane_dict):
    record = {
        "id": lane_dict["id"], "kind": lane_dict["kind"],
        "title": lane_dict["title"], "status": lane_dict.get("status"),
        "at": int(time.time()),
    }
    path = selection_path()
    os.makedirs(os.path.dirname(path), exist_ok=True)
    tmp = f"{path}.tmp{os.getpid()}"
    with open(tmp, "w") as fh:
        json.dump(record, fh)
    os.replace(tmp, path)


def read_selection():
    try:
        with open(selection_path()) as fh:
            data = json.load(fh)
    except (OSError, ValueError):
        return None
    return data if isinstance(data, dict) else None
