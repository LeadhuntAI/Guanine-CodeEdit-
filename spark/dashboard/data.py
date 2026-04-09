"""Read-only data access for the Spark dashboard.

Queries spark.db and parses jcodemunch.log — never writes.
"""

from __future__ import annotations

import json
import os
import re
import sqlite3
from datetime import datetime, timezone
from typing import Any, Optional


# ---------------------------------------------------------------------------
# Database helpers
# ---------------------------------------------------------------------------

def _md_to_html(md: str) -> str:
    """Convert markdown to basic HTML. Handles headers, tables, lists, code, bold/italic."""
    import html as _html
    lines = md.split("\n")
    out: list[str] = []
    in_table = False
    in_code = False
    in_list = False

    for line in lines:
        # Fenced code blocks
        if line.strip().startswith("```"):
            if in_code:
                out.append("</code></pre>")
                in_code = False
            else:
                out.append("<pre><code>")
                in_code = True
            continue
        if in_code:
            out.append(_html.escape(line))
            continue

        stripped = line.strip()

        # Empty line — close list if open
        if not stripped:
            if in_list:
                out.append("</ul>")
                in_list = False
            if in_table:
                out.append("</tbody></table>")
                in_table = False
            out.append("")
            continue

        # Blockquote
        if stripped.startswith("> "):
            out.append(f'<div style="color: var(--text-dim); border-left: 3px solid var(--border); padding-left: 12px; font-size: 12px;">{_html.escape(stripped[2:])}</div>')
            continue

        # Headers
        if stripped.startswith("# "):
            out.append(f"<h1>{_html.escape(stripped[2:])}</h1>")
            continue
        if stripped.startswith("## "):
            out.append(f"<h2>{_html.escape(stripped[3:])}</h2>")
            continue
        if stripped.startswith("### "):
            out.append(f"<h3>{_html.escape(stripped[4:])}</h3>")
            continue

        # Table rows
        if "|" in stripped and stripped.startswith("|"):
            cells = [c.strip() for c in stripped.split("|")[1:-1]]
            # Skip separator rows (|---|---|)
            if all(c.replace("-", "").replace(":", "") == "" for c in cells):
                continue
            if not in_table:
                out.append('<table><thead><tr>')
                for c in cells:
                    out.append(f"<th>{_html.escape(c)}</th>")
                out.append("</tr></thead><tbody>")
                in_table = True
            else:
                out.append("<tr>")
                for c in cells:
                    # Inline code in cells
                    cell_html = _html.escape(c)
                    cell_html = re.sub(r"`([^`]+)`", r"<code>\1</code>", cell_html)
                    out.append(f"<td>{cell_html}</td>")
                out.append("</tr>")
            continue

        # Close table if non-table line
        if in_table:
            out.append("</tbody></table>")
            in_table = False

        # Unordered list
        if stripped.startswith("- ") or stripped.startswith("* "):
            if not in_list:
                out.append("<ul>")
                in_list = True
            content = _html.escape(stripped[2:])
            content = re.sub(r"`([^`]+)`", r"<code>\1</code>", content)
            content = re.sub(r"\*\*([^*]+)\*\*", r"<strong>\1</strong>", content)
            out.append(f"<li>{content}</li>")
            continue

        # Close list if non-list line
        if in_list:
            out.append("</ul>")
            in_list = False

        # Paragraph — apply inline formatting
        p = _html.escape(stripped)
        p = re.sub(r"`([^`]+)`", r"<code>\1</code>", p)
        p = re.sub(r"\*\*([^*]+)\*\*", r"<strong>\1</strong>", p)
        out.append(f"<p>{p}</p>")

    if in_table:
        out.append("</tbody></table>")
    if in_list:
        out.append("</ul>")
    if in_code:
        out.append("</code></pre>")
    return "\n".join(out)


def _connect_ro(db_path: str) -> sqlite3.Connection:
    """Open a read-only SQLite connection (WAL-safe for concurrent reads)."""
    uri = f"file:{db_path}?mode=ro"
    conn = sqlite3.connect(uri, uri=True)
    conn.row_factory = sqlite3.Row
    return conn


def _rows_to_dicts(rows: list[sqlite3.Row]) -> list[dict]:
    return [dict(row) for row in rows]


def _safe_query(conn: sqlite3.Connection, sql: str, params: tuple = ()) -> list[sqlite3.Row]:
    """Execute a query, returning [] if the table doesn't exist."""
    try:
        return conn.execute(sql, params).fetchall()
    except sqlite3.OperationalError:
        return []


def _safe_scalar(conn: sqlite3.Connection, sql: str, params: tuple = (), default: int = 0) -> int:
    """Execute a scalar query, returning default if the table doesn't exist."""
    try:
        row = conn.execute(sql, params).fetchone()
        return row[0] if row else default
    except sqlite3.OperationalError:
        return default


# ---------------------------------------------------------------------------
# File classification
# ---------------------------------------------------------------------------

# Categories that represent actual code needing documentation
_DOCUMENTABLE_CATEGORIES = {"source", "script"}


def _is_documentable(category: str | None) -> bool:
    return (category or "other") in _DOCUMENTABLE_CATEGORIES


# ---------------------------------------------------------------------------
# Overview
# ---------------------------------------------------------------------------

def get_overview(db_path: str, log_path: str, target_dir: str = "") -> dict:
    conn = _connect_ro(db_path)
    try:
        total = _safe_scalar(conn, "SELECT COUNT(*) FROM files")

        # Documentable (source code) vs infrastructure split
        source_total = _safe_scalar(
            conn,
            "SELECT COUNT(*) FROM files WHERE category IN ('source', 'script')",
        )
        infra_total = total - source_total

        documented = _safe_scalar(
            conn,
            "SELECT COUNT(DISTINCT f.path) FROM docs d "
            "JOIN files f ON f.id = d.file_id WHERE d.status = 'current'",
        )

        # Documented source files specifically
        source_documented = _safe_scalar(
            conn,
            "SELECT COUNT(DISTINCT f.path) FROM docs d "
            "JOIN files f ON f.id = d.file_id "
            "WHERE d.status = 'current' AND f.category IN ('source', 'script')",
        )

        stale = _safe_scalar(
            conn,
            "SELECT COUNT(DISTINCT f.path) FROM docs d "
            "JOIN files f ON f.id = d.file_id "
            "WHERE d.status = 'current' AND f.content_hash != d.source_hash",
        )

        source_undocumented = source_total - source_documented
        source_pct = round(source_documented / source_total * 100, 1) if source_total > 0 else 0

        # Legacy totals for backward compat
        undocumented = total - documented
        pct = round(documented / total * 100, 1) if total > 0 else 0

        last_run_rows = _safe_query(conn, "SELECT * FROM runs ORDER BY id DESC LIMIT 1")
        last_run = dict(last_run_rows[0]) if last_run_rows else None

        # Recent area results — latest per area+phase only
        recent_areas = []
        if last_run:
            rows = _safe_query(
                conn,
                """SELECT * FROM area_results
                   WHERE run_id = ?
                     AND id IN (
                         SELECT MAX(id) FROM area_results
                         WHERE run_id = ? GROUP BY area_name, phase
                     )
                   ORDER BY area_name, phase""",
                (last_run["id"], last_run["id"]),
            )
            recent_areas = _rows_to_dicts(rows)

        recent_tools = get_tool_calls(log_path, limit=10)

        return {
            "total_files": total,
            "documented": documented,
            "stale": stale,
            "undocumented": undocumented,
            "pct": pct,
            # Source-specific stats
            "source_total": source_total,
            "source_documented": source_documented,
            "source_undocumented": source_undocumented,
            "source_pct": source_pct,
            "infra_total": infra_total,
            # Existing
            "last_run": last_run,
            "recent_areas": recent_areas,
            "recent_tools": recent_tools,
            # Project overview availability
            "has_project_overview": _has_project_overview(target_dir),
        }
    finally:
        conn.close()


def _has_project_overview(target_dir: str) -> bool:
    """Check if a project-overview.md exists on disk."""
    if not target_dir:
        return False
    for plat_dir in (".claude", ".windsurf", ".github", ".codex"):
        path = os.path.join(target_dir, plat_dir, "rules", "docs", "project-overview.md")
        if os.path.isfile(path):
            return True
    return False


def _find_project_overview_path(target_dir: str) -> str | None:
    """Return the full path to project-overview.md if it exists."""
    if not target_dir:
        return None
    for plat_dir in (".claude", ".windsurf", ".github", ".codex"):
        path = os.path.join(target_dir, plat_dir, "rules", "docs", "project-overview.md")
        if os.path.isfile(path):
            return path
    return None


def _get_project_overview_detail(conn, target_dir: str) -> dict | None:
    """Build a doc detail dict for the project overview (disk-only, not in docs table)."""
    full_path = _find_project_overview_path(target_dir)
    if not full_path:
        return None

    content = ""
    mtime = ""
    try:
        with open(full_path, "r", encoding="utf-8") as fh:
            content = fh.read()
        mtime = datetime.fromtimestamp(os.path.getmtime(full_path)).strftime(
            "%Y-%m-%d %H:%M:%S"
        )
    except OSError:
        return None

    # Get area summary from the latest plan for context
    plan_rows = _safe_query(
        conn,
        "SELECT plan_json FROM area_plans ORDER BY id DESC LIMIT 1",
    )
    area_names: list[str] = []
    if plan_rows:
        plan = json.loads(plan_rows[0][0])
        area_names = [a.get("name", "") for a in plan.get("areas", [])]

    return {
        "area_name": "project-overview",
        "doc_path": os.path.relpath(full_path, target_dir).replace("\\", "/"),
        "generated_at": mtime,
        "status": "current",
        "content": content,
        "content_html": _md_to_html(content) if content else "",
        "files_covered": [],
        "description": "High-level project description, feature index, and architecture overview",
        "priority": 0,
        "file_patterns": [],
        "rationale": f"Synthesized from {len(area_names)} area docs: {', '.join(area_names)}" if area_names else "",
    }


# ---------------------------------------------------------------------------
# Files
# ---------------------------------------------------------------------------

def get_files_table(db_path: str, status_filter: str | None = None, category_filter: str | None = None) -> list[dict]:
    conn = _connect_ro(db_path)
    try:
        all_files = _rows_to_dicts(
            conn.execute("SELECT * FROM files ORDER BY path").fetchall()
        )

        # Doc info per file: status, line count at doc time, doc generation date
        doc_info: dict[str, dict] = {}
        doc_rows = _safe_query(
            conn,
            "SELECT f.path, d.source_hash, d.source_line_count, d.generated_at, d.doc_path "
            "FROM docs d JOIN files f ON f.id = d.file_id "
            "WHERE d.status = 'current' ORDER BY d.generated_at DESC",
        )
        for row in doc_rows:
            r = dict(row)
            path = r["path"]
            if path not in doc_info:  # keep most recent doc per file
                doc_info[path] = r

        stale_paths = {
            row[0] for row in _safe_query(
                conn,
                "SELECT DISTINCT f.path FROM docs d "
                "JOIN files f ON f.id = d.file_id "
                "WHERE d.status = 'current' AND f.content_hash != d.source_hash",
            )
        }

        for f in all_files:
            path = f["path"]
            di = doc_info.get(path)

            if path in stale_paths:
                f["status"] = "stale"
            elif di:
                f["status"] = "documented"
            else:
                f["status"] = "undocumented"

            # Truncate hash for display
            h = f.get("content_hash") or ""
            f["hash_short"] = h[:10] if h else ""

            # Doc-time info
            if di:
                f["doc_lines"] = di.get("source_line_count")
                f["doc_date"] = di.get("generated_at", "")
                f["doc_path"] = di.get("doc_path", "")
                current = f.get("line_count") or 0
                doc_lc = di.get("source_line_count")
                if doc_lc is not None and current > 0:
                    f["line_delta"] = current - doc_lc
                else:
                    f["line_delta"] = None
            else:
                f["doc_lines"] = None
                f["doc_date"] = None
                f["doc_path"] = None
                f["line_delta"] = None

        # Classify documentable vs infrastructure and change magnitude
        for f in all_files:
            f["documentable"] = _is_documentable(f.get("category"))
            delta = f.get("line_delta")
            if delta is not None:
                abs_d = abs(delta)
                if abs_d > 50:
                    f["change_magnitude"] = "major"
                elif abs_d > 10:
                    f["change_magnitude"] = "moderate"
                else:
                    f["change_magnitude"] = "minor"
            else:
                f["change_magnitude"] = None

        if status_filter:
            all_files = [f for f in all_files if f["status"] == status_filter]
        if category_filter:
            all_files = [f for f in all_files if f.get("category") == category_filter]

        # Sort: stale first, then undocumented, then documented
        _STATUS_ORDER = {"stale": 0, "undocumented": 1, "documented": 2}
        all_files.sort(key=lambda f: (_STATUS_ORDER.get(f["status"], 3), f["path"]))

        return all_files
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# Runs
# ---------------------------------------------------------------------------

def get_runs(db_path: str) -> list[dict]:
    conn = _connect_ro(db_path)
    try:
        return _rows_to_dicts(
            conn.execute("SELECT * FROM runs ORDER BY id DESC").fetchall()
        )
    finally:
        conn.close()


def get_run_detail(db_path: str, run_id: int) -> dict | None:
    conn = _connect_ro(db_path)
    try:
        rows = _safe_query(conn, "SELECT * FROM runs WHERE id = ?", (run_id,))
        if not rows:
            return None

        area_results = _rows_to_dicts(
            _safe_query(
                conn,
                "SELECT * FROM area_results WHERE run_id = ? ORDER BY area_name, phase",
                (run_id,),
            )
        )

        plan_rows = _safe_query(
            conn,
            "SELECT plan_json FROM area_plans WHERE run_id = ? ORDER BY iteration DESC LIMIT 1",
            (run_id,),
        )
        area_plan = json.loads(plan_rows[0][0]) if plan_rows else None

        rel_rows = _safe_query(
            conn,
            "SELECT map_json FROM relationship_maps WHERE run_id = ? ORDER BY iteration DESC LIMIT 1",
            (run_id,),
        )
        rel_map = json.loads(rel_rows[0][0]) if rel_rows else None

        iteration_states = _rows_to_dicts(
            _safe_query(
                conn,
                "SELECT * FROM iteration_state WHERE run_id = ? ORDER BY id",
                (run_id,),
            )
        )

        return {
            "run": dict(rows[0]),
            "area_results": area_results,
            "area_plan": area_plan,
            "rel_map": rel_map,
            "iteration_states": iteration_states,
        }
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# Docs
# ---------------------------------------------------------------------------

def get_docs(db_path: str, target_dir: str = "") -> list[dict]:
    conn = _connect_ro(db_path)
    try:
        rows = conn.execute(
            """
            SELECT d.doc_path, d.area_name, d.generated_at, d.status, d.source_hash,
                   COUNT(DISTINCT d.file_id) as file_count
            FROM docs d
            GROUP BY d.doc_path, d.area_name
            ORDER BY d.generated_at DESC
            """
        ).fetchall()
        docs = _rows_to_dicts(rows)

        # Prepend project overview if it exists on disk
        overview_path = _find_project_overview_path(target_dir) if target_dir else None
        if overview_path:
            mtime = ""
            try:
                mtime = datetime.fromtimestamp(os.path.getmtime(overview_path)).strftime(
                    "%Y-%m-%d %H:%M:%S"
                )
            except OSError:
                pass
            docs.insert(0, {
                "doc_path": os.path.relpath(overview_path, target_dir).replace("\\", "/"),
                "area_name": "project-overview",
                "generated_at": mtime,
                "status": "current",
                "source_hash": None,
                "file_count": 0,
            })
        return docs
    finally:
        conn.close()


def get_doc_detail(db_path: str, target_dir: str, area_name: str) -> dict | None:
    """Return doc content, area plan info, and grouping rationale for an area."""
    conn = _connect_ro(db_path)
    try:
        # Special case: project-overview lives on disk only (not in docs table)
        if area_name == "project-overview":
            return _get_project_overview_detail(conn, target_dir)

        # Find the doc record
        doc_rows = _safe_query(
            conn,
            "SELECT DISTINCT doc_path, area_name, generated_at, status "
            "FROM docs WHERE area_name = ? ORDER BY generated_at DESC LIMIT 1",
            (area_name,),
        )
        if not doc_rows:
            return None
        doc = dict(doc_rows[0])

        # Read the actual .md file from disk
        doc_content = ""
        full_path = os.path.join(target_dir, doc["doc_path"])
        if os.path.isfile(full_path):
            try:
                with open(full_path, "r", encoding="utf-8") as fh:
                    doc_content = fh.read()
            except OSError:
                doc_content = "(Could not read file)"

        # Files covered by this doc
        file_rows = _safe_query(
            conn,
            "SELECT DISTINCT f.path, f.category, f.language, f.line_count "
            "FROM docs d JOIN files f ON f.id = d.file_id "
            "WHERE d.area_name = ? ORDER BY f.path",
            (area_name,),
        )
        files_covered = _rows_to_dicts(file_rows)

        # Area plan info + rationale from the latest run
        area_info: dict = {}
        rationale = ""
        plan_rows = _safe_query(
            conn,
            "SELECT plan_json FROM area_plans ORDER BY id DESC LIMIT 1",
        )
        if plan_rows:
            plan = json.loads(plan_rows[0][0])
            rationale = plan.get("rationale", "")
            for a in plan.get("areas", []):
                if a.get("name") == area_name:
                    area_info = a
                    break

        return {
            "area_name": area_name,
            "doc_path": doc["doc_path"],
            "generated_at": doc["generated_at"],
            "status": doc["status"],
            "content": doc_content,
            "content_html": _md_to_html(doc_content) if doc_content else "",
            "files_covered": files_covered,
            "description": area_info.get("description", ""),
            "priority": area_info.get("priority"),
            "file_patterns": area_info.get("file_patterns", []),
            "rationale": rationale,
        }
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# jCodeMunch tool call log parsing
# ---------------------------------------------------------------------------

_TOOL_CALL_RE = re.compile(
    r"^(\d{4}-\d{2}-\d{2}[T ]\d{2}:\d{2}:\d{2}[^|]*?)\s+"
    r"(?:\S+\s+)?(\w+)\s+"
    r"tool_call:\s+(\S+)\s+args=(.*)"
)

_ERROR_RE = re.compile(
    r"^(\d{4}-\d{2}-\d{2}[T ]\d{2}:\d{2}:\d{2}[^|]*?)\s+"
    r"(?:\S+\s+)?ERROR\s+"
    r"call_tool\s+(\S+)\s+failed"
)


def get_tool_calls(
    log_path: str,
    limit: int = 50,
    since: str | None = None,
) -> list[dict]:
    """Parse jcodemunch.log for tool call entries.

    Reads the last ~500KB of the log to avoid loading huge files.
    """
    if not log_path or not os.path.isfile(log_path):
        return []

    try:
        file_size = os.path.getsize(log_path)
        read_bytes = min(file_size, 512 * 1024)  # last 500KB

        with open(log_path, "r", encoding="utf-8", errors="replace") as f:
            if file_size > read_bytes:
                f.seek(file_size - read_bytes)
                f.readline()  # skip partial line
            lines = f.readlines()
    except OSError:
        return []

    calls: list[dict] = []
    for line in reversed(lines):
        line = line.strip()
        if not line:
            continue

        m = _TOOL_CALL_RE.match(line)
        if m:
            ts, level, tool, args = m.group(1), m.group(2), m.group(3), m.group(4)
            if since and ts <= since:
                continue
            calls.append({
                "timestamp": ts.strip(),
                "level": level,
                "tool": tool,
                "args": args[:200],
                "type": "tool_call",
            })
            if len(calls) >= limit:
                break
            continue

        m = _ERROR_RE.match(line)
        if m:
            ts, tool = m.group(1), m.group(2)
            if since and ts <= since:
                continue
            calls.append({
                "timestamp": ts.strip(),
                "level": "ERROR",
                "tool": tool,
                "args": "failed",
                "type": "error",
            })
            if len(calls) >= limit:
                break

    return calls


# ---------------------------------------------------------------------------
# Activity feed (merged timeline)
# ---------------------------------------------------------------------------

def _get_doc_file_events(target_dir: str, limit: int, since: str | None) -> list[dict]:
    """Scan .claude/rules/docs/*.md for file modification times.

    These represent when documentation rules were last written/updated on disk.
    """
    events: list[dict] = []
    for plat_subdir in (".claude", ".windsurf", ".github", ".codex"):
        docs_dir = os.path.join(target_dir, plat_subdir, "rules", "docs")
        if not os.path.isdir(docs_dir):
            continue
        for fname in os.listdir(docs_dir):
            if not fname.endswith(".md"):
                continue
            fpath = os.path.join(docs_dir, fname)
            try:
                mtime = os.path.getmtime(fpath)
                ts = datetime.fromtimestamp(mtime, tz=timezone.utc).isoformat()
            except OSError:
                continue
            if since and ts <= since:
                continue
            rel = os.path.join(plat_subdir, "rules", "docs", fname)
            events.append({
                "time": ts,
                "type": "doc_file",
                "icon": "book",
                "desc": f"Doc rule on disk: {rel}",
                "level": "INFO",
            })
    events.sort(key=lambda e: e["time"], reverse=True)
    return events[:limit]


def get_activity_feed(
    db_path: str,
    log_path: str,
    limit: int = 100,
    since: str | None = None,
    target_dir: str | None = None,
) -> list[dict]:
    """Merge tool calls, doc events, and file changes into a single timeline."""
    events: list[dict] = []

    # Tool calls from jcodemunch log
    for tc in get_tool_calls(log_path, limit=limit, since=since):
        events.append({
            "time": tc["timestamp"],
            "type": "tool",
            "icon": "wrench",
            "desc": f"{tc['tool']}({tc['args'][:80]})",
            "level": tc["level"],
        })

    # Doc generation events (from DB)
    conn = _connect_ro(db_path)
    try:
        doc_rows = conn.execute(
            "SELECT DISTINCT doc_path, area_name, generated_at, status FROM docs "
            "ORDER BY generated_at DESC LIMIT ?",
            (limit,),
        ).fetchall()
        for row in doc_rows:
            r = dict(row)
            ts = r["generated_at"]
            if since and ts <= since:
                continue
            events.append({
                "time": ts,
                "type": "doc_gen",
                "icon": "file-text",
                "desc": f"Generated {r['doc_path']} (area: {r['area_name']}, status: {r['status']})",
                "level": "INFO",
            })

        # Recent file scans — group by timestamp to avoid flooding
        file_rows = conn.execute(
            "SELECT last_scanned_at, language, COUNT(*) as cnt "
            "FROM files GROUP BY last_scanned_at, language "
            "ORDER BY last_scanned_at DESC LIMIT ?",
            (min(limit, 20),),
        ).fetchall()
        for row in file_rows:
            r = dict(row)
            ts = r["last_scanned_at"]
            if since and ts <= since:
                continue
            lang = r.get("language") or "?"
            cnt = r.get("cnt", 1)
            desc = f"Scanned {cnt} {lang} file{'s' if cnt != 1 else ''}" if cnt > 1 else f"Scanned 1 {lang} file"
            events.append({
                "time": ts,
                "type": "scan",
                "icon": "search",
                "desc": desc,
                "level": "INFO",
            })
    finally:
        conn.close()

    # Doc rule files on disk (modification times)
    if target_dir:
        events.extend(_get_doc_file_events(target_dir, limit, since))

    # Sort by time descending
    events.sort(key=lambda e: e.get("time", ""), reverse=True)
    return events[:limit]
