"""
Spark Orchestrator — coordinates the planner, explorer, relationship mapper,
and doc writer agents through iterative refinement loops.

This is the core iteration loop that drives multi-agent documentation generation
with concurrent execution, mode filtering, context separation, and resumability.
"""

from __future__ import annotations

import copy
import json
import logging
import os
import threading
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional

from spark.config import SparkConfig
from spark.db import Database
from spark.engine.openrouter import OpenRouterClient
from spark.engine.loop import process_agentic_loop_native
from spark.engine.tool_executor import extract_json
from spark.tools.registry import ToolRegistry
from spark.ui import ui

logger = logging.getLogger(__name__)


class _LightweightSession:
    """Minimal session object expected by process_agentic_loop_native."""

    def __init__(self) -> None:
        self.chat_history: list[dict] = []
        self.context: dict[str, Any] = {}


class Orchestrator:
    """Coordinates all Spark agents through iterative documentation loops."""

    def __init__(self, config: SparkConfig, db: Database, shutdown_event: Optional[threading.Event] = None) -> None:
        self.config = config
        self.db = db
        self.base_dir = os.path.abspath(config.target_dir)
        self.shutdown_event = shutdown_event or threading.Event()
        self.client = OpenRouterClient(api_key=config.api_key, shutdown_event=self.shutdown_event)
        self.tool_registry = ToolRegistry(
            definitions_path=os.path.join(os.path.dirname(__file__), "tools", "definitions.json"),
            base_dir=self.base_dir,
            db=db,
        )
        self._load_agent_defs()

        # Strip code tools from agent defs if code indexing is disabled
        if not config.code_index:
            code_tools = {"code_search", "code_index"}
            for agent_def in self.agent_defs.values():
                agent_def["tools"] = [t for t in agent_def.get("tools", []) if t not in code_tools]

    # ------------------------------------------------------------------
    # Setup helpers
    # ------------------------------------------------------------------

    def _load_agent_defs(self) -> None:
        """Load agent JSON definitions from spark/agents/."""
        agents_dir = os.path.join(os.path.dirname(__file__), "agents")
        self.agent_defs: dict[str, dict] = {}
        for name in ["planner", "explorer", "relationship_mapper", "doc_writer", "overview_writer", "doc_patcher"]:
            path = os.path.join(agents_dir, f"{name}.json")
            with open(path, encoding="utf-8") as f:
                self.agent_defs[name] = json.load(f)

    def _load_prompt(self, agent_name: str) -> str:
        """Load system prompt for an agent."""
        prompt_file = self.agent_defs[agent_name].get(
            "system_prompt_file", f"prompts/{agent_name}.md"
        )
        path = os.path.join(os.path.dirname(__file__), prompt_file)
        with open(path, encoding="utf-8") as f:
            return f.read()

    # ------------------------------------------------------------------
    # Main entry point
    # ------------------------------------------------------------------

    def run(self, dry_run: bool = False, resume: bool = False) -> dict:
        """Run the full iteration loop.

        Returns a dict with run_id, plan, and status.
        """
        # 1. Start or resume a run
        if resume:
            run_id = self._resume_run()
        else:
            run_id = self.db.start_run(
                mode=self.config.mode,
                iterations=self.config.iterations,
                config_snapshot=json.dumps({
                    "models": self.config.models,
                    "iterations": self.config.iterations,
                    "mode": self.config.mode,
                }),
            )

        # 2. Scan files
        ui.phase("Scan", "Indexing repository files")
        self.db.scan_files(self.base_dir, exclude_patterns=self.config.exclude_patterns)
        file_tree = self._get_file_tree()
        repo_metadata = self._get_repo_metadata()
        file_count = repo_metadata.get("total_files", 0)
        lang_summary = ", ".join(
            f"{lang} ({n})" for lang, n in list(repo_metadata.get("languages", {}).items())[:5]
        )
        ui.phase_end(f"{file_count} files indexed" + (f" — {lang_summary}" if lang_summary else ""))

        # 2b. Code indexing (if enabled)
        repo_analysis: Optional[dict] = None
        if self.config.code_index:
            ui.phase("Code Index", "Building symbol index (jCodeMunch)")
            from spark.code_index import index_repo as run_code_index
            idx_result = run_code_index(self.base_dir, exclude_patterns=self.config.exclude_patterns)
            if idx_result:
                ui.phase_end("Symbol index ready")
                # Store git HEAD SHA for symbol-level diffing
                try:
                    import subprocess
                    git_sha = subprocess.check_output(
                        ["git", "rev-parse", "HEAD"],
                        cwd=self.base_dir, stderr=subprocess.DEVNULL,
                    ).decode().strip()
                    self.db.save_index_sha(run_id, git_sha)
                except Exception:
                    pass  # no git or not a git repo
                # Pre-compute repo analysis for doc writers
                try:
                    from spark.code_index import get_repo_identifier, _ensure_jcodemunch
                    if _ensure_jcodemunch():
                        from jcodemunch_mcp.tools.get_repo_outline import get_repo_outline
                        owner, name = get_repo_identifier(self.base_dir)
                        repo_id = f"{owner}/{name}"
                        index_path = os.path.join(self.base_dir, ".code-index")
                        outline = get_repo_outline(repo=repo_id, storage_path=index_path)
                        if not outline.get("error"):
                            repo_analysis = outline
                except Exception as exc:
                    logger.warning("Pre-compute repo analysis failed: %s", exc)
            else:
                ui.phase_end("Skipped (unavailable)")

        # 2c. Adopt mode — separate pipeline
        if self.config.mode == "adopt":
            return self._run_adopt_pipeline(run_id)

        # 3. Iteration loop
        area_plan: Optional[dict] = None
        explorer_reports: list[dict] = []
        relationship_map: Optional[dict] = None

        for iteration in range(1, self.config.iterations + 1):
            ui.phase(
                f"Iteration {iteration}/{self.config.iterations}",
                "Planning → Exploration → Mapping",
            )

            # Phase 1: Planning
            area_plan = self._run_planner(
                run_id, iteration, file_tree, repo_metadata,
                explorer_reports, relationship_map,
                repo_analysis=repo_analysis,
            )
            self.db.save_area_plan(run_id, iteration, json.dumps(area_plan))

            if dry_run and iteration == 1:
                ui.phase_end("Dry run — stopping after plan")
                return {"plan": area_plan, "dry_run": True}

            # Filter areas based on mode
            filtered_plan = self._filter_for_mode(area_plan)
            if not filtered_plan.get("areas"):
                ui.phase_end("No areas to process in this mode")
                break

            # Phase 2: Exploration (concurrent)
            area_count = len(filtered_plan["areas"])
            ui.phase("Exploration", f"{area_count} area{'s' if area_count != 1 else ''}")
            ui.track_reset()
            explorer_reports = self._run_explorers(run_id, iteration, filtered_plan)

            # Phase 3: Relationship Mapping
            ui.phase("Relationship Mapping")
            relationship_map = self._run_relationship_mapper(
                run_id, iteration, explorer_reports, area_plan,
            )
            self.db.save_relationship_map(run_id, iteration, json.dumps(relationship_map))

            self.db.update_run_iterations(run_id, iteration)

        # Phase 4: Doc Generation (concurrent)
        if not dry_run and area_plan and explorer_reports:
            area_count = len(area_plan.get("areas", []))
            ui.phase("Documentation", f"Writing docs for {area_count} area{'s' if area_count != 1 else ''}")
            ui.track_reset()
            doc_results = self._run_doc_writers(
                run_id, area_plan, explorer_reports, relationship_map,
                repo_analysis=repo_analysis,
            )

            # Phase 5: Finalize
            self._finalize(run_id, doc_results, area_plan, relationship_map)

        self.db.complete_run(run_id)
        return {"run_id": run_id, "plan": area_plan, "status": "completed"}

    # ------------------------------------------------------------------
    # Phase 1: Planner
    # ------------------------------------------------------------------

    def _run_planner(
        self,
        run_id: int,
        iteration: int,
        file_tree: str,
        repo_metadata: dict,
        prev_reports: list[dict],
        prev_rel_map: Optional[dict],
        repo_analysis: Optional[dict] = None,
    ) -> dict:
        """Run the planner agent and return the area plan dict."""
        state_id = self.db.save_iteration_state(
            run_id, iteration, "planning", None, None, "running",
        )

        # Build user message
        user_parts: list[str] = []

        # Inject project context from README and instructions files (if they exist)
        project_context = self._read_project_context()
        if project_context:
            user_parts.append(project_context)

        user_parts.append(f"## Repository Structure\n\n```\n{file_tree}\n```")
        user_parts.append(
            f"## Repository Metadata\n\n```json\n{json.dumps(repo_metadata, indent=2)}\n```"
        )
        user_parts.append(f"## Documentation Mode\n\nMode: **{self.config.mode}**")

        # Inject workflow analysis (or fallback to AST analysis) if code index available
        workflow_section = self._build_workflow_analysis(repo_analysis)
        if workflow_section:
            user_parts.append(workflow_section)

        if iteration >= 2 and prev_reports:
            # Include previous explorer summaries
            summaries = []
            for report in prev_reports:
                summaries.append(
                    f"### Area: {report.get('area', 'unknown')}\n"
                    f"{report.get('area_summary', 'No summary available.')}\n"
                    f"Cross-area refs: {json.dumps(report.get('cross_area_refs', []))}\n"
                    f"Suggested splits: {json.dumps(report.get('suggested_splits', []))}"
                )
            user_parts.append(
                "## Previous Explorer Summaries\n\n" + "\n\n".join(summaries)
            )

        if iteration >= 2 and prev_rel_map:
            user_parts.append(
                "## Previous Relationship Map\n\n"
                f"```json\n{json.dumps(prev_rel_map, indent=2)}\n```"
            )

        user_parts.append(
            f"\n## Instructions\n\n"
            f"This is iteration {iteration}/{self.config.iterations}. "
            f"Produce an area plan as a JSON object with `areas` and `rationale` keys."
        )

        user_message = "\n\n".join(user_parts)

        # Build layer_def
        agent_def = self.agent_defs["planner"]
        model = self.config.models.get("planner", agent_def.get("model", "openai/gpt-4o-mini"))
        tool_names = agent_def.get("tools", [])

        layer_def = {
            "model": model,
            "system_message": self._load_prompt("planner"),
            "user_message": user_message,
            "max_iterations": agent_def.get("max_iterations", 15),
            "max_tokens": agent_def.get("max_tokens", 8192),
            "temperature": agent_def.get("temperature", 0.2),
        }

        tool_callables = self.tool_registry.get_tool_callables(tool_names)
        session = _LightweightSession()

        try:
            result = process_agentic_loop_native(
                layer_def=layer_def,
                session=session,
                client=self.client,
                tool_registry=tool_callables,
                knowledge={},
            )

            response_text = result.get("response", "")
            area_plan = extract_json(response_text)

            if area_plan is None:
                raise ValueError(
                    f"Planner did not return valid JSON. Response: {response_text[:500]}"
                )

            # Ensure required keys
            if "areas" not in area_plan:
                area_plan["areas"] = []
            if "rationale" not in area_plan:
                area_plan["rationale"] = ""

            self.db.update_iteration_state(
                state_id, output_json=json.dumps(area_plan), status="completed",
            )

            area_count = len(area_plan.get("areas", []))
            ui.phase_end(f"{area_count} areas identified")
            for area in area_plan.get("areas", []):
                ui._write(
                    f"    • {area.get('name', '?')} (P{area.get('priority', '?')}): "
                    f"{area.get('description', '')[:60]}"
                )

            return area_plan

        except Exception as exc:
            logger.error("Planner failed: %s", exc)
            self.db.update_iteration_state(state_id, status="failed")
            raise RuntimeError(f"Planner failed (fatal): {exc}") from exc

    # ------------------------------------------------------------------
    # Phase 2: Explorers (concurrent)
    # ------------------------------------------------------------------

    def _run_explorers(
        self,
        run_id: int,
        iteration: int,
        plan: dict,
    ) -> list[dict]:
        """Run explorer agents concurrently, one per area. Returns list of reports."""
        areas = plan.get("areas", [])
        if not areas:
            return []

        # Build plan summary for context (without full file lists)
        plan_summary = "Areas in the plan:\n" + "\n".join(
            f"- {a.get('name', '?')}: {a.get('description', '')}"
            for a in areas
        )

        reports: list[dict] = []
        agent_def = self.agent_defs["explorer"]
        model = self.config.models.get("explorer", agent_def.get("model", "openai/gpt-4o-mini"))
        tool_names = agent_def.get("tools", [])

        def run_single_explorer(area: dict) -> dict:
            area_name = area.get("name", "unknown")
            ui.track_start(area_name)
            state_id = self.db.save_iteration_state(
                run_id, iteration, "exploration", area_name, json.dumps(area), "running",
            )

            # Build focus-files section if mode filtering added one
            focus_section = ""
            focus_files = area.get("focus_files")
            if focus_files:
                focus_section = (
                    f"\n## Focus Files\n\n"
                    f"The following files need attention (undocumented or stale). "
                    f"Prioritise these but still read the surrounding files for context.\n\n"
                    + "\n".join(f"- `{f}`" for f in focus_files)
                    + "\n"
                )

            # Build changed-symbols section for refresh/fill-gaps modes
            changes_section = ""
            symbol_changes = area.get("symbol_changes")
            if symbol_changes:
                lines = [
                    "\n## Changed Symbols (since last documentation)\n\n"
                    "The following symbols have changed and need re-analysis. "
                    "Use `code_search` with `get_source` to read only these symbols.\n"
                ]
                for sym in symbol_changes[:30]:  # cap display
                    sym_id = sym.get("symbol_id", sym.get("name", "?"))
                    change_type = sym.get("change_type", "modified")
                    lines.append(f"- `{sym_id}` ({change_type})")
                changes_section = "\n".join(lines) + "\n"

            # Pre-digest large files with jcodemunch outlines
            outlines_section = self._build_file_outlines(area)

            # Pre-compute dependency graph for the area
            deps_section = self._build_area_dependencies(area)

            user_message = (
                f"## Your Area Assignment\n\n"
                f"**Area:** {area_name}\n"
                f"**Description:** {area.get('description', '')}\n"
                f"**File patterns:** {json.dumps(area.get('file_patterns', []))}\n\n"
                f"## Plan Summary\n\n{plan_summary}\n"
                f"{focus_section}"
                f"{changes_section}"
                f"{outlines_section}"
                f"{deps_section}\n"
                f"## Instructions\n\n"
                f"Analyze all files matching your area's patterns. "
                f"Return a JSON report with `area`, `files`, `area_summary`, "
                f"`cross_area_refs`, and optionally `suggested_splits`."
            )

            layer_def = {
                "model": model,
                "system_message": self._load_prompt("explorer"),
                "user_message": user_message,
                "max_iterations": agent_def.get("max_iterations", 25),
                "max_tokens": agent_def.get("max_tokens", 16384),
                "temperature": agent_def.get("temperature", 0.1),
                "context_budget": 120_000,  # chars (~30K tokens), conservative for 131K context models
            }

            last_error: Exception | None = None
            retry_layer_def = None  # modified layer_def for retry with explicit shape hint
            _used_fallback = False  # track if we already switched to fallback model
            for attempt in range(2):  # 1 try + 1 retry
                tool_callables = self.tool_registry.get_tool_callables(tool_names)
                session = _LightweightSession()
                try:
                    active_layer = retry_layer_def if (retry_layer_def and attempt > 0) else layer_def
                    result = process_agentic_loop_native(
                        layer_def=active_layer,
                        session=session,
                        client=self.client,
                        tool_registry=tool_callables,
                        knowledge={},
                    )

                    response_text = result.get("response", "")

                    # Detect context overflow from the loop's error message
                    if "context length" in response_text.lower() and "maximum" in response_text.lower():
                        if not _used_fallback and attempt < 1:
                            from spark.config import CONTEXT_OVERFLOW_FALLBACKS
                            fallback_model = CONTEXT_OVERFLOW_FALLBACKS.get("explorer")
                            if fallback_model:
                                logger.warning(
                                    "Explorer for %s hit context limit on %s, retrying with %s",
                                    area_name, model, fallback_model,
                                )
                                ui._write(f"  ↻ {area_name} context overflow — switching to {fallback_model.split('/')[-1]}")
                                retry_layer_def = dict(layer_def)
                                retry_layer_def["model"] = fallback_model
                                retry_layer_def["context_budget"] = 800_000  # ~200K tokens, generous for 1M model
                                _used_fallback = True
                                continue  # retry with fallback model

                    report = extract_json(response_text)

                    if report is None:
                        report = {
                            "area": area_name,
                            "files": [],
                            "area_summary": response_text[:500] if response_text else "Explorer did not return structured output.",
                            "cross_area_refs": [],
                        }

                    report.setdefault("area", area_name)
                    report.setdefault("files", [])
                    report.setdefault("area_summary", "")
                    report.setdefault("cross_area_refs", [])

                    # Validate response shape: the LLM sometimes returns
                    # a single file object instead of the expected
                    # {area, files[], area_summary} structure. Treat
                    # malformed responses as retriable failures.
                    files_analyzed = len(report.get("files", []))
                    area_patterns = area.get("file_patterns", [])
                    if files_analyzed == 0 and area_patterns and attempt < 1:
                        logger.warning(
                            "Explorer for %s returned 0 files (malformed response), retrying",
                            area_name,
                        )
                        ui._write(f"  ↻ {area_name} returned empty result, retrying...")
                        # Add explicit shape hint for the retry
                        retry_layer_def = dict(retry_layer_def or layer_def)
                        retry_layer_def["user_message"] = (
                            layer_def["user_message"]
                            + "\n\nIMPORTANT: Your response MUST be a JSON object with this exact shape:\n"
                            '{"area": "' + area_name + '", "files": [{...per-file analysis...}], '
                            '"area_summary": "...", "cross_area_refs": [...]}\n'
                            "Read each file in the area and include it in the files array."
                        )
                        continue  # retry

                    self.db.update_iteration_state(
                        state_id, output_json=json.dumps(report), status="completed",
                    )

                    # Score exploration quality
                    # Count files with empty exports/imports (sign of incomplete analysis).
                    # Exclude entry-points/scripts — they legitimately have no exports.
                    _NO_EXPORT_ROLES = {"entry-point", "script", "config", "migration", "asset", "template", "ui-template", "view", "layout", "partial"}
                    # Also skip HTML/CSS/template files — they don't have exports
                    _TEMPLATE_EXTS = {".html", ".htm", ".hbs", ".ejs", ".pug", ".svelte", ".vue", ".css", ".scss", ".less"}
                    shallow_files = sum(
                        1 for f in report.get("files", [])
                        if not f.get("exports") and not f.get("key_functions")
                        and f.get("role") not in _NO_EXPORT_ROLES
                        and not any(f.get("path", "").endswith(ext) for ext in _TEMPLATE_EXTS)
                    )
                    # Count tool errors from the session history
                    tool_errors = sum(
                        1 for msg in session.chat_history
                        if msg.get("role") == "tool" and '"error"' in msg.get("content", "")
                    )

                    if files_analyzed == 0:
                        # Build a skeleton from file_patterns so doc writer
                        # has at least the file list to work with
                        if area_patterns:
                            report["files"] = [
                                {"path": p, "role": "unknown", "summary": "Explorer did not analyze this file.", "exports": [], "imports_from": [], "key_functions": [], "patterns": []}
                                for p in area_patterns if not any(c in p for c in "*?")
                            ]
                            files_analyzed = len(report["files"])
                        quality = "failed" if files_analyzed == 0 else "partial"
                    elif tool_errors > files_analyzed * 2:
                        quality = "partial"
                    elif shallow_files > files_analyzed * 0.5:
                        quality = "partial"
                    else:
                        quality = "complete"

                    self.db.record_area_result(
                        run_id=run_id, area_name=area_name, phase="exploration",
                        status="completed", files_analyzed=files_analyzed,
                        tool_errors=tool_errors, quality=quality,
                        detail=f"{shallow_files} shallow files" if shallow_files else None,
                    )

                    ui.track_done(area_name, f"{files_analyzed} files analyzed")
                    return report

                except Exception as exc:
                    last_error = exc
                    # Detect context overflow in exception and switch to fallback model
                    exc_msg = str(exc).lower()
                    if "context length" in exc_msg and "maximum" in exc_msg and not _used_fallback:
                        from spark.config import CONTEXT_OVERFLOW_FALLBACKS
                        fallback_model = CONTEXT_OVERFLOW_FALLBACKS.get("explorer")
                        if fallback_model and attempt < 1:
                            logger.warning(
                                "Explorer for %s hit context limit on %s, retrying with %s",
                                area_name, model, fallback_model,
                            )
                            ui._write(f"  ↻ {area_name} context overflow — switching to {fallback_model.split('/')[-1]}")
                            retry_layer_def = dict(layer_def)
                            retry_layer_def["model"] = fallback_model
                            retry_layer_def["context_budget"] = 800_000
                            _used_fallback = True
                            continue
                    if attempt == 0:
                        logger.warning("Explorer failed for area %s (retrying): %s", area_name, exc)
                        ui._write(f"  ↻ {area_name} failed, retrying...")
                    else:
                        logger.error("Explorer failed for area %s (giving up): %s", area_name, exc)

            # Both attempts failed
            self.db.update_iteration_state(state_id, status="failed")
            self.db.record_area_result(
                run_id=run_id, area_name=area_name, phase="exploration",
                status="failed", quality="failed",
                detail=str(last_error)[:200] if last_error else None,
            )
            ui.track_fail(area_name, str(last_error))
            return {
                "area": area_name,
                "files": [],
                "area_summary": f"Explorer failed: {last_error}",
                "cross_area_refs": [],
                "error": str(last_error),
            }

        max_workers = min(self.config.max_concurrent_workers, len(areas))
        with ThreadPoolExecutor(max_workers=max_workers) as executor:
            futures = {
                executor.submit(run_single_explorer, area): area
                for area in areas
            }
            remaining = set(futures.keys())
            while remaining:
                if self.shutdown_event.is_set():
                    executor.shutdown(wait=False, cancel_futures=True)
                    raise KeyboardInterrupt("Shutdown requested")
                # Short timeout so main thread stays responsive to Ctrl+C
                done = set()
                try:
                    for future in as_completed(remaining, timeout=1.0):
                        done.add(future)
                        area = futures[future]
                        try:
                            report = future.result()
                            reports.append(report)
                        except Exception as exc:
                            area_name = area.get("name", "unknown")
                            logger.error("Explorer thread failed for %s: %s", area_name, exc)
                            ui.track_fail(area_name, str(exc))
                            reports.append({
                                "area": area_name,
                                "files": [],
                                "area_summary": f"Thread error: {exc}",
                                "cross_area_refs": [],
                                "error": str(exc),
                            })
                except TimeoutError:
                    pass  # Normal — just loop to check shutdown_event
                remaining -= done

        failed = [r for r in reports if r.get("error")]
        if failed:
            ui._write(f"\n  ⚠ {len(failed)} area(s) failed exploration")
        ok = len(reports) - len(failed)
        ui.phase_end(f"{ok} successful, {len(failed)} failed")
        return reports

    # ------------------------------------------------------------------
    # Phase 3: Relationship Mapper (single call, no tools)
    # ------------------------------------------------------------------

    def _run_relationship_mapper(
        self,
        run_id: int,
        iteration: int,
        reports: list[dict],
        plan: dict,
    ) -> dict:
        """Run the relationship mapper as a single LLM call (no agentic loop)."""
        state_id = self.db.save_iteration_state(
            run_id, iteration, "relationship_mapping", None, None, "running",
        )

        # Build user message with explorer summaries (no raw file contents)
        user_parts: list[str] = []

        user_parts.append("## Explorer Area Summaries\n")
        for report in reports:
            area_name = report.get("area", "unknown")
            summary = report.get("area_summary", "No summary.")
            cross_refs = report.get("cross_area_refs", [])

            # Include per-file exports/imports for relationship analysis, but not file contents
            file_summaries = []
            for f in report.get("files", []):
                file_summaries.append(
                    f"  - **{f.get('path', '?')}** ({f.get('role', '?')}): "
                    f"{f.get('summary', '')}\n"
                    f"    Exports: {json.dumps(f.get('exports', []))}\n"
                    f"    Imports from: {json.dumps(f.get('imports_from', []))}"
                )

            user_parts.append(
                f"### Area: {area_name}\n\n"
                f"**Summary:** {summary}\n\n"
                f"**Cross-area references:** {json.dumps(cross_refs)}\n\n"
                f"**Files:**\n" + "\n".join(file_summaries)
            )

        # Include current area plan
        plan_areas = [
            {"name": a.get("name"), "description": a.get("description"), "file_patterns": a.get("file_patterns")}
            for a in plan.get("areas", [])
        ]
        user_parts.append(
            f"\n## Current Area Plan\n\n```json\n{json.dumps(plan_areas, indent=2)}\n```"
        )

        # Inject AST-derived cross-area dependencies
        cross_area_section = self._build_cross_area_deps(plan)
        if cross_area_section:
            user_parts.append(cross_area_section)
        coupling_section = self._build_area_coupling_summary(plan)
        if coupling_section:
            user_parts.append(coupling_section)

        user_parts.append(
            "\n## Instructions\n\n"
            "Analyze the explorer summaries and produce a relationship map as a JSON object "
            "with `edges`, `shared_types`, `data_flows`, and `suggested_regroupings`."
        )

        user_message = "\n\n".join(user_parts)
        system_prompt = self._load_prompt("relationship_mapper")

        agent_def = self.agent_defs["relationship_mapper"]
        model = self.config.models.get(
            "relationship_mapper", agent_def.get("model", "openai/gpt-4o-mini")
        )

        messages = [
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": user_message},
        ]

        try:
            resp = self.client.chat_completion(
                model=model,
                messages=messages,
                max_tokens=agent_def.get("max_tokens", 8192),
                temperature=agent_def.get("temperature", 0.2),
            )

            response_text = resp.get("content", "")
            rel_map = extract_json(response_text)

            if rel_map is None:
                rel_map = {
                    "edges": [],
                    "shared_types": [],
                    "data_flows": [],
                    "suggested_regroupings": [],
                }
                logger.warning("Relationship mapper did not return valid JSON.")

            # Ensure required keys
            rel_map.setdefault("edges", [])
            rel_map.setdefault("shared_types", [])
            rel_map.setdefault("data_flows", [])
            rel_map.setdefault("suggested_regroupings", [])

            self.db.update_iteration_state(
                state_id, output_json=json.dumps(rel_map), status="completed",
            )

            ui.phase_end(
                f"{len(rel_map['edges'])} edges, "
                f"{len(rel_map['shared_types'])} shared types, "
                f"{len(rel_map['data_flows'])} data flows"
            )
            return rel_map

        except Exception as exc:
            logger.error("Relationship mapper failed: %s", exc)
            self.db.update_iteration_state(state_id, status="failed")
            ui.phase_end(f"Failed: {exc}")
            # Non-fatal: return an empty map
            return {
                "edges": [],
                "shared_types": [],
                "data_flows": [],
                "suggested_regroupings": [],
            }

    # ------------------------------------------------------------------
    # Phase 4: Doc Writers (concurrent)
    # ------------------------------------------------------------------

    def _run_doc_writers(
        self,
        run_id: int,
        plan: dict,
        reports: list[dict],
        rel_map: Optional[dict],
        repo_analysis: Optional[dict] = None,
    ) -> list[dict]:
        """Run doc writer agents concurrently, one per area. Returns list of results."""
        areas = plan.get("areas", [])
        if not areas:
            return []

        # Index reports by area name
        report_by_area: dict[str, dict] = {}
        for report in reports:
            report_by_area[report.get("area", "")] = report

        rel_map = rel_map or {"edges": [], "shared_types": [], "data_flows": [], "suggested_regroupings": []}

        agent_def = self.agent_defs["doc_writer"]
        model = self.config.models.get("doc_writer", agent_def.get("model", "openai/gpt-4o-mini"))
        tool_names = agent_def.get("tools", [])
        # Doc writers need write_file (update_rules_index handled by _finalize)
        all_tool_names = list(set(tool_names) | {"write_file"})

        project_name = os.path.basename(self.base_dir) or "Project"
        today = datetime.now(timezone.utc).strftime("%Y-%m-%d")

        results: list[dict] = []

        def run_single_doc_writer(area: dict) -> dict:
            area_name = area.get("name", "unknown")
            report = report_by_area.get(area_name)
            if not report or report.get("error"):
                ui._write(f"  ⊘ {area_name} skipped — no explorer report")
                return {"area": area_name, "status": "skipped"}

            ui.track_start(area_name)
            state_id = self.db.save_iteration_state(
                run_id, 0, "doc_writing", area_name, None, "running",
            )

            # Filter relationship data to only those involving this area
            relevant_edges = [
                e for e in rel_map.get("edges", [])
                if e.get("from_area") == area_name or e.get("to_area") == area_name
            ]
            relevant_shared_types = [
                t for t in rel_map.get("shared_types", [])
                if area_name in t.get("used_by", []) or area_name == t.get("defined_in", "")
            ]
            relevant_data_flows = [
                f for f in rel_map.get("data_flows", [])
                if area_name in f.get("path", [])
            ]

            user_message = (
                f"## Area: {area_name}\n"
                f"**Project:** {project_name}\n"
                f"**Date:** {today}\n\n"
                f"## Explorer Report\n\n"
                f"```json\n{json.dumps(report, indent=2)}\n```\n\n"
                f"## Relationship Edges for This Area\n\n"
                f"```json\n{json.dumps(relevant_edges, indent=2)}\n```\n\n"
            )
            if relevant_shared_types:
                user_message += (
                    f"## Shared Types Involving This Area\n\n"
                    f"```json\n{json.dumps(relevant_shared_types, indent=2)}\n```\n\n"
                )
            if relevant_data_flows:
                user_message += (
                    f"## Data Flows Through This Area\n\n"
                    f"```json\n{json.dumps(relevant_data_flows, indent=2)}\n```\n\n"
                )
            if repo_analysis:
                # Compact summary: truncate if too large
                analysis_json = json.dumps(repo_analysis, indent=2)
                if len(analysis_json) > 10000:
                    # Truncate to essential fields only
                    compact = {
                        k: v for k, v in repo_analysis.items()
                        if k in ("repo", "file_count", "symbol_count", "languages",
                                 "directories", "symbol_kinds", "most_imported_files",
                                 "most_central_symbols")
                    }
                    analysis_json = json.dumps(compact, indent=2)
                user_message += (
                    f"## Repo Analysis (AST-based)\n\n"
                    f"```json\n{analysis_json}\n```\n\n"
                )

            # Pre-computed impact data and file outlines
            impact_section = self._build_doc_impact_data(report)
            if impact_section:
                user_message += impact_section
            outlines_section = self._build_doc_file_outlines(report)
            if outlines_section:
                user_message += outlines_section

            user_message += (
                f"## Instructions\n\n"
                f"Write the documentation file to `.claude/rules/docs/{area_name}.md` "
                f"following the document template in your system prompt. "
                f"Use `write_file` to create the file. "
                f"The rules index will be updated automatically after you finish."
            )

            layer_def = {
                "model": model,
                "system_message": self._load_prompt("doc_writer"),
                "user_message": user_message,
                "max_iterations": agent_def.get("max_iterations", 15),
                "max_tokens": agent_def.get("max_tokens", 8192),
                "temperature": agent_def.get("temperature", 0.1),
            }

            last_error: Exception | None = None
            for attempt in range(2):  # 1 try + 1 retry
                tool_callables = self.tool_registry.get_tool_callables(all_tool_names)
                session = _LightweightSession()

                try:
                    result = process_agentic_loop_native(
                        layer_def=layer_def,
                        session=session,
                        client=self.client,
                        tool_registry=tool_callables,
                        knowledge={},
                    )

                    doc_path = f".claude/rules/docs/{area_name}.md"

                    # Check if the doc file was actually written
                    full_doc_path = os.path.join(self.base_dir, doc_path)
                    doc_written = os.path.isfile(full_doc_path)
                    doc_size = os.path.getsize(full_doc_path) if doc_written else 0

                    # Retry if the doc wasn't written at all
                    if not doc_written and attempt < 1:
                        logger.warning(
                            "Doc writer for %s did not write file, retrying", area_name
                        )
                        ui._write(f"  ↻ {area_name} doc not written, retrying...")
                        continue

                    self.db.update_iteration_state(
                        state_id,
                        output_json=json.dumps({"doc_path": doc_path}),
                        status="completed",
                    )

                    # Score doc quality
                    tool_errors = sum(
                        1 for msg in session.chat_history
                        if msg.get("role") == "tool" and '"error"' in msg.get("content", "")
                    )
                    if not doc_written:
                        quality = "failed"
                    elif doc_size < 200:
                        quality = "partial"
                    elif tool_errors > 5:
                        quality = "partial"
                    else:
                        quality = "complete"

                    self.db.record_area_result(
                        run_id=run_id, area_name=area_name, phase="doc_writing",
                        status="completed", doc_path=doc_path,
                        tool_errors=tool_errors, quality=quality,
                        detail=f"{doc_size} bytes" if doc_written else "doc file not written",
                    )

                    ui.track_done(area_name, doc_path)
                    return {"area": area_name, "doc_path": doc_path, "status": "completed", "quality": quality}

                except Exception as exc:
                    last_error = exc
                    if attempt == 0:
                        logger.warning("Doc writer failed for area %s (retrying): %s", area_name, exc)
                        ui._write(f"  ↻ {area_name} failed, retrying...")
                    else:
                        logger.error("Doc writer failed for area %s (giving up): %s", area_name, exc)

            # Both attempts failed
            self.db.update_iteration_state(state_id, status="failed")
            self.db.record_area_result(
                run_id=run_id, area_name=area_name, phase="doc_writing",
                status="failed", quality="failed",
                detail=str(last_error)[:200] if last_error else "doc not written after retry",
            )
            ui.track_fail(area_name, str(last_error) if last_error else "doc not written")
            return {"area": area_name, "status": "failed", "error": str(last_error)}

        max_workers = min(self.config.max_concurrent_workers, len(areas))
        with ThreadPoolExecutor(max_workers=max_workers) as executor:
            futures = {
                executor.submit(run_single_doc_writer, area): area
                for area in areas
            }
            remaining = set(futures.keys())
            while remaining:
                if self.shutdown_event.is_set():
                    executor.shutdown(wait=False, cancel_futures=True)
                    raise KeyboardInterrupt("Shutdown requested")
                done = set()
                try:
                    for future in as_completed(remaining, timeout=1.0):
                        done.add(future)
                        area = futures[future]
                        try:
                            doc_result = future.result()
                            results.append(doc_result)
                        except Exception as exc:
                            area_name = area.get("name", "unknown")
                            logger.error("Doc writer thread failed for %s: %s", area_name, exc)
                            ui.track_fail(area_name, str(exc))
                            results.append({"area": area_name, "status": "failed", "error": str(exc)})
                except TimeoutError:
                    pass  # Normal — loop back to check shutdown_event
                remaining -= done

        return results

    # ------------------------------------------------------------------
    # Mode filtering
    # ------------------------------------------------------------------

    def _filter_for_mode(self, plan: dict) -> dict:
        """Filter the area plan based on the documentation mode.

        For fill-gaps: drop areas where every matching file is already documented.
        For refresh: keep only areas that contain stale files.
        Attaches a ``focus_files`` list to each surviving area so explorers
        know which files to prioritise.
        """
        mode = self.config.mode

        if mode == "fresh":
            return plan

        filtered = copy.deepcopy(plan)
        all_files = {f["path"] for f in self.db.get_all_files()}

        if mode == "fill-gaps":
            documented = self.db.get_documented_files()
            # Also include areas that were incomplete in the last run
            incomplete_areas = {
                r["area_name"] for r in self.db.get_incomplete_areas()
            }
            if not documented and not incomplete_areas:
                return filtered
            undocumented = all_files - documented

            # Symbol-level gap detection: find documented files with new symbols
            symbol_changes = self._get_symbol_changes()
            files_with_new_symbols: set[str] = set()
            if symbol_changes:
                for added in symbol_changes.get("added_symbols", []):
                    fp = added.get("file", "") if isinstance(added, dict) else ""
                    if fp and fp in documented and fp not in undocumented:
                        files_with_new_symbols.add(fp)

            # Combine undocumented + files with new symbols
            needs_attention = undocumented | files_with_new_symbols

            filtered["areas"] = [
                area for area in filtered.get("areas", [])
                if self._area_has_matching_files(area, needs_attention)
                or area.get("name") in incomplete_areas
            ]
            for area in filtered.get("areas", []):
                area["focus_files"] = sorted(
                    self._files_matching_area(area, needs_attention)
                )
                if area.get("name") in incomplete_areas:
                    area["retry_reason"] = "incomplete in previous run"
                # Annotate new symbols for the explorer
                if files_with_new_symbols:
                    area_new = self._files_matching_area(area, files_with_new_symbols)
                    if area_new:
                        area["new_symbol_files"] = sorted(area_new)
            return filtered

        if mode == "refresh":
            stale = self.db.get_stale_files()
            if not stale:
                filtered["areas"] = []
                return filtered
            filtered["areas"] = [
                area for area in filtered.get("areas", [])
                if self._area_has_matching_files(area, stale)
            ]
            for area in filtered.get("areas", []):
                area["focus_files"] = sorted(
                    self._files_matching_area(area, stale)
                )

            # Symbol-level change annotation for explorers
            symbol_changes = self._get_symbol_changes()
            if symbol_changes:
                changed_by_file: dict[str, list[dict]] = {}
                for sym in symbol_changes.get("changed_symbols", []):
                    fp = sym.get("file", "") if isinstance(sym, dict) else ""
                    if fp:
                        changed_by_file.setdefault(fp, []).append(sym)
                for added in symbol_changes.get("added_symbols", []):
                    fp = added.get("file", "") if isinstance(added, dict) else ""
                    if fp:
                        changed_by_file.setdefault(fp, []).append(
                            {**added, "change_type": "added"} if isinstance(added, dict) else {"file": fp, "change_type": "added"}
                        )

                for area in filtered.get("areas", []):
                    area_changes = []
                    for fp in area.get("focus_files", []):
                        if fp in changed_by_file:
                            area_changes.extend(changed_by_file[fp])
                    if area_changes:
                        area["symbol_changes"] = area_changes

            return filtered

        return plan

    # ------------------------------------------------------------------
    # Large-file pre-digest with jcodemunch
    # ------------------------------------------------------------------

    _LARGE_FILE_THRESHOLD = 500  # lines

    def _build_file_outlines(self, area: dict) -> str:
        """Pre-compute jcodemunch file outlines for large files in an area.

        Returns a markdown section to append to the explorer's user message,
        or an empty string if no large files or code index unavailable.
        """
        if not self.config.code_index:
            return ""

        patterns = area.get("file_patterns", [])
        if not patterns:
            return ""

        # Resolve patterns to actual files and count lines
        large_files: list[tuple[str, int]] = []  # (rel_path, line_count)
        for pattern in patterns:
            # Patterns can be exact paths or globs
            from pathlib import Path
            base = Path(self.base_dir)
            matches = list(base.glob(pattern))
            if not matches and not any(c in pattern for c in "*?["):
                # Exact path that glob didn't match — try directly
                exact = base / pattern
                if exact.is_file():
                    matches = [exact]

            for fpath in matches:
                if not fpath.is_file():
                    continue
                try:
                    line_count = sum(1 for _ in open(fpath, "r", encoding="utf-8", errors="replace"))
                except OSError:
                    continue
                if line_count > self._LARGE_FILE_THRESHOLD:
                    rel = str(fpath.relative_to(base)).replace("\\", "/")
                    large_files.append((rel, line_count))

        if not large_files:
            return ""

        # Get outlines via code_search tool
        sections: list[str] = []
        for rel_path, line_count in large_files:
            try:
                result_json = self.tool_registry.execute(
                    "code_search",
                    {"action": "file_outline", "file_path": rel_path},
                )
                result = json.loads(result_json)
                symbols = result.get("symbols", [])
                if not symbols:
                    # Fallback: just note the file is large
                    sections.append(
                        f"### `{rel_path}` ({line_count} lines)\n\n"
                        f"*No outline available — read in chunks of 200 lines using start_line/end_line.*\n"
                    )
                    continue

                # Format symbols as a compact table
                lines = [f"### `{rel_path}` ({line_count} lines, {len(symbols)} symbols)\n"]
                lines.append("| Symbol | Kind | Line | Signature |")
                lines.append("|--------|------|------|-----------|")
                for sym in symbols:
                    name = sym.get("name", "?")
                    kind = sym.get("kind", "?")
                    line = sym.get("line", "?")
                    sig = sym.get("signature", "")
                    # Truncate long signatures
                    if len(sig) > 120:
                        sig = sig[:117] + "..."
                    # Escape pipes in signature
                    sig = sig.replace("|", "\\|")
                    lines.append(f"| `{name}` | {kind} | {line} | `{sig}` |")
                sections.append("\n".join(lines) + "\n")
            except Exception:
                # Non-fatal — explorer will read the file normally
                sections.append(
                    f"### `{rel_path}` ({line_count} lines)\n\n"
                    f"*Outline unavailable — read in chunks of 200 lines using start_line/end_line.*\n"
                )

        header = (
            "\n## Pre-computed File Outlines\n\n"
            "The following large files have been pre-analyzed. "
            "**Use `code_search` with `get_source` to read specific symbols "
            "instead of reading the full file with `read_file`.** "
            "For sections not covered by a symbol, use `read_file` with `start_line`/`end_line`.\n\n"
        )
        return header + "\n".join(sections)

    def _build_area_dependencies(self, area: dict) -> str:
        """Pre-compute dependency graph for all files in an area.

        Returns a compact markdown section showing imports/importers per file,
        or empty string if code index unavailable.
        """
        if not self.config.code_index:
            return ""

        try:
            patterns = area.get("file_patterns", [])
            if not patterns:
                return ""

            # Resolve to actual files
            from pathlib import Path
            base = Path(self.base_dir)
            area_files: list[str] = []
            for pattern in patterns:
                matches = list(base.glob(pattern))
                if not matches and not any(c in pattern for c in "*?["):
                    exact = base / pattern
                    if exact.is_file():
                        matches = [exact]
                for fpath in matches:
                    if fpath.is_file():
                        area_files.append(str(fpath.relative_to(base)).replace("\\", "/"))

            if not area_files:
                return ""

            sections: list[str] = []
            for fp in area_files[:15]:  # cap files
                try:
                    result_json = self.tool_registry.execute(
                        "code_search",
                        {"action": "dependency_graph", "file_path": fp, "direction": "both", "depth": 1},
                    )
                    result = json.loads(result_json)
                    if "error" in result:
                        continue

                    # Extract from neighbors dict
                    neighbors = result.get("neighbors", {})
                    file_info = neighbors.get(fp, {})
                    imp_names = file_info.get("imports", [])[:6]
                    importer_names = file_info.get("imported_by", [])[:6]
                    # Fallback: extract from edges
                    if not imp_names and not importer_names:
                        for edge in result.get("edges", []):
                            if isinstance(edge, list) and len(edge) == 2:
                                if edge[0] == fp and len(imp_names) < 6:
                                    imp_names.append(edge[1])
                                elif edge[1] == fp and len(importer_names) < 6:
                                    importer_names.append(edge[0])

                    if imp_names or importer_names:
                        lines = [f"### `{fp}`"]
                        if imp_names:
                            lines.append(f"Imports: {', '.join(f'`{n}`' for n in imp_names)}")
                        if importer_names:
                            lines.append(f"Imported by: {', '.join(f'`{n}`' for n in importer_names)}")
                        sections.append("\n".join(lines))
                except Exception:
                    continue

            if not sections:
                return ""

            content = "\n\n".join(sections)
            # Cap at 3000 chars
            if len(content) > 3000:
                content = content[:2997] + "..."

            return (
                f"\n## Pre-computed Dependencies\n\n"
                f"Import/importer relationships from code index. "
                f"Use this to understand cross-area references without extra tool calls.\n\n"
                f"{content}\n"
            )

        except Exception:
            return ""

    # ------------------------------------------------------------------
    # Project context reading for planner
    # ------------------------------------------------------------------

    def _read_project_context(self) -> str:
        """Read README and instructions files to give the planner domain context.

        Checks for README.md, CLAUDE.md, AGENTS.md, GEMINI.md in the target repo.
        Returns a markdown section capped at 3000 chars, or empty string if nothing found.
        """
        sections: list[str] = []

        # 1. README.md — project description, features, architecture
        for readme_name in ("README.md", "readme.md", "Readme.md"):
            readme_path = os.path.join(self.base_dir, readme_name)
            if os.path.isfile(readme_path):
                try:
                    with open(readme_path, "r", encoding="utf-8", errors="replace") as f:
                        content = f.read(4000)  # cap raw read
                    if content.strip():
                        sections.append(f"### README.md\n\n{content.strip()}")
                except Exception:
                    pass
                break

        # 2. Instructions files — CLAUDE.md, AGENTS.md, GEMINI.md
        for instr_name in ("CLAUDE.md", "AGENTS.md", "GEMINI.md"):
            instr_path = os.path.join(self.base_dir, instr_name)
            if os.path.isfile(instr_path):
                try:
                    with open(instr_path, "r", encoding="utf-8", errors="replace") as f:
                        content = f.read(4000)
                    if content.strip():
                        sections.append(f"### {instr_name}\n\n{content.strip()}")
                except Exception:
                    pass
                break  # only read the first instructions file found

        if not sections:
            return ""

        combined = "\n\n".join(sections)
        # Cap total at 3000 chars to avoid blowing the planner's token budget
        if len(combined) > 3000:
            combined = combined[:2997] + "..."

        return f"## Project Context\n\nExisting project documentation (use for domain understanding, naming, and feature context — may contain inaccuracies):\n\n{combined}"

    # ------------------------------------------------------------------
    # Workflow analysis pre-computation for planner
    # ------------------------------------------------------------------

    def _build_workflow_analysis(self, repo_analysis: Optional[dict] = None) -> str:
        """Build a workflow-based analysis for the planner using jcodemunch call-graph tools.

        Traces function call chains from entry points to detect workflows that span
        multiple files. Returns a markdown section for the planner's user message.
        Falls back to empty string if code index is unavailable or repo is too small.
        """
        if not self.config.code_index:
            return ""

        import time as _time
        _wf_start = _time.monotonic()

        parts: list[str] = []

        # Step 1: Repo health triage (one-call aggregator)
        health: dict = {}
        try:
            result_json = self.tool_registry.execute(
                "code_search", {"action": "repo_health", "days": 90},
            )
            health = json.loads(result_json)
            if not health.get("error"):
                lines = ["### Repo Health"]
                lines.append(
                    f"- {health.get('total_files', 0)} files, "
                    f"{health.get('total_symbols', 0)} symbols, "
                    f"avg complexity {health.get('avg_complexity', 0):.1f}"
                )
                dead_pct = health.get("dead_code_pct", 0)
                if dead_pct > 0:
                    lines.append(f"- Dead code: {dead_pct:.1f}% ({health.get('dead_count', 0)} functions likely unused)")
                cycle_count = health.get("cycle_count", 0)
                if cycle_count > 0:
                    lines.append(f"- Dependency cycles: {cycle_count}")
                    for cycle in health.get("cycles_sample", [])[:5]:
                        if isinstance(cycle, list):
                            lines.append(f"  - {' → '.join(f'`{f}`' for f in cycle)}")
                unstable = health.get("unstable_modules", 0)
                if unstable > 0:
                    lines.append(f"- Unstable modules: {unstable} (high outgoing dependencies)")
                hotspots = health.get("top_hotspots", [])
                if hotspots:
                    hs_parts = []
                    for hs in hotspots[:5]:
                        name = hs.get("name", "?")
                        fpath = hs.get("file", "?")
                        score = hs.get("hotspot_score", 0)
                        assessment = hs.get("assessment", "?").upper()
                        hs_parts.append(f"`{fpath}::{name}` (score {score:.1f} {assessment})")
                    lines.append(f"- Top hotspots: {', '.join(hs_parts)}")
                parts.append("\n".join(lines))
        except Exception as exc:
            logger.debug("Repo health failed: %s", exc)

        # Small repos: skip workflow detection, just provide health + basic info
        total_symbols = health.get("total_symbols", 0) or health.get("fn_method_count", 0)
        if total_symbols < 20:
            logger.info("Small repo (%d symbols) — skipping workflow analysis", total_symbols)
            if parts:
                return f"\n## Workflow Analysis\n\n{chr(10).join(parts)}\n"
            return ""

        # Step 2: Entry point detection via symbol importance (PageRank top 50)
        top_symbols: list[dict] = []
        try:
            result_json = self.tool_registry.execute(
                "code_search", {"action": "symbol_importance", "limit": 50},
            )
            result = json.loads(result_json)
            top_symbols = result.get("ranked_symbols", [])
        except Exception as exc:
            logger.debug("Symbol importance failed: %s", exc)

        if not top_symbols:
            if parts:
                return f"\n## Workflow Analysis\n\n{chr(10).join(parts)}\n"
            return ""

        # Step 3: Filter entry points — symbols with callees >= 2 and callers <= callees
        # Skip common short names that cause false positive call links via word-token matching.
        # jcodemunch's call detection is import-constrained (only checks importing files),
        # but common names like "get" or "run" still match within import-connected files.
        _COMMON_NAMES = frozenset({
            "get", "set", "run", "init", "main", "new", "open", "close",
            "read", "write", "start", "stop", "call", "apply", "update",
            "delete", "create", "load", "save", "parse", "format", "reset",
            "execute", "process", "handle", "setup", "configure",
            "__init__", "__str__", "__repr__", "__eq__", "__hash__",
            "__enter__", "__exit__", "__call__", "__len__", "__iter__",
        })

        entry_points: list[dict] = []
        _wf_calls = 0
        for sym in top_symbols[:50]:
            if self.shutdown_event and self.shutdown_event.is_set():
                break
            if _wf_calls >= 100:
                break
            sym_id = sym.get("symbol_id", "")
            if not sym_id:
                continue
            # Extract name for common-name filtering
            sym_name = sym_id.split("::")[-1].split("#")[0] if "::" in sym_id else sym_id
            try:
                _wf_calls += 1
                ch_json = self.tool_registry.execute(
                    "code_search", {
                        "action": "call_hierarchy", "symbol_id": sym_id,
                        "direction": "both", "depth": 1,
                    },
                )
                ch = json.loads(ch_json)
                if ch.get("error"):
                    continue
                callers_count = len(ch.get("callers", []))
                callees_count = len(ch.get("callees", []))
                # Skip common names unless they have many unique callees (real hubs)
                if sym_name in _COMMON_NAMES and callees_count < 5:
                    continue
                # Skip symbols with too many callers — likely utility functions with
                # false-positive word-token matches, not genuine entry points
                if callers_count > 30:
                    continue
                if callees_count >= 2 and callers_count <= callees_count:
                    entry_points.append({
                        "symbol_id": sym_id,
                        "name": sym_name,
                        "file": sym_id.split("::")[0] if "::" in sym_id else "",
                        "score": sym.get("score", 0),
                        "callers": callers_count,
                        "callees": callees_count,
                    })
            except Exception:
                continue
            if len(entry_points) >= 20:
                break

        # Step 4: Trace workflow chains from each entry point (depth=3 callees)
        workflows: list[dict] = []
        for ep in entry_points:
            if self.shutdown_event and self.shutdown_event.is_set():
                break
            if _wf_calls >= 100:
                break
            try:
                _wf_calls += 1
                chain_json = self.tool_registry.execute(
                    "code_search", {
                        "action": "call_hierarchy", "symbol_id": ep["symbol_id"],
                        "direction": "callees", "depth": 3,
                    },
                )
                chain = json.loads(chain_json)
                if chain.get("error"):
                    continue
                callees = chain.get("callees", [])
                sym_ids = [ep["symbol_id"]] + [c.get("id", "") for c in callees if c.get("id")]
                files = list(dict.fromkeys(
                    [ep["file"]] + [c.get("file", "") for c in callees if c.get("file")]
                ))
                sym_names = [ep["name"]] + [c.get("name", "") for c in callees if c.get("name")]
                workflows.append({
                    "entry_point": ep,
                    "symbols": sym_ids,
                    "symbol_names": sym_names[:15],
                    "files": files,
                })
            except Exception:
                continue

        if not workflows:
            if parts:
                return f"\n## Workflow Analysis\n\n{chr(10).join(parts)}\n"
            return ""

        # Step 5: Cluster overlapping workflows (>50% symbol overlap → merge)
        clusters: list[dict] = []
        used: set[int] = set()
        for i, w in enumerate(workflows):
            if i in used:
                continue
            cluster = {
                "entry_points": [w["entry_point"]],
                "symbols": set(w["symbols"]),
                "symbol_names": list(w["symbol_names"]),
                "files": list(w["files"]),
            }
            for j in range(i + 1, len(workflows)):
                if j in used:
                    continue
                other_syms = set(workflows[j]["symbols"])
                union_size = len(cluster["symbols"] | other_syms)
                if union_size == 0:
                    continue
                overlap = len(cluster["symbols"] & other_syms) / union_size
                if overlap > 0.5:
                    cluster["entry_points"].append(workflows[j]["entry_point"])
                    cluster["symbols"] |= other_syms
                    for f in workflows[j]["files"]:
                        if f not in cluster["files"]:
                            cluster["files"].append(f)
                    for n in workflows[j]["symbol_names"]:
                        if n not in cluster["symbol_names"] and len(cluster["symbol_names"]) < 20:
                            cluster["symbol_names"].append(n)
                    used.add(j)
            cluster["symbols"] = list(cluster["symbols"])
            clusters.append(cluster)

        # Step 6: Related symbols gap-filling (expand clusters with nearby symbols)
        for cluster in clusters:
            if self.shutdown_event and self.shutdown_event.is_set():
                break
            if _wf_calls >= 100:
                break
            ep_id = cluster["entry_points"][0]["symbol_id"]
            try:
                _wf_calls += 1
                rel_json = self.tool_registry.execute(
                    "code_search", {"action": "related_symbols", "symbol_id": ep_id, "limit": 15},
                )
                rel = json.loads(rel_json)
                if rel.get("error"):
                    continue
                for r in rel.get("related", []):
                    rid = r.get("id", "")
                    if rid and rid not in cluster["symbols"]:
                        cluster["symbols"].append(rid)
                        rfile = r.get("file", "")
                        if rfile and rfile not in cluster["files"]:
                            cluster["files"].append(rfile)
                        rname = r.get("name", "")
                        if rname and rname not in cluster["symbol_names"] and len(cluster["symbol_names"]) < 20:
                            cluster["symbol_names"].append(rname)
            except Exception:
                continue

        # Step 7: Annotate with hotspots from repo_health
        hotspot_map: dict[str, dict] = {}
        for hs in health.get("top_hotspots", []):
            hs_file = hs.get("file", "")
            if hs_file:
                hotspot_map[hs_file] = hs

        # Step 8: Build workflow descriptions
        workflow_lines: list[str] = ["### Detected Workflows (by call-graph analysis)\n"]
        for idx, cluster in enumerate(clusters, 1):
            ep_descs = []
            for ep in cluster["entry_points"]:
                ep_descs.append(f"`{ep['symbol_id']}` (PageRank {ep['score']:.3f})")
            entry_str = ", ".join(ep_descs)

            sym_count = len(cluster["symbols"])
            chain_preview = " → ".join(cluster["symbol_names"][:10])
            if len(cluster["symbol_names"]) > 10:
                chain_preview += " → ..."

            file_list = ", ".join(f"`{f}`" for f in cluster["files"][:10])
            if len(cluster["files"]) > 10:
                file_list += f" +{len(cluster['files']) - 10} more"

            workflow_lines.append(f"**Workflow {idx}**")
            workflow_lines.append(f"- Entry points: {entry_str}")
            workflow_lines.append(f"- Symbol chain ({sym_count} symbols): {chain_preview}")
            workflow_lines.append(f"- Files: {file_list}")

            # Hotspot annotation
            cluster_hotspots = []
            for f in cluster["files"]:
                if f in hotspot_map:
                    hs = hotspot_map[f]
                    cluster_hotspots.append(
                        f"`{f}::{hs.get('name', '?')}` ({hs.get('assessment', '?').upper()})"
                    )
            if cluster_hotspots:
                workflow_lines.append(f"- Hotspots: {', '.join(cluster_hotspots)}")
            workflow_lines.append("")

        parts.append("\n".join(workflow_lines))

        # Step 9: Cross-workflow connections
        cross_connections: list[str] = []
        for i in range(len(clusters)):
            for j in range(i + 1, len(clusters)):
                shared_files = set(clusters[i]["files"]) & set(clusters[j]["files"])
                if shared_files:
                    shared_str = ", ".join(f"`{f}`" for f in list(shared_files)[:5])
                    cross_connections.append(f"- Workflows {i+1}↔{j+1} share: {shared_str}")
        if cross_connections:
            parts.append("### Cross-workflow Connections\n" + "\n".join(cross_connections))

        # Step 10: Orphan detection
        all_workflow_files = set()
        for cluster in clusters:
            all_workflow_files.update(cluster["files"])
        all_files = self.db.get_all_files()
        source_files = {
            f["path"] for f in all_files
            if f.get("category") in ("source", "ui")
        }
        orphan_files = sorted(source_files - all_workflow_files)
        if orphan_files:
            orphan_str = ", ".join(f"`{f}`" for f in orphan_files[:20])
            if len(orphan_files) > 20:
                orphan_str += f" +{len(orphan_files) - 20} more"
            parts.append(f"### Orphan Files (not in any workflow — group by directory)\n{orphan_str}")

        # Step 11: Dependency cycles from repo_health
        cycles = health.get("cycles_sample", [])
        if cycles:
            cycle_lines = ["### Dependency Cycles (must stay in same area)"]
            for i, cycle in enumerate(cycles[:5], 1):
                if isinstance(cycle, list):
                    cycle_lines.append(f"{i}. {' → '.join(f'`{f}`' for f in cycle)}")
            parts.append("\n".join(cycle_lines))

        section = "\n\n".join(parts)
        # Cap at 6000 chars (workflow analysis is richer than old AST analysis)
        if len(section) > 6000:
            section = section[:5997] + "..."

        elapsed = _time.monotonic() - _wf_start
        logger.info(
            "Workflow analysis: %d workflows, %d clusters, %d calls in %.1fs",
            len(workflows), len(clusters), _wf_calls, elapsed,
        )

        return f"\n## Workflow Analysis\n\n{section}\n"

    # ------------------------------------------------------------------
    # AST pre-computation for doc writers
    # ------------------------------------------------------------------

    def _build_doc_impact_data(self, report: dict) -> str:
        """Pre-compute blast radius and coupling for key exports in an area.

        Returns a markdown section or empty string if unavailable.
        """
        if not self.config.code_index:
            return ""

        try:
            # Collect top exports from explorer report
            exports: list[dict] = []
            for f in report.get("files", []):
                file_path = f.get("path", "")
                for exp in f.get("exports", [])[:5]:
                    name = exp if isinstance(exp, str) else exp.get("name", str(exp))
                    exports.append({"name": name, "file": file_path})
                for kf in f.get("key_functions", [])[:3]:
                    name = kf if isinstance(kf, str) else kf.get("name", str(kf))
                    exports.append({"name": name, "file": file_path})

            if not exports:
                return ""

            # Deduplicate and cap
            seen = set()
            unique_exports: list[dict] = []
            for exp in exports:
                key = (exp["name"], exp["file"])
                if key not in seen:
                    seen.add(key)
                    unique_exports.append(exp)
            unique_exports = unique_exports[:10]

            # Pre-compute blast radius for each
            blast_lines: list[str] = []
            for exp in unique_exports:
                try:
                    # Search for the symbol to get its ID
                    search_json = self.tool_registry.execute(
                        "code_search",
                        {"action": "search", "query": exp["name"], "file_pattern": exp["file"], "limit": 1},
                    )
                    search_result = json.loads(search_json)
                    results = search_result.get("results", [])
                    if not results:
                        continue
                    symbol_id = results[0].get("id", "")
                    if not symbol_id:
                        continue

                    blast_json = self.tool_registry.execute(
                        "code_search",
                        {"action": "blast_radius", "symbol_id": symbol_id, "depth": 1},
                    )
                    blast_result = json.loads(blast_json)
                    if "error" in blast_result:
                        continue

                    affected = blast_result.get("affected_files", blast_result.get("files", []))
                    if isinstance(affected, list) and affected:
                        file_names = [
                            a.get("file", str(a)) if isinstance(a, dict) else str(a)
                            for a in affected[:5]
                        ]
                        blast_lines.append(
                            f"- **`{exp['name']}`** (`{exp['file']}`): affects {len(affected)} file(s) — "
                            + ", ".join(f"`{f}`" for f in file_names)
                        )
                except Exception:
                    continue

            if not blast_lines:
                return ""

            section = "\n".join(blast_lines)
            if len(section) > 3000:
                section = section[:2997] + "..."

            return (
                f"## Pre-computed Impact Data\n\n"
                f"Blast radius for key exports (from code index):\n\n"
                f"{section}\n\n"
            )

        except Exception:
            return ""

    def _build_doc_file_outlines(self, report: dict) -> str:
        """Pre-compute file outlines for all files in an area (for doc writer).

        Returns a markdown section or empty string. Caps at 20 files.
        """
        if not self.config.code_index:
            return ""

        try:
            files = report.get("files", [])
            if not files:
                return ""

            # Cap at 20 files
            files_to_outline = files[:20]

            sections: list[str] = []
            for f in files_to_outline:
                file_path = f.get("path", "")
                if not file_path:
                    continue
                try:
                    result_json = self.tool_registry.execute(
                        "code_search",
                        {"action": "file_outline", "file_path": file_path},
                    )
                    result = json.loads(result_json)
                    symbols = result.get("symbols", [])
                    if not symbols:
                        continue

                    lines = [f"### `{file_path}` ({len(symbols)} symbols)\n"]
                    lines.append("| Symbol | Kind | Line | Signature |")
                    lines.append("|--------|------|------|-----------|")
                    for sym in symbols[:20]:  # cap symbols per file
                        name = sym.get("name", "?")
                        kind = sym.get("kind", "?")
                        line = sym.get("line", "?")
                        sig = sym.get("signature", "")
                        if len(sig) > 100:
                            sig = sig[:97] + "..."
                        sig = sig.replace("|", "\\|")
                        lines.append(f"| `{name}` | {kind} | {line} | `{sig}` |")
                    sections.append("\n".join(lines))
                except Exception:
                    continue

            if not sections:
                return ""

            content = "\n\n".join(sections)
            # Cap at 5000 chars
            if len(content) > 5000:
                content = content[:4997] + "..."

            return (
                f"## File Outlines (AST-derived)\n\n"
                f"Precise function/class definitions from code index. "
                f"Use these for the Definitions tables instead of reading files.\n\n"
                f"{content}\n\n"
            )

        except Exception:
            return ""

    # ------------------------------------------------------------------
    # Symbol-level change detection
    # ------------------------------------------------------------------

    def _get_symbol_changes(self) -> Optional[dict]:
        """Get symbol-level changes since the last indexed run.

        Returns the parsed result from code_search changed_symbols action,
        or None if unavailable (no git, no previous SHA, no code index).
        Cached per run.
        """
        if hasattr(self, "_cached_symbol_changes"):
            return self._cached_symbol_changes

        self._cached_symbol_changes: Optional[dict] = None

        if not self.config.code_index:
            return None

        try:
            last_sha = self.db.get_last_index_sha()
            if not last_sha:
                return None

            result_json = self.tool_registry.execute(
                "code_search",
                {"action": "changed_symbols", "since_sha": last_sha, "include_blast_radius": False},
            )
            result = json.loads(result_json)
            if "error" not in result:
                self._cached_symbol_changes = result
        except Exception:
            pass

        return self._cached_symbol_changes

    # ------------------------------------------------------------------
    # AST analysis pre-computation for relationship mapper
    # ------------------------------------------------------------------

    def _build_cross_area_deps(self, plan: dict) -> str:
        """Pre-compute AST-derived cross-area dependencies for the relationship mapper.

        For each area's files, runs dependency_graph and cross-references against
        the area plan to find inter-area import edges.
        Returns a markdown section or empty string if unavailable.
        """
        if not self.config.code_index:
            return ""

        try:
            areas = plan.get("areas", [])
            if not areas:
                return ""

            # Build file-to-area mapping
            file_to_area: dict[str, str] = {}
            area_files: dict[str, list[str]] = {}
            all_source = {f["path"] for f in self.db.get_all_files() if f.get("category") in ("source", "ui")}

            for area in areas:
                area_name = area.get("name", "unknown")
                matched = self._files_matching_area(area, all_source)
                area_files[area_name] = sorted(matched)
                for fp in matched:
                    file_to_area[fp] = area_name

            # Collect cross-area edges
            cross_edges: dict[tuple[str, str], list[str]] = {}  # (from_area, to_area) -> [details]
            files_sampled = 0

            for area_name, files in area_files.items():
                for fp in files[:10]:  # cap per area to avoid slowness
                    try:
                        result_json = self.tool_registry.execute(
                            "code_search",
                            {"action": "dependency_graph", "file_path": fp, "direction": "imports", "depth": 1},
                        )
                        result = json.loads(result_json)
                        # Extract imports from neighbors or edges
                        neighbors = result.get("neighbors", {})
                        file_info = neighbors.get(fp, {})
                        dep_files = file_info.get("imports", [])
                        if not dep_files:
                            for edge in result.get("edges", []):
                                if isinstance(edge, list) and len(edge) == 2 and edge[0] == fp:
                                    dep_files.append(edge[1])
                        for dep_file in dep_files:
                            dep_area = file_to_area.get(dep_file)
                            if dep_area and dep_area != area_name:
                                key = (area_name, dep_area)
                                if key not in cross_edges:
                                    cross_edges[key] = []
                                detail = f"`{fp}` → `{dep_file}`"
                                if len(cross_edges[key]) < 5:  # cap details per edge
                                    cross_edges[key].append(detail)
                    except Exception:
                        continue
                    files_sampled += 1
                    if files_sampled >= 60:
                        break
                if files_sampled >= 60:
                    break

            if not cross_edges:
                return ""

            # Format edges
            lines = []
            for (from_a, to_a), details in sorted(cross_edges.items())[:100]:
                lines.append(f"- **{from_a}** → **{to_a}** ({len(details)} imports)")
                for d in details[:3]:
                    lines.append(f"  - {d}")

            section = "\n".join(lines)
            # Cap at 5000 chars
            if len(section) > 5000:
                section = section[:4997] + "..."

            return (
                f"\n## AST-Derived Cross-Area Dependencies\n\n"
                f"Verified import relationships between areas (from code index):\n\n"
                f"{section}\n"
            )

        except Exception:
            return ""

    def _build_area_coupling_summary(self, plan: dict) -> str:
        """Compute per-area coupling metrics summary for the relationship mapper.

        Returns a markdown table or empty string if unavailable.
        """
        if not self.config.code_index:
            return ""

        try:
            areas = plan.get("areas", [])
            if not areas:
                return ""

            all_source = {f["path"] for f in self.db.get_all_files() if f.get("category") in ("source", "ui")}
            rows: list[tuple[str, float, int, int]] = []

            for area in areas:
                area_name = area.get("name", "unknown")
                matched = self._files_matching_area(area, all_source)
                if not matched:
                    continue

                total_ca, total_ce, count = 0, 0, 0
                for fp in list(matched)[:8]:  # sample per area
                    try:
                        result_json = self.tool_registry.execute(
                            "code_search",
                            {"action": "coupling_metrics", "file_path": fp},
                        )
                        result = json.loads(result_json)
                        if "error" not in result:
                            total_ca += result.get("ca", result.get("afferent_coupling", 0))
                            total_ce += result.get("ce", result.get("efferent_coupling", 0))
                            count += 1
                    except Exception:
                        continue

                if count > 0:
                    avg_instability = total_ce / max(total_ca + total_ce, 1)
                    rows.append((area_name, avg_instability, total_ca, total_ce))

            if not rows:
                return ""

            lines = ["| Area | Avg Instability | Inbound (Ca) | Outbound (Ce) |"]
            lines.append("|------|----------------|-------------|---------------|")
            for name, inst, ca, ce in sorted(rows, key=lambda r: r[1]):
                lines.append(f"| {name} | {inst:.2f} | {ca} | {ce} |")

            return (
                f"\n## Area Coupling Summary\n\n"
                + "\n".join(lines) + "\n"
            )

        except Exception:
            return ""

    @staticmethod
    def _area_prefixes_and_exact(area: dict) -> tuple[list[str], set[str]]:
        """Extract directory prefixes and exact file names from file_patterns.

        Turns ``["src/api/**/*.py", "src/api/utils.py"]`` into
        prefixes ``["src/api/"]`` and exact ``{"src/api/utils.py"}``.

        Root-level files (no directory, no glob) become exact matches
        instead of being silently dropped.
        """
        prefixes: list[str] = []
        exact: set[str] = set()
        for pat in area.get("file_patterns", []):
            has_glob = "*" in pat or "?" in pat
            has_slash = "/" in pat

            if not has_glob:
                # Exact file path — only match this specific file.
                # Do NOT extract a directory prefix (that would match
                # all siblings, e.g. templates/a.html would pull in
                # every file under templates/).
                exact.add(pat)
                continue

            # Glob pattern: strip wildcards to get leading directory
            clean = pat.split("*")[0].split("?")[0]
            if clean and not clean.endswith("/"):
                idx = clean.rfind("/")
                clean = clean[: idx + 1] if idx >= 0 else ""
            if clean:
                prefixes.append(clean)
        return prefixes, exact

    @classmethod
    def _files_matching_area(cls, area: dict, file_paths: set[str]) -> set[str]:
        """Return the subset of *file_paths* that fall under the area's patterns."""
        prefixes, exact = cls._area_prefixes_and_exact(area)
        matched = exact & file_paths
        if prefixes:
            matched |= {p for p in file_paths if any(p.startswith(pfx) for pfx in prefixes)}
        return matched

    @classmethod
    def _area_has_matching_files(cls, area: dict, file_paths: set[str]) -> bool:
        return bool(cls._files_matching_area(area, file_paths))

    # ------------------------------------------------------------------
    # Utility methods
    # ------------------------------------------------------------------

    def _get_file_tree(self) -> str:
        """Get the file tree as text using the get_file_tree tool."""
        callables = self.tool_registry.get_tool_callables(["get_file_tree"])
        if "get_file_tree" in callables:
            result_str = callables["get_file_tree"](max_depth=4)
            try:
                result = json.loads(result_str)
                return result.get("tree", result_str)
            except (json.JSONDecodeError, TypeError):
                return str(result_str)
        return "(file tree unavailable)"

    # Categories that should be documented by the pipeline
    _DOCUMENTABLE_CATEGORIES = {"source", "ui"}

    def _get_repo_metadata(self) -> dict:
        """Gather repository metadata from the database."""
        all_files = self.db.get_all_files()

        # Count by language
        lang_counts: dict[str, int] = {}
        total_lines = 0
        cat_counts: dict[str, int] = {}
        for f in all_files:
            lang = f.get("language")
            if lang:
                lang_counts[lang] = lang_counts.get(lang, 0) + 1
            total_lines += f.get("line_count", 0)
            cat = f.get("category", "other")
            cat_counts[cat] = cat_counts.get(cat, 0) + 1

        # Top-level directories
        top_dirs: set[str] = set()
        for f in all_files:
            path = f.get("path", "")
            parts = path.split("/")
            if len(parts) > 1:
                top_dirs.add(parts[0])

        documentable = sum(cat_counts.get(c, 0) for c in self._DOCUMENTABLE_CATEGORIES)

        return {
            "total_files": len(all_files),
            "documentable_files": documentable,
            "total_lines": total_lines,
            "languages": dict(sorted(lang_counts.items(), key=lambda x: -x[1])),
            "categories": dict(sorted(cat_counts.items(), key=lambda x: -x[1])),
            "top_level_directories": sorted(top_dirs),
        }

    def _finalize(self, run_id: int, doc_results: list[dict], area_plan: dict, relationship_map: Optional[dict] = None) -> None:
        """Record generated docs in DB and print summary."""
        completed = [r for r in doc_results if r.get("status") == "completed"]
        failed = [r for r in doc_results if r.get("status") == "failed"]
        skipped = [r for r in doc_results if r.get("status") == "skipped"]

        # Record docs in DB — link each documented area to its matching files
        all_files = self.db.get_all_files()
        all_file_paths = {f["path"] for f in all_files}
        files_by_path = {f["path"]: f for f in all_files}
        areas_list = area_plan.get("areas", []) if area_plan else []

        for result in completed:
            doc_path = result.get("doc_path", "")
            area_name = result.get("area", "")
            if not doc_path or not area_name:
                continue
            # Find the area definition to get file_patterns for matching
            area_def = next(
                (a for a in areas_list if a.get("name") == area_name),
                None,
            )
            if not area_def:
                continue
            matching = self._files_matching_area(area_def, all_file_paths)
            for path in matching:
                f = files_by_path.get(path)
                if f:
                    self.db.record_doc(
                        file_id=f["id"],
                        area_name=area_name,
                        doc_path=doc_path,
                        run_id=run_id,
                        source_hash=f.get("content_hash", ""),
                        source_line_count=f.get("line_count"),
                    )

        # Re-link ALL existing doc files on disk to their matching source files.
        # This catches docs from previous runs that the current plan still covers
        # (e.g., refresh/fill-gaps modes that only update a subset of areas).
        self._relink_existing_docs(run_id, areas_list, all_file_paths, files_by_path)

        # Ensure RULES_INDEX.md is updated for every completed doc.
        # The LLM is instructed to do this but doesn't always follow through,
        # so we do it programmatically as a guarantee.
        if completed:
            from spark.tools.update_rules_index import execute as _update_index
            from spark.tools.install_templates import detect_platform, PLATFORM_MAP
            platform = detect_platform(self.base_dir)
            plat_dir = PLATFORM_MAP[platform]["dir"]
            idx_path = f"{plat_dir}/RULES_INDEX.md"

            for result in completed:
                doc_path = result.get("doc_path", "")
                area_name = result.get("area", "")
                if not doc_path or not area_name:
                    continue

                # Build a one-line summary from the doc file's Purpose section
                summary = area_name.replace("-", " ").title()
                full_doc = os.path.join(self.base_dir, doc_path)
                if os.path.isfile(full_doc):
                    try:
                        with open(full_doc, "r", encoding="utf-8") as fh:
                            for line in fh:
                                line = line.strip()
                                if not line or line.startswith("#") or line.startswith(">") or line.startswith("## "):
                                    continue
                                summary = line[:150]
                                break
                    except OSError:
                        pass

                # entry_path: relative to the index file's directory
                # Index is at .claude/RULES_INDEX.md
                # Doc is at .claude/rules/docs/area.md
                # So entry_path = rules/docs/area.md
                entry_rel = doc_path.replace(f"{plat_dir}/", "", 1)
                _update_index(
                    index_path=idx_path,
                    entry_path=entry_rel,
                    summary=summary,
                    section="Documentation Rules",
                    _base_dir=self.base_dir,
                )

        # Clean up orphaned doc files from previous runs (fresh mode only)
        if self.config.mode == "fresh" and completed:
            self._cleanup_orphaned_docs(completed)

        # Generate project overview & feature index
        if completed:
            self._run_overview_writer(run_id, area_plan, relationship_map, completed)

        # Set up coding agent access to code index (jCodeMunch)
        if self.config.code_index:
            from spark.code_index import finalize_code_index
            from spark.tools.install_templates import detect_platform
            platform = detect_platform(self.base_dir)
            ci_result = finalize_code_index(self.base_dir, platform)
            if ci_result.get("mcp_config") or ci_result.get("skill_installed"):
                c = ui.c
                ui._write("")
                ui._write(f"  {c.ACCENT}◆ jCodeMunch Code Intelligence{c.RESET}")
                if ci_result.get("mcp_config"):
                    ui._write(f"    {c.GREEN}✓{c.RESET} MCP server configured {c.DIM}— Claude Code will have code_search tools{c.RESET}")
                if ci_result.get("skill_installed"):
                    ui._write(f"    {c.GREEN}✓{c.RESET} Code search skill installed")
                if ci_result.get("instructions_injected"):
                    from spark.tools.install_templates import PLATFORM_MAP
                    instr_file = PLATFORM_MAP.get(platform, PLATFORM_MAP["claude"])["instructions"]
                    ui._write(f"    {c.GREEN}✓{c.RESET} Usage guide injected into {instr_file}")
                ui._write(f"    {c.DIM}Available: dependency graph, blast radius, symbol complexity, repo outline{c.RESET}")
                ui._write("")

        # Print quality report
        area_results = self.db.get_area_results_for_run(run_id)
        if area_results:
            c = ui.c
            partial = [r for r in area_results if r["quality"] == "partial"]
            failed_q = [r for r in area_results if r["quality"] == "failed"]
            if partial or failed_q:
                ui._write(f"\n  {c.WARN}Areas needing attention:{c.RESET}")
                for r in partial:
                    ui._write(
                        f"    {c.YELLOW}◐{c.RESET} {r['area_name']} "
                        f"{c.DIM}({r['phase']}: {r.get('detail', 'partial')}){c.RESET}"
                    )
                for r in failed_q:
                    ui._write(
                        f"    {c.RED}✗{c.RESET} {r['area_name']} "
                        f"{c.DIM}({r['phase']}: {r.get('detail', 'failed')}){c.RESET}"
                    )
                ui._write(f"  {c.DIM}Run with --mode fill-gaps to retry incomplete areas{c.RESET}")

        # Print summary
        ui.summary(
            areas_completed=len(completed),
            areas_failed=len(failed),
            areas_skipped=len(skipped),
        )

    # ------------------------------------------------------------------
    # Overview Writer
    # ------------------------------------------------------------------

    def _run_overview_writer(
        self,
        run_id: int,
        area_plan: dict,
        relationship_map: Optional[dict],
        completed_docs: list[dict],
    ) -> None:
        """Generate a project overview & feature index from all area docs."""
        from spark.tools.install_templates import detect_platform, PLATFORM_MAP

        platform = detect_platform(self.base_dir)
        plat_dir = PLATFORM_MAP[platform]["dir"]
        overview_path = os.path.join(
            self.base_dir, plat_dir, "rules", "docs", "project-overview.md"
        )

        try:
            ui.phase("Overview", "Generating project overview & feature index")

            # Gather area info from generated doc files
            _SECTION_LIMIT = 500  # chars — sections longer than this get LLM-summarized

            # Step 1: Extract raw sections from each area doc
            area_raw: list[dict] = []  # {area, sections}
            for area in area_plan.get("areas", []):
                name = area.get("name", "")
                doc_file = os.path.join(
                    self.base_dir, plat_dir, "rules", "docs", f"{name}.md"
                )
                sections: dict[str, str] = {}
                if os.path.isfile(doc_file):
                    try:
                        with open(doc_file, "r", encoding="utf-8") as fh:
                            current_section = ""
                            section_lines: list[str] = []
                            for line in fh:
                                if line.strip().startswith("## "):
                                    if current_section and section_lines:
                                        text = " ".join(
                                            l.strip() for l in section_lines if l.strip()
                                        )
                                        sections[current_section] = text
                                    heading = line.strip().lstrip("# ").strip()
                                    if heading in ("Purpose", "Data Flows", "Definitions"):
                                        current_section = heading
                                        section_lines = []
                                    else:
                                        current_section = ""
                                        section_lines = []
                                elif current_section:
                                    section_lines.append(line.rstrip())
                            if current_section and section_lines:
                                text = " ".join(
                                    l.strip() for l in section_lines if l.strip()
                                )
                                sections[current_section] = text
                    except OSError:
                        pass
                area_raw.append({"area": area, "sections": sections})

            # Step 2: Batch-summarize oversized sections in one LLM call
            oversized: list[tuple[int, str, str]] = []  # (area_idx, section_name, text)
            for i, entry in enumerate(area_raw):
                for sec_name, sec_text in entry["sections"].items():
                    if len(sec_text) > _SECTION_LIMIT:
                        oversized.append((i, sec_name, sec_text))

            if oversized:
                summarize_prompt = (
                    "Summarize each section below to under 400 characters. "
                    "Preserve all key facts: names, mechanisms, data flows, "
                    "function signatures. Remove filler words and redundancy. "
                    "Return ONLY numbered summaries matching the input numbers.\n\n"
                )
                for idx, (area_i, sec_name, sec_text) in enumerate(oversized):
                    area_name = area_raw[area_i]["area"].get("name", "?")
                    summarize_prompt += (
                        f"[{idx+1}] {area_name} / {sec_name}:\n{sec_text}\n\n"
                    )
                try:
                    sum_model = self.agent_defs.get("overview_writer", {}).get(
                        "model", "z-ai/glm-5-turbo"
                    )
                    sum_resp = self.client.chat_completion(
                        model=sum_model,
                        messages=[{"role": "user", "content": summarize_prompt}],
                        max_tokens=2048,
                        temperature=0.0,
                    )
                    sum_text = sum_resp.get("content", "")
                    # Parse numbered summaries: [1] ..., [2] ..., etc.
                    import re as _re
                    parts = _re.split(r"\[(\d+)\]\s*", sum_text)
                    # parts = ['', '1', 'summary1', '2', 'summary2', ...]
                    for j in range(1, len(parts) - 1, 2):
                        num = int(parts[j]) - 1
                        summary = parts[j + 1].strip()
                        if 0 <= num < len(oversized):
                            area_i, sec_name, _ = oversized[num]
                            area_raw[area_i]["sections"][sec_name] = summary[:500]
                except Exception as exc:
                    logger.debug("Section summarization failed (using truncation): %s", exc)
                    # Fallback: hard truncate
                    for area_i, sec_name, sec_text in oversized:
                        area_raw[area_i]["sections"][sec_name] = sec_text[:_SECTION_LIMIT]

            # Step 3: Build area summaries from (now condensed) sections
            area_summaries: list[str] = []
            for entry in area_raw:
                area = entry["area"]
                sections = entry["sections"]
                name = area.get("name", "")
                desc = area.get("description", "")
                priority = area.get("priority", 3)

                summary_parts = [
                    f"### {name} (P{priority})",
                    f"Description: {desc}",
                    f"Files: {', '.join(area.get('file_patterns', []))}",
                ]
                if sections.get("Purpose"):
                    summary_parts.append(f"Purpose: {sections['Purpose']}")
                else:
                    summary_parts.append("Purpose: (not yet documented)")
                if sections.get("Data Flows"):
                    summary_parts.append(f"Data flows: {sections['Data Flows']}")
                if sections.get("Definitions"):
                    summary_parts.append(f"Key definitions: {sections['Definitions']}")
                area_summaries.append("\n".join(summary_parts))

            # Build relationship summary
            rel_summary = ""
            if relationship_map:
                edges = relationship_map.get("edges", [])
                if edges:
                    edge_lines = [
                        f"- {e.get('from_area', '?')} -> {e.get('to_area', '?')} ({e.get('type', 'imports')})"
                        for e in edges[:30]
                    ]
                    rel_summary += "## Dependency Edges\n" + "\n".join(edge_lines)

                flows = relationship_map.get("data_flows", [])
                if flows:
                    flow_lines = [
                        f"- {f.get('name', '?')}: {' -> '.join(f.get('path', []))}"
                        for f in flows[:10]
                    ]
                    rel_summary += "\n\n## Data Flows\n" + "\n".join(flow_lines)

                shared = relationship_map.get("shared_types", [])
                if shared:
                    type_lines = [
                        f"- {t.get('name', '?')}: used in {', '.join(t.get('areas', []))}"
                        for t in shared[:10]
                    ]
                    rel_summary += "\n\n## Shared Types\n" + "\n".join(type_lines)

            # Repo metadata
            repo_meta = self._get_repo_metadata()
            meta_summary = (
                f"Languages: {', '.join(f'{k} ({v})' for k, v in sorted(repo_meta.get('languages', {}).items(), key=lambda x: -x[1])[:5])}\n"
                f"Total files: {repo_meta.get('total_files', 0)}\n"
                f"Total lines: {repo_meta.get('total_lines', 0)}\n"
                f"Top directories: {', '.join(repo_meta.get('top_dirs', []))}"
            )

            # Project name
            project_name = os.path.basename(os.path.abspath(self.base_dir))
            init_json = os.path.join(self.base_dir, plat_dir, "spark_plans", "spark_init.json")
            if os.path.isfile(init_json):
                try:
                    with open(init_json, "r", encoding="utf-8") as fh:
                        init_data = json.load(fh)
                        project_name = init_data.get("project_name", project_name)
                except (json.JSONDecodeError, OSError):
                    pass

            user_message = (
                f"# Project: {project_name}\n\n"
                f"## Repo Metadata\n{meta_summary}\n\n"
                f"## Areas\n\n" + "\n\n".join(area_summaries) + "\n\n"
            )
            if rel_summary:
                user_message += f"## Relationship Map\n\n{rel_summary}\n\n"
            user_message += (
                "Generate the project overview document following the template exactly. "
                "Output ONLY the markdown content."
            )

            # Load agent def and prompt
            agent_def = self.agent_defs.get("overview_writer", {})
            model = agent_def.get("model", "z-ai/glm-5-turbo")
            system_prompt = self._load_prompt("overview_writer")

            response = self.client.chat_completion(
                model=model,
                messages=[
                    {"role": "system", "content": system_prompt},
                    {"role": "user", "content": user_message},
                ],
                max_tokens=agent_def.get("max_tokens", 8192),
                temperature=agent_def.get("temperature", 0.1),
            )

            overview_content = response.get("content", "")
            if not overview_content or len(overview_content) < 100:
                logger.warning("Overview writer returned insufficient content")
                ui.phase_end("skipped (insufficient content)")
                return

            # Write the file
            os.makedirs(os.path.dirname(overview_path), exist_ok=True)
            with open(overview_path, "w", encoding="utf-8") as fh:
                fh.write(overview_content)

            # Register in RULES_INDEX.md
            from spark.tools.update_rules_index import execute as _update_index
            _update_index(
                index_path=f"{plat_dir}/RULES_INDEX.md",
                entry_path="docs/project-overview.md",
                summary=f"High-level project description, feature index, and architecture overview linking to all area docs",
                section="Project Overview",
                _base_dir=self.base_dir,
            )

            ui.phase_end("project-overview.md written")

        except Exception as exc:
            logger.warning("Overview writer failed (non-fatal): %s", exc)
            ui.phase_end("skipped (error)")

    # ------------------------------------------------------------------
    # Adopt Mode Pipeline
    # ------------------------------------------------------------------

    def _run_adopt_pipeline(self, run_id: int) -> dict:
        """Run the adopt mode pipeline: detect stale adopted docs and patch them."""
        from spark.tools.install_templates import detect_platform, PLATFORM_MAP

        platform = detect_platform(self.base_dir)
        plat_dir = PLATFORM_MAP[platform]["dir"]

        # Check if we have adopted docs
        if not self.db.has_adopted_docs():
            ui.phase("Adopt", "No adopted docs found")
            ui.phase_end("Run adopt mode on a repo with existing docs in .claude/rules/docs/")
            self.db.complete_run(run_id)
            return {"run_id": run_id, "status": "no_adopted_docs"}

        adopted = self.db.get_adopted_docs()
        ui.phase("Adopt", f"Checking {len(adopted)} adopted docs for staleness")

        # Find which adopted docs have stale source files
        stale_docs: list[dict] = []
        for doc in adopted:
            area_name = doc["area_name"]
            # Get the source files linked to this doc
            stale_files = self._get_stale_files_for_area(area_name)
            if stale_files:
                stale_docs.append({
                    **doc,
                    "stale_files": stale_files,
                })

        if not stale_docs:
            ui.phase_end("All adopted docs are current")
            self.db.complete_run(run_id)
            return {"run_id": run_id, "status": "all_current", "adopted_count": len(adopted)}

        ui.phase_end(f"{len(stale_docs)} docs have stale source files")

        # Run doc patchers concurrently
        ui.phase("Patching", f"Updating {len(stale_docs)} docs")
        ui.track_reset()
        patch_results = self._run_doc_patchers(run_id, stale_docs, plat_dir)

        # Review and apply patches
        applied = 0
        for result in patch_results:
            if result.get("status") == "applied":
                applied += 1

        ui.phase_end(f"{applied}/{len(stale_docs)} docs patched")
        self.db.complete_run(run_id)
        return {
            "run_id": run_id,
            "status": "completed",
            "patched": applied,
            "total_stale": len(stale_docs),
        }

    def _get_stale_files_for_area(self, area_name: str) -> list[str]:
        """Return source files linked to an adopted doc that have changed."""
        with self.db._lock:
            rows = self.db.conn.execute(
                """
                SELECT f.path, f.content_hash, d.source_hash
                FROM docs d
                JOIN files f ON f.id = d.file_id
                WHERE d.area_name = ? AND d.source = 'adopted' AND d.status = 'current'
                """,
                (area_name,),
            ).fetchall()
        stale = []
        for path, current_hash, doc_hash in rows:
            if current_hash and doc_hash and current_hash != doc_hash:
                stale.append(path)
        return stale

    def _run_doc_patchers(
        self, run_id: int, stale_docs: list[dict], plat_dir: str
    ) -> list[dict]:
        """Run doc patcher agents concurrently for stale adopted docs."""
        from concurrent.futures import ThreadPoolExecutor, as_completed

        results: list[dict] = []
        max_workers = min(self.config.max_concurrent_workers, len(stale_docs))

        with ThreadPoolExecutor(max_workers=max_workers) as pool:
            futures = {
                pool.submit(
                    self._patch_single_doc, run_id, doc, plat_dir
                ): doc
                for doc in stale_docs
            }
            for future in as_completed(futures):
                doc = futures[future]
                try:
                    result = future.result()
                    results.append(result)
                    ui.track_done(doc["area_name"])
                except Exception as exc:
                    logger.warning("Patcher failed for %s: %s", doc["area_name"], exc)
                    results.append({
                        "area_name": doc["area_name"],
                        "status": "failed",
                        "error": str(exc),
                    })
                    ui.track_done(doc["area_name"])

        return results

    def _patch_single_doc(
        self, run_id: int, doc_info: dict, plat_dir: str
    ) -> dict:
        """Patch a single adopted doc using the doc_patcher agent."""
        import difflib

        area_name = doc_info["area_name"]
        doc_path = doc_info["doc_path"]
        stale_files = doc_info["stale_files"]

        # Read existing doc
        abs_path = os.path.join(self.base_dir, doc_path.replace("/", os.sep))
        if not os.path.isfile(abs_path):
            return {"area_name": area_name, "status": "missing", "error": "doc file not found"}

        with open(abs_path, "r", encoding="utf-8") as fh:
            original_content = fh.read()

        # Check if user manually edited since adoption
        import hashlib
        current_doc_hash = hashlib.sha256(original_content.encode("utf-8")).hexdigest()
        if doc_info.get("doc_content_hash") and current_doc_hash != doc_info["doc_content_hash"]:
            logger.info("Doc %s was manually edited since adoption — proceeding with caution", area_name)

        # Build change manifest from stale files
        change_entries: list[str] = []
        for fpath in stale_files:
            # Try to get file outline for new state
            try:
                from spark.tools.code_search import execute as cs_exec
                outline_json = cs_exec(
                    action="file_outline", file_path=fpath,
                    _base_dir=self.base_dir,
                )
                outline = json.loads(outline_json)
                symbols = outline.get("symbols", [])
                sym_summary = ", ".join(
                    f"{s['name']}({s['kind']})" for s in symbols[:10]
                )
                change_entries.append(
                    f"- **{fpath}** — file changed. Current symbols: {sym_summary}"
                )
            except Exception:
                change_entries.append(f"- **{fpath}** — file changed (outline unavailable)")

        change_manifest = (
            f"## Change Manifest for {area_name}\n\n"
            f"The following source files have changed since this doc was last verified:\n\n"
            + "\n".join(change_entries)
            + "\n\nUpdate the doc sections that describe these files. "
            "Preserve all other content exactly as-is."
        )

        # Build user message
        user_message = (
            f"## Existing Documentation\n\n"
            f"```markdown\n{original_content}\n```\n\n"
            f"{change_manifest}"
        )

        # Run patcher agent
        agent_def = self.agent_defs.get("doc_patcher", {})
        model = agent_def.get("model", "anthropic/claude-opus-4.6")
        system_prompt = self._load_prompt("doc_patcher")

        tool_names = agent_def.get("tools", ["read_file", "code_search"])
        tools = self.registry.get_tools_for_role(tool_names)
        tool_callables = self.registry.get_tool_callables(tool_names)

        session = _LightweightSession()
        session.chat_history.append({"role": "user", "content": user_message})
        session.context["system_message"] = system_prompt

        try:
            from spark.engine.loop import process_agentic_loop_native
            response = process_agentic_loop_native(
                layer_def={
                    "model": model,
                    "system_message": system_prompt,
                    "max_iterations": agent_def.get("max_iterations", 15),
                    "max_tokens": agent_def.get("max_tokens", 8192),
                    "temperature": agent_def.get("temperature", 0.1),
                },
                session=session,
                client=self.client,
                tool_registry=tool_callables,
                knowledge={},
                tools=tools,
            )
        except Exception as exc:
            return {"area_name": area_name, "status": "failed", "error": str(exc)}

        new_content = response.get("content", "")
        if not new_content or len(new_content) < 50:
            return {"area_name": area_name, "status": "failed", "error": "patcher returned insufficient content"}

        # Strip markdown code fences if the patcher wrapped the output
        if new_content.startswith("```"):
            lines = new_content.split("\n")
            if lines[0].startswith("```"):
                lines = lines[1:]
            if lines and lines[-1].strip() == "```":
                lines = lines[:-1]
            new_content = "\n".join(lines)

        # Compute diff
        original_lines = original_content.splitlines(keepends=True)
        new_lines = new_content.splitlines(keepends=True)
        diff = list(difflib.unified_diff(
            original_lines, new_lines,
            fromfile=f"a/{doc_path}", tofile=f"b/{doc_path}",
        ))

        if not diff:
            return {"area_name": area_name, "status": "unchanged"}

        # Write the patched doc (backup original first)
        backup_path = abs_path + ".bak"
        try:
            with open(backup_path, "w", encoding="utf-8") as fh:
                fh.write(original_content)
        except OSError:
            pass  # backup failed, continue anyway

        with open(abs_path, "w", encoding="utf-8") as fh:
            fh.write(new_content)

        # Update doc hashes in DB
        new_hash = hashlib.sha256(new_content.encode("utf-8")).hexdigest()
        with self.db._lock:
            self.db.conn.execute(
                "UPDATE docs SET doc_content_hash = ? WHERE area_name = ? AND source = 'adopted'",
                (new_hash, area_name),
            )
            # Update source hashes for the stale files
            for fpath in stale_files:
                abs_src = os.path.join(self.base_dir, fpath.replace("/", os.sep))
                if os.path.isfile(abs_src):
                    from spark.db import _sha256
                    new_src_hash = _sha256(abs_src)
                    self.db.conn.execute(
                        """
                        UPDATE docs SET source_hash = ?
                        WHERE area_name = ? AND source = 'adopted'
                        AND file_id IN (SELECT id FROM files WHERE path = ?)
                        """,
                        (new_src_hash, area_name, fpath),
                    )
            self.db.conn.commit()

        diff_summary = f"{sum(1 for l in diff if l.startswith('+') and not l.startswith('+++'))} additions, " \
                       f"{sum(1 for l in diff if l.startswith('-') and not l.startswith('---'))} deletions"
        logger.info("Patched %s: %s", area_name, diff_summary)

        return {
            "area_name": area_name,
            "status": "applied",
            "diff_summary": diff_summary,
            "backup": backup_path,
        }

    def _relink_existing_docs(
        self,
        run_id: int,
        areas_list: list[dict],
        all_file_paths: set[str],
        files_by_path: dict[str, dict],
    ) -> None:
        """Re-link doc files from previous runs to matching source files.

        When refresh/fill-gaps mode only updates a few areas, docs from earlier
        runs still exist on disk but have no doc-file links for newly-scanned
        files (or files whose categories changed). This scans all doc files on
        disk and ensures every matching source file has a current link.
        """
        from spark.tools.install_templates import detect_platform, PLATFORM_MAP

        platform = detect_platform(self.base_dir)
        plat_dir = PLATFORM_MAP[platform]["dir"]
        docs_dir = os.path.join(self.base_dir, plat_dir, "rules", "docs")
        if not os.path.isdir(docs_dir):
            return

        # Build set of already-linked (area_name, file_path) pairs
        existing_links: set[tuple[str, str]] = set()
        try:
            with self.db._lock:
                cur = self.db.conn.execute(
                    "SELECT DISTINCT d.area_name, f.path "
                    "FROM docs d JOIN files f ON f.id = d.file_id "
                    "WHERE d.status = 'current'"
                )
                existing_links = {(row[0], row[1]) for row in cur.fetchall()}
        except Exception:
            existing_links = set()

        # For each doc file on disk, find its area in the plan and link missing files
        for fname in os.listdir(docs_dir):
            if not fname.endswith(".md"):
                continue
            area_name = fname[:-3]  # strip .md
            doc_rel = f"{plat_dir}/rules/docs/{fname}"

            # Find area definition — try exact match, then fuzzy prefix match
            area_def = next(
                (a for a in areas_list if a.get("name") == area_name),
                None,
            )
            if not area_def:
                continue

            matching = self._files_matching_area(area_def, all_file_paths)
            for path in matching:
                if (area_name, path) in existing_links:
                    continue  # already linked
                f = files_by_path.get(path)
                if f:
                    self.db.record_doc(
                        file_id=f["id"],
                        area_name=area_name,
                        doc_path=doc_rel,
                        run_id=run_id,
                        source_hash=f.get("content_hash", ""),
                        source_line_count=f.get("line_count"),
                    )

    def _cleanup_orphaned_docs(self, completed: list[dict]) -> None:
        """Remove doc files from previous runs that aren't in the current run.

        Only called in fresh mode. Scans the docs directory for .md files,
        compares against what was just generated, and removes orphans along
        with their RULES_INDEX.md entries.
        """
        from spark.tools.install_templates import detect_platform, PLATFORM_MAP

        platform = detect_platform(self.base_dir)
        plat_dir = PLATFORM_MAP[platform]["dir"]
        docs_dir = os.path.join(self.base_dir, plat_dir, "rules", "docs")

        if not os.path.isdir(docs_dir):
            return

        # Collect doc file basenames generated in this run
        current_docs: set[str] = set()
        for result in completed:
            doc_path = result.get("doc_path", "")
            if doc_path:
                current_docs.add(os.path.basename(doc_path))

        # Find orphaned .md files (not generated in this run, not .gitkeep)
        removed: list[str] = []
        for fname in os.listdir(docs_dir):
            if not fname.endswith(".md"):
                continue
            if fname in current_docs:
                continue
            # This doc file wasn't generated in the current run — remove it
            orphan_path = os.path.join(docs_dir, fname)
            try:
                os.remove(orphan_path)
                removed.append(fname)
            except OSError:
                pass

        if not removed:
            return

        # Clean orphaned entries from the database
        orphan_doc_paths = [
            f"{plat_dir}/rules/docs/{fname}" for fname in removed
        ]
        deleted_count = self.db.delete_docs_by_path(orphan_doc_paths)

        # Clean orphaned entries from RULES_INDEX.md
        idx_path = os.path.join(self.base_dir, plat_dir, "RULES_INDEX.md")
        if os.path.isfile(idx_path):
            try:
                with open(idx_path, "r", encoding="utf-8") as fh:
                    lines = fh.readlines()
                # Remove lines referencing orphaned doc files
                cleaned = []
                for line in lines:
                    if any(fname in line for fname in removed):
                        continue
                    cleaned.append(line)
                with open(idx_path, "w", encoding="utf-8") as fh:
                    fh.writelines(cleaned)
            except OSError:
                pass

        c = ui.c
        ui._write(f"\n  {c.ACCENT}◆ Cleanup{c.RESET}")
        ui._write(
            f"    {c.GREEN}✓{c.RESET} Removed {len(removed)} orphaned doc"
            f"{'s' if len(removed) != 1 else ''} from previous runs"
            f" {c.DIM}({deleted_count} DB entries){c.RESET}"
        )
        for fname in sorted(removed):
            ui._write(f"    {c.DIM}  × {fname}{c.RESET}")

    def _resume_run(self) -> int:
        """Resume the last interrupted run, or start fresh if none found."""
        last_run = self.db.get_last_run()
        if last_run and last_run.get("status") == "running":
            run_id = last_run["id"]
            print(f"  Resuming run #{run_id} (iteration {last_run.get('iterations_completed', 0)})...")
            return run_id

        # No interrupted run — start fresh
        print("  No interrupted run found. Starting fresh.")
        return self.db.start_run(
            mode=self.config.mode,
            iterations=self.config.iterations,
            config_snapshot=json.dumps({
                "models": self.config.models,
                "iterations": self.config.iterations,
                "mode": self.config.mode,
            }),
        )
