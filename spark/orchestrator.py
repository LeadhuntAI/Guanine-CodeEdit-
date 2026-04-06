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
        self.client = OpenRouterClient(api_key=config.api_key)
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
        for name in ["planner", "explorer", "relationship_mapper", "doc_writer"]:
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
            self._finalize(run_id, doc_results)

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
    ) -> dict:
        """Run the planner agent and return the area plan dict."""
        state_id = self.db.save_iteration_state(
            run_id, iteration, "planning", None, None, "running",
        )

        # Build user message
        user_parts: list[str] = []
        user_parts.append(f"## Repository Structure\n\n```\n{file_tree}\n```")
        user_parts.append(
            f"## Repository Metadata\n\n```json\n{json.dumps(repo_metadata, indent=2)}\n```"
        )
        user_parts.append(f"## Documentation Mode\n\nMode: **{self.config.mode}**")

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

            user_message = (
                f"## Your Area Assignment\n\n"
                f"**Area:** {area_name}\n"
                f"**Description:** {area.get('description', '')}\n"
                f"**File patterns:** {json.dumps(area.get('file_patterns', []))}\n\n"
                f"## Plan Summary\n\n{plan_summary}\n"
                f"{focus_section}\n"
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
            }

            last_error: Exception | None = None
            for attempt in range(2):  # 1 try + 1 retry
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

                    self.db.update_iteration_state(
                        state_id, output_json=json.dumps(report), status="completed",
                    )
                    ui.track_done(area_name, f"{len(report.get('files', []))} files analyzed")
                    return report

                except Exception as exc:
                    last_error = exc
                    if attempt == 0:
                        logger.warning("Explorer failed for area %s (retrying): %s", area_name, exc)
                        ui._write(f"  ↻ {area_name} failed, retrying...")
                    else:
                        logger.error("Explorer failed for area %s (giving up): %s", area_name, exc)

            # Both attempts failed
            self.db.update_iteration_state(state_id, status="failed")
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
            for future in as_completed(futures):
                if self.shutdown_event.is_set():
                    executor.shutdown(wait=False, cancel_futures=True)
                    raise KeyboardInterrupt("Shutdown requested")
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
        # Doc writers also need write_file and update_rules_index
        all_tool_names = list(set(tool_names) | {"write_file", "update_rules_index"})

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
            user_message += (
                f"## Instructions\n\n"
                f"Write the documentation file to `.claude/rules/docs/{area_name}.md` "
                f"following the document template in your system prompt. "
                f"Use `write_file` to create the file. "
                f"Then update the rules index using `update_rules_index` with:\n"
                f"- index_path: `.claude/rules/RULES_INDEX.md`\n"
                f"- entry_path: `docs/{area_name}.md`\n"
                f"- summary: A one-line summary of this area\n"
                f"- section: `Documentation Rules`"
            )

            layer_def = {
                "model": model,
                "system_message": self._load_prompt("doc_writer"),
                "user_message": user_message,
                "max_iterations": agent_def.get("max_iterations", 15),
                "max_tokens": agent_def.get("max_tokens", 8192),
                "temperature": agent_def.get("temperature", 0.1),
            }

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
                self.db.update_iteration_state(
                    state_id,
                    output_json=json.dumps({"doc_path": doc_path}),
                    status="completed",
                )
                ui.track_done(area_name, doc_path)
                return {"area": area_name, "doc_path": doc_path, "status": "completed"}

            except Exception as exc:
                logger.error("Doc writer failed for area %s: %s", area_name, exc)
                self.db.update_iteration_state(state_id, status="failed")
                ui.track_fail(area_name, str(exc))
                return {"area": area_name, "status": "failed", "error": str(exc)}

        max_workers = min(self.config.max_concurrent_workers, len(areas))
        with ThreadPoolExecutor(max_workers=max_workers) as executor:
            futures = {
                executor.submit(run_single_doc_writer, area): area
                for area in areas
            }
            for future in as_completed(futures):
                if self.shutdown_event.is_set():
                    executor.shutdown(wait=False, cancel_futures=True)
                    raise KeyboardInterrupt("Shutdown requested")
                area = futures[future]
                try:
                    doc_result = future.result()
                    results.append(doc_result)
                except Exception as exc:
                    area_name = area.get("name", "unknown")
                    logger.error("Doc writer thread failed for %s: %s", area_name, exc)
                    ui.track_fail(area_name, str(exc))
                    results.append({"area": area_name, "status": "failed", "error": str(exc)})

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
            if not documented:
                return filtered
            undocumented = all_files - documented
            filtered["areas"] = [
                area for area in filtered.get("areas", [])
                if self._area_has_matching_files(area, undocumented)
            ]
            for area in filtered.get("areas", []):
                area["focus_files"] = sorted(
                    self._files_matching_area(area, undocumented)
                )
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
            return filtered

        return plan

    @staticmethod
    def _area_prefixes(area: dict) -> list[str]:
        """Extract directory prefixes from an area's file_patterns.

        Turns ``["src/api/**/*.py", "src/api/utils.py"]`` into ``["src/api/"]``.
        Falls back to the raw pattern (minus glob chars) if it contains no slash.
        """
        prefixes: list[str] = []
        for pat in area.get("file_patterns", []):
            # Strip glob wildcards to get the leading directory
            clean = pat.split("*")[0].split("?")[0]
            if clean and not clean.endswith("/"):
                # Take directory portion
                idx = clean.rfind("/")
                clean = clean[: idx + 1] if idx >= 0 else ""
            if clean:
                prefixes.append(clean)
        return prefixes

    @classmethod
    def _files_matching_area(cls, area: dict, file_paths: set[str]) -> set[str]:
        """Return the subset of *file_paths* that fall under the area's prefixes."""
        prefixes = cls._area_prefixes(area)
        if not prefixes:
            return set()
        return {p for p in file_paths if any(p.startswith(pfx) for pfx in prefixes)}

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

    def _get_repo_metadata(self) -> dict:
        """Gather repository metadata from the database."""
        all_files = self.db.get_all_files()

        # Count by language
        lang_counts: dict[str, int] = {}
        total_lines = 0
        for f in all_files:
            lang = f.get("language")
            if lang:
                lang_counts[lang] = lang_counts.get(lang, 0) + 1
            total_lines += f.get("line_count", 0)

        # Top-level directories
        top_dirs: set[str] = set()
        for f in all_files:
            path = f.get("path", "")
            parts = path.split("/")
            if len(parts) > 1:
                top_dirs.add(parts[0])

        return {
            "total_files": len(all_files),
            "total_lines": total_lines,
            "languages": dict(sorted(lang_counts.items(), key=lambda x: -x[1])),
            "top_level_directories": sorted(top_dirs),
        }

    def _finalize(self, run_id: int, doc_results: list[dict]) -> None:
        """Record generated docs in DB and print summary."""
        completed = [r for r in doc_results if r.get("status") == "completed"]
        failed = [r for r in doc_results if r.get("status") == "failed"]
        skipped = [r for r in doc_results if r.get("status") == "skipped"]

        # Record docs in DB
        for result in completed:
            doc_path = result.get("doc_path", "")
            area_name = result.get("area", "")
            if doc_path and area_name:
                # Find files in this area to link docs
                all_files = self.db.get_all_files()
                for f in all_files:
                    if f.get("area_name") == area_name:
                        self.db.record_doc(
                            file_id=f["id"],
                            area_name=area_name,
                            doc_path=doc_path,
                            run_id=run_id,
                            source_hash=f.get("content_hash", ""),
                        )

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
                ui._write(f"    {c.DIM}Available: dependency graph, blast radius, symbol complexity, repo outline{c.RESET}")
                ui._write("")

        # Print summary
        ui.summary(
            areas_completed=len(completed),
            areas_failed=len(failed),
            areas_skipped=len(skipped),
        )

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
