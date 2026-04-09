"""Spark dashboard HTTP server.

Stdlib-only ThreadingHTTPServer with route dispatch and jinja2 template rendering.
"""

from __future__ import annotations

import json
import os
import re
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlparse

from jinja2 import Environment, FileSystemLoader

from spark.dashboard import data as dashboard_data


class _DashboardHandler(BaseHTTPRequestHandler):
    """Request handler — class-level attrs set before server starts."""

    db_path: str = ""
    log_path: str = ""
    target_dir: str = ""
    jinja_env: Environment | None = None

    # Suppress default stderr logging per request
    def log_message(self, format: str, *args: object) -> None:  # noqa: A002
        pass

    # ------------------------------------------------------------------
    # Routing
    # ------------------------------------------------------------------

    _RUN_DETAIL_RE = re.compile(r"^/runs/(\d+)$")
    _DOC_VIEW_RE = re.compile(r"^/docs/view/(.+)$")

    def do_GET(self) -> None:  # noqa: N802
        parsed = urlparse(self.path)
        path = parsed.path.rstrip("/") or "/"
        qs = parse_qs(parsed.query)

        # Static routes
        if path == "/":
            self._redirect("/overview")
        elif path == "/overview":
            self._render_page("overview.html", "overview", self._overview_ctx())
        elif path == "/files":
            self._render_page("files.html", "files", self._files_ctx(qs))
        elif path == "/runs":
            self._render_page("runs.html", "runs", self._runs_ctx())
        elif path == "/activity":
            self._render_page("activity.html", "activity", self._activity_ctx())
        elif path == "/docs":
            self._render_page("docs.html", "docs", self._docs_ctx())

        # Dynamic: /runs/<id>
        elif (m := self._RUN_DETAIL_RE.match(path)):
            run_id = int(m.group(1))
            self._render_run_detail(run_id)

        # Dynamic: /docs/view/<area-name>
        elif (m := self._DOC_VIEW_RE.match(path)):
            area_name = m.group(1)
            self._render_doc_view(area_name)

        # JSON API
        elif path == "/api/activity":
            self._json_activity(qs)
        elif path == "/api/overview":
            self._json_overview()

        else:
            self._send_error(404, "Not found")

    # ------------------------------------------------------------------
    # Template rendering
    # ------------------------------------------------------------------

    def _render_page(self, template: str, page: str, ctx: dict) -> None:
        assert self.jinja_env is not None
        try:
            tmpl = self.jinja_env.get_template(template)
            html = tmpl.render(page=page, **ctx)
            self._send_html(html)
        except Exception as exc:
            self._send_error(500, f"Render error: {exc}")

    def _render_run_detail(self, run_id: int) -> None:
        detail = dashboard_data.get_run_detail(self.db_path, run_id)
        if detail is None:
            self._send_error(404, f"Run #{run_id} not found")
            return
        self._render_page("run_detail.html", "runs", {"detail": detail})

    def _render_doc_view(self, area_name: str) -> None:
        detail = dashboard_data.get_doc_detail(
            self.db_path, self.target_dir, area_name,
        )
        if detail is None:
            self._send_error(404, f"Documentation for area '{area_name}' not found")
            return
        self._render_page("doc_view.html", "docs", {"detail": detail})

    # ------------------------------------------------------------------
    # Context builders
    # ------------------------------------------------------------------

    def _overview_ctx(self) -> dict:
        return {"data": dashboard_data.get_overview(self.db_path, self.log_path, self.target_dir)}

    def _files_ctx(self, qs: dict) -> dict:
        status_filter = qs.get("status", [None])[0]
        category_filter = qs.get("category", [None])[0]
        files = dashboard_data.get_files_table(self.db_path, status_filter, category_filter)
        # Compute counts from unfiltered set
        all_files = dashboard_data.get_files_table(self.db_path, None, None)
        counts = {
            "all": len(all_files),
            "documented": sum(1 for f in all_files if f["status"] == "documented"),
            "stale": sum(1 for f in all_files if f["status"] == "stale"),
            "undocumented": sum(1 for f in all_files if f["status"] == "undocumented"),
        }
        cat_counts: dict[str, int] = {}
        for f in all_files:
            c = f.get("category", "other")
            cat_counts[c] = cat_counts.get(c, 0) + 1
        return {
            "files": files,
            "status_filter": status_filter,
            "category_filter": category_filter,
            "counts": counts,
            "cat_counts": cat_counts,
        }

    def _runs_ctx(self) -> dict:
        return {"runs": dashboard_data.get_runs(self.db_path)}

    def _activity_ctx(self) -> dict:
        events = dashboard_data.get_activity_feed(
            self.db_path, self.log_path, limit=100, target_dir=self.target_dir,
        )
        return {"events": events}

    def _docs_ctx(self) -> dict:
        return {"docs": dashboard_data.get_docs(self.db_path, self.target_dir)}

    # ------------------------------------------------------------------
    # JSON API endpoints
    # ------------------------------------------------------------------

    def _json_activity(self, qs: dict) -> None:
        since = qs.get("since", [None])[0]
        limit = int(qs.get("limit", ["50"])[0])
        events = dashboard_data.get_activity_feed(
            self.db_path, self.log_path, limit=limit, since=since,
            target_dir=self.target_dir,
        )
        self._send_json(events)

    def _json_overview(self) -> None:
        overview = dashboard_data.get_overview(self.db_path, self.log_path, self.target_dir)
        self._send_json(overview)

    # ------------------------------------------------------------------
    # Response helpers
    # ------------------------------------------------------------------

    def _send_html(self, html: str) -> None:
        body = html.encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _send_json(self, obj: object) -> None:
        body = json.dumps(obj, default=str).encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Access-Control-Allow-Origin", "*")
        self.end_headers()
        self.wfile.write(body)

    def _redirect(self, location: str) -> None:
        self.send_response(302)
        self.send_header("Location", location)
        self.end_headers()

    def _send_error(self, code: int, message: str) -> None:
        body = f"<h1>{code}</h1><p>{message}</p>".encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)


def create_server(
    db_path: str,
    log_path: str,
    templates_dir: str,
    host: str = "127.0.0.1",
    port: int = 8383,
    target_dir: str = "",
) -> ThreadingHTTPServer:
    """Create a configured dashboard server (not yet started)."""
    _DashboardHandler.db_path = db_path
    _DashboardHandler.log_path = log_path
    _DashboardHandler.target_dir = target_dir
    _DashboardHandler.jinja_env = Environment(
        loader=FileSystemLoader(templates_dir),
        autoescape=True,
    )
    return ThreadingHTTPServer((host, port), _DashboardHandler)
