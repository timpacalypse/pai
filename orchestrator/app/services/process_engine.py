"""Process Engine — executes multi-step workflows defined as configuration.

Handles four step types: skill, agent, decision, gate.
Steps share an accumulating context object with reference resolution.
"""

import asyncio
import json
import logging
import time
from datetime import datetime, timezone
from uuid import uuid4

import httpx
from sqlalchemy import text

from app.core.database import async_session

logger = logging.getLogger("pai.process_engine")


# ── Reference Resolution ──────────────────────────────────────


def resolve_references(value, context: dict) -> object:
    """Recursively resolve dot-notation references in a value.

    Reference format:
      steps.<step_id>.<output_key>  — output from a previous step
      context.<key>.<subkey>        — role context, identity info
      trigger.<key>                 — original trigger parameters

    Non-string values (int, float, bool, None) pass through unchanged.
    Strings that don't match a reference pattern pass through as literals.
    Dicts and lists are traversed recursively.
    """
    if isinstance(value, str):
        return _resolve_single_ref(value, context)
    elif isinstance(value, dict):
        return {k: resolve_references(v, context) for k, v in value.items()}
    elif isinstance(value, list):
        return [resolve_references(v, context) for v in value]
    return value


def _resolve_single_ref(ref: str, context: dict) -> object:
    """Resolve a single dotted reference against the context.

    Returns the original string if it doesn't look like a reference.
    """
    prefixes = ("steps.", "context.", "trigger.")
    if not any(ref.startswith(p) for p in prefixes):
        return ref

    parts = ref.split(".")
    current = context
    for part in parts:
        if isinstance(current, dict) and part in current:
            current = current[part]
        else:
            raise KeyError(f"Reference '{ref}' could not be resolved — missing key '{part}'")
    return current


# ── Safe Expression Evaluator for Decision Steps ──────────────


def evaluate_condition(condition: str, context: dict) -> bool:
    """Evaluate a simple condition string against the process context.

    Supported operators: ==, !=, >, <, >=, <=, in, not_in, exists, not_exists
    Format: "<reference> <operator> <value>"
    Or: "exists <reference>" / "not_exists <reference>"

    Does NOT use eval(). All parsing is explicit.
    """
    condition = condition.strip()

    # exists / not_exists
    if condition.startswith("exists "):
        ref = condition[7:].strip()
        try:
            resolve_references(ref, context)
            return True
        except KeyError:
            return False
    if condition.startswith("not_exists "):
        ref = condition[11:].strip()
        try:
            resolve_references(ref, context)
            return False
        except KeyError:
            return True

    # Binary operators (ordered longest-first to avoid prefix conflicts)
    operators = ["!=", ">=", "<=", "==", ">", "<", " not_in ", " in "]
    op = None
    left_str = ""
    right_str = ""
    for candidate in operators:
        idx = condition.find(candidate)
        if idx >= 0:
            op = candidate.strip()
            left_str = condition[:idx].strip()
            right_str = condition[idx + len(candidate):].strip()
            break

    if op is None:
        raise ValueError(f"Unsupported condition format: '{condition}'")

    # Resolve left side (typically a reference)
    left = resolve_references(left_str, context)

    # Parse right side as a literal value
    right = _parse_literal(right_str)

    if op == "==":
        return left == right
    elif op == "!=":
        return left != right
    elif op == ">":
        return float(left) > float(right)
    elif op == "<":
        return float(left) < float(right)
    elif op == ">=":
        return float(left) >= float(right)
    elif op == "<=":
        return float(left) <= float(right)
    elif op == "in":
        return left in right
    elif op == "not_in":
        return left not in right
    else:
        raise ValueError(f"Unsupported operator: '{op}'")


def _parse_literal(s: str) -> object:
    """Parse a string into a Python literal (str, int, float, bool, None, list)."""
    s = s.strip()
    if s.lower() == "true":
        return True
    if s.lower() == "false":
        return False
    if s.lower() == "none" or s.lower() == "null":
        return None
    if s.startswith("[") and s.endswith("]"):
        try:
            return json.loads(s)
        except json.JSONDecodeError:
            pass
    if s.startswith('"') and s.endswith('"'):
        return s[1:-1]
    if s.startswith("'") and s.endswith("'"):
        return s[1:-1]
    try:
        return int(s)
    except ValueError:
        pass
    try:
        return float(s)
    except ValueError:
        pass
    # If it looks like a reference, resolve it
    if any(s.startswith(p) for p in ("steps.", "context.", "trigger.")):
        # Deferred — would need context, but we don't have it here
        # This is for the right-hand side, so treat as string literal
        pass
    return s


# ── Process Execution Engine ──────────────────────────────────


async def start_process(
    process_id: str,
    params: dict | None = None,
    role: str | None = None,
    http_client: httpx.AsyncClient | None = None,
) -> dict:
    """Start a new process execution.

    Creates an execution record, initialises the context, and begins stepping.
    """
    definition = await get_process_definition(process_id)
    if not definition:
        return {"error": f"Process '{process_id}' not found"}
    if not definition["is_active"]:
        return {"error": f"Process '{process_id}' is inactive"}

    execution_id = str(uuid4())
    trigger_params = params or {}
    role_name = role or (definition["roles"][0] if definition["roles"] else "")

    # Build initial context
    process_context = {
        "trigger": trigger_params,
        "context": {"role": {"name": role_name}},
        "steps": {},
    }

    # Persist execution record
    async with async_session() as session:
        await session.execute(
            text("""
                INSERT INTO process_executions
                    (execution_id, process_id, status, current_step_idx,
                     process_context, trigger_params, role, step_log)
                VALUES
                    (:eid, :pid, 'running', 0,
                     CAST(:ctx AS jsonb), CAST(:tp AS jsonb), :role, '[]'::jsonb)
            """),
            {
                "eid": execution_id,
                "pid": process_id,
                "ctx": json.dumps(process_context),
                "tp": json.dumps(trigger_params),
                "role": role_name,
            },
        )
        await session.commit()

    # Execute steps
    result = await _execute_steps(
        execution_id=execution_id,
        steps=definition["steps"],
        process_context=process_context,
        http_client=http_client,
    )
    return result


async def resume_process(
    execution_id: str,
    decision: str,
    message: str = "",
    modifications: dict | None = None,
    http_client: httpx.AsyncClient | None = None,
) -> dict:
    """Resume a paused (gated) process execution.

    decision: 'approve', 'reject', or 'modify'
    """
    execution = await get_execution(execution_id)
    if not execution:
        return {"error": f"Execution '{execution_id}' not found"}
    if execution["status"] != "paused":
        return {"error": f"Execution is '{execution['status']}', not paused"}

    definition = await get_process_definition(execution["process_id"])
    if not definition:
        return {"error": "Process definition not found"}

    process_context = execution["process_context"]
    step_idx = execution["current_step_idx"]
    steps = definition["steps"]

    # Record gate response in context
    current_step = steps[step_idx]
    process_context["steps"][current_step["id"]] = {
        "gate_decision": decision,
        "gate_message": message,
        "gate_modifications": modifications or {},
    }

    # If rejected, cancel
    if decision == "reject":
        await _update_execution(
            execution_id,
            status="cancelled",
            process_context=process_context,
            step_log=execution["step_log"],
            error="Gate rejected by user",
        )
        return await get_execution(execution_id)

    # If modify, merge modifications into context
    if decision == "modify" and modifications:
        process_context["trigger"].update(modifications)

    # Move to next step
    new_idx = step_idx + 1
    await _update_execution(
        execution_id,
        status="running",
        current_step_idx=new_idx,
        process_context=process_context,
        step_log=execution["step_log"],
    )

    # Continue execution from next step
    result = await _execute_steps(
        execution_id=execution_id,
        steps=steps,
        process_context=process_context,
        start_idx=new_idx,
        step_log=execution["step_log"],
        http_client=http_client,
    )
    return result


async def cancel_execution(execution_id: str) -> dict:
    """Cancel a running or paused execution."""
    execution = await get_execution(execution_id)
    if not execution:
        return {"error": f"Execution '{execution_id}' not found"}
    if execution["status"] in ("completed", "failed", "cancelled"):
        return {"error": f"Cannot cancel — execution is '{execution['status']}'"}

    await _update_execution(
        execution_id,
        status="cancelled",
        process_context=execution["process_context"],
        step_log=execution["step_log"],
        error="Cancelled by user",
    )
    return await get_execution(execution_id)


# ── Step Execution ────────────────────────────────────────────


async def _execute_steps(
    execution_id: str,
    steps: list[dict],
    process_context: dict,
    start_idx: int = 0,
    step_log: list | None = None,
    http_client: httpx.AsyncClient | None = None,
) -> dict:
    """Walk through steps sequentially, handling parallel groups."""
    step_log = step_log or []
    idx = start_idx

    try:
        while idx < len(steps):
            step = steps[idx]

            # Check for parallel group
            if step.get("parallel_group"):
                group_id = step["parallel_group"]
                group_steps = []
                group_start_idx = idx

                # Collect all steps in this parallel group
                while idx < len(steps) and steps[idx].get("parallel_group") == group_id:
                    group_steps.append((idx, steps[idx]))
                    idx += 1

                # Execute in parallel
                tasks = [
                    _execute_single_step(s, process_context, http_client)
                    for _, s in group_steps
                ]
                results = await asyncio.gather(*tasks, return_exceptions=True)

                for (sidx, s), result in zip(group_steps, results):
                    if isinstance(result, Exception):
                        entry = _log_entry(s, 0, "failed", str(result))
                        step_log.append(entry)
                        raise result
                    else:
                        duration_ms, outputs = result
                        process_context["steps"][s["id"]] = outputs
                        step_log.append(_log_entry(s, duration_ms, "completed"))

            else:
                # Gate step — pause
                if step["type"] == "gate":
                    gate_msg = step.get("gate_message", "Approval required")
                    # Resolve any references in gate message inputs
                    gate_ctx = {}
                    if step.get("inputs"):
                        gate_ctx = resolve_references(step["inputs"], process_context)

                    step_log.append(_log_entry(step, 0, "paused"))

                    await _update_execution(
                        execution_id,
                        status="paused",
                        current_step_idx=idx,
                        process_context=process_context,
                        step_log=step_log,
                        gate_message=gate_msg,
                        gate_context=gate_ctx,
                    )
                    return await get_execution(execution_id)

                # Decision step — branch
                if step["type"] == "decision":
                    start_t = time.perf_counter()
                    condition = step.get("condition", "")
                    result = evaluate_condition(condition, process_context)
                    duration_ms = (time.perf_counter() - start_t) * 1000

                    process_context["steps"][step["id"]] = {"result": result}
                    step_log.append(_log_entry(step, duration_ms, "completed"))

                    branches = step.get("branches", {})
                    target = branches.get(str(result).lower())
                    if target:
                        # Jump to the target step
                        target_idx = _find_step_idx(steps, target)
                        if target_idx is not None:
                            idx = target_idx
                            continue
                    idx += 1
                    continue

                # Skill or agent step
                duration_ms, outputs = await _execute_single_step(
                    step, process_context, http_client,
                )
                process_context["steps"][step["id"]] = outputs
                step_log.append(_log_entry(step, duration_ms, "completed"))
                idx += 1

            # Persist progress after each step/group
            await _update_execution(
                execution_id,
                status="running",
                current_step_idx=idx,
                process_context=process_context,
                step_log=step_log,
            )

        # All steps completed
        await _update_execution(
            execution_id,
            status="completed",
            current_step_idx=idx,
            process_context=process_context,
            step_log=step_log,
        )
        # Update telemetry on the definition
        await _update_definition_telemetry(
            steps[0]["id"] if steps else "",
            execution_id,
        )
        return await get_execution(execution_id)

    except Exception as e:
        logger.error("process_execution_failed", extra={
            "execution_id": execution_id,
            "error": str(e),
        })
        await _update_execution(
            execution_id,
            status="failed",
            current_step_idx=idx,
            process_context=process_context,
            step_log=step_log,
            error=str(e),
        )
        return await get_execution(execution_id)


async def _execute_single_step(
    step: dict,
    process_context: dict,
    http_client: httpx.AsyncClient | None = None,
) -> tuple[float, dict]:
    """Execute a single skill or agent step. Returns (duration_ms, outputs_dict)."""
    start_t = time.perf_counter()

    # Resolve inputs
    resolved_inputs = resolve_references(step.get("inputs", {}), process_context)

    if step["type"] == "skill":
        outputs = await _execute_skill_step(step, resolved_inputs)
    elif step["type"] == "agent":
        outputs = await _execute_agent_step(step, resolved_inputs, http_client)
    else:
        outputs = {}

    duration_ms = (time.perf_counter() - start_t) * 1000
    return duration_ms, outputs


async def _execute_skill_step(step: dict, resolved_inputs: dict) -> dict:
    """Execute a skill step. Stubbed for now — logs what would execute."""
    skill_id = step.get("skill_id", "unknown")
    output_keys = step.get("outputs", [])

    logger.info("skill_step_executed", extra={
        "step_id": step["id"],
        "skill_id": skill_id,
        "inputs": {k: str(v)[:100] for k, v in resolved_inputs.items()},
    })

    # Stub: return placeholder outputs
    outputs = {}
    for key in output_keys:
        outputs[key] = f"[stub:{skill_id}:{key}]"
    return outputs


async def _execute_agent_step(
    step: dict,
    resolved_inputs: dict,
    http_client: httpx.AsyncClient | None = None,
) -> dict:
    """Execute an agent step using the existing agent pipeline."""
    from uuid import uuid4 as uid4
    from app.agents.base import AgentInput
    from app.core.orchestrator import Orchestrator

    agent_name = step.get("agent", "research")
    output_keys = step.get("outputs", [])

    # Build a task description from all resolved inputs
    task_parts = []
    for k, v in resolved_inputs.items():
        if isinstance(v, str):
            task_parts.append(f"{k}: {v}")
        else:
            task_parts.append(f"{k}: {json.dumps(v, default=str)[:500]}")

    task_text = "\n".join(task_parts) if task_parts else step.get("name", "Process step")

    orchestrator = Orchestrator()
    agent = orchestrator._agents.get(agent_name)
    if not agent:
        raise ValueError(f"Agent '{agent_name}' not found")

    agent_input = AgentInput(
        request_id=uid4(),
        task=task_text,
        role_context=resolved_inputs.get("role_context", {}),
        retrieved_context=[],
    )

    # Build prompt and generate
    system_prompt, user_prompt = await agent.build_prompt(agent_input)
    from app.services.ollama_service import generate
    raw = await generate(
        prompt=user_prompt,
        system_prompt=system_prompt,
        http_client=http_client,
    )
    agent_output = agent.parse_response(raw, agent_input)

    # Map agent output to declared output keys
    outputs = {"result": agent_output.result, "reasoning": agent_output.reasoning}
    if len(output_keys) == 1:
        outputs[output_keys[0]] = agent_output.result
    elif len(output_keys) > 1:
        outputs[output_keys[0]] = agent_output.result
        for extra_key in output_keys[1:]:
            outputs[extra_key] = agent_output.metadata.get(extra_key, "")
    outputs["confidence"] = agent_output.confidence
    outputs["metadata"] = agent_output.metadata

    return outputs


def _find_step_idx(steps: list[dict], step_id: str) -> int | None:
    """Find the index of a step by its id."""
    for i, s in enumerate(steps):
        if s["id"] == step_id:
            return i
    return None


def _log_entry(step: dict, duration_ms: float, status: str, error: str = "") -> dict:
    """Build a step log entry."""
    return {
        "step_id": step["id"],
        "step_type": step["type"],
        "step_name": step.get("name", ""),
        "skill_id": step.get("skill_id"),
        "agent": step.get("agent"),
        "started_at": datetime.now(timezone.utc).isoformat(),
        "duration_ms": round(duration_ms, 2),
        "status": status,
        "error": error,
    }


# ── Database Operations ───────────────────────────────────────


async def create_process_definition(data: dict) -> dict:
    """Create a new process definition (upsert — updates if process_id exists)."""
    async with async_session() as session:
        result = await session.execute(
            text("""
                INSERT INTO process_definitions
                    (process_id, name, description, roles, trigger_config, steps, is_active)
                VALUES
                    (:pid, :name, :desc, CAST(:roles AS jsonb),
                     CAST(:trigger AS jsonb), CAST(:steps AS jsonb), TRUE)
                ON CONFLICT (process_id) DO UPDATE SET
                    name = EXCLUDED.name,
                    description = EXCLUDED.description,
                    roles = EXCLUDED.roles,
                    trigger_config = EXCLUDED.trigger_config,
                    steps = EXCLUDED.steps,
                    is_active = TRUE,
                    updated_at = NOW()
                RETURNING id, process_id, name, description, roles, trigger_config,
                          steps, is_active, execution_count, success_count,
                          avg_duration_ms, created_at, updated_at
            """),
            {
                "pid": data["process_id"],
                "name": data["name"],
                "desc": data.get("description", ""),
                "roles": json.dumps(data.get("roles", [])),
                "trigger": json.dumps(data.get("trigger_config", {})),
                "steps": json.dumps([s if isinstance(s, dict) else s.model_dump() for s in data.get("steps", [])]),
            },
        )
        await session.commit()
        row = result.mappings().one()
        return dict(row)


async def get_process_definition(process_id: str) -> dict | None:
    """Get a process definition by process_id."""
    async with async_session() as session:
        result = await session.execute(
            text("""
                SELECT id, process_id, name, description, roles, trigger_config,
                       steps, is_active, execution_count, success_count,
                       avg_duration_ms, created_at, updated_at
                FROM process_definitions
                WHERE process_id = :pid
            """),
            {"pid": process_id},
        )
        row = result.mappings().first()
        return dict(row) if row else None


async def list_process_definitions(
    active_only: bool = True,
    role: str | None = None,
) -> list[dict]:
    """List process definitions, optionally filtered."""
    clauses = []
    params: dict = {}
    if active_only:
        clauses.append("is_active = TRUE")
    if role:
        clauses.append("roles @> CAST(:role_filter AS jsonb)")
        params["role_filter"] = json.dumps([role])

    where = "WHERE " + " AND ".join(clauses) if clauses else ""
    async with async_session() as session:
        result = await session.execute(
            text(f"""
                SELECT id, process_id, name, description, roles, trigger_config,
                       steps, is_active, execution_count, success_count,
                       avg_duration_ms, created_at, updated_at
                FROM process_definitions
                {where}
                ORDER BY created_at DESC
            """),
            params,
        )
        return [dict(r) for r in result.mappings().all()]


async def update_process_definition(process_id: str, updates: dict) -> dict | None:
    """Update a process definition. Only updates provided fields."""
    set_clauses = ["updated_at = NOW()"]
    params: dict = {"pid": process_id}

    field_map = {
        "name": ("name", str),
        "description": ("desc", str),
        "is_active": ("active", bool),
    }
    for field, (param, _) in field_map.items():
        if field in updates and updates[field] is not None:
            set_clauses.append(f"{field} = :{param}")
            params[param] = updates[field]

    json_fields = {"roles": "roles", "trigger_config": "trigger", "steps": "steps_data"}
    for field, param in json_fields.items():
        if field in updates and updates[field] is not None:
            val = updates[field]
            if field == "steps":
                val = [s if isinstance(s, dict) else s.model_dump() for s in val]
            set_clauses.append(f"{field} = CAST(:{param} AS jsonb)")
            params[param] = json.dumps(val)

    async with async_session() as session:
        result = await session.execute(
            text(f"""
                UPDATE process_definitions
                SET {', '.join(set_clauses)}
                WHERE process_id = :pid
                RETURNING id, process_id, name, description, roles, trigger_config,
                          steps, is_active, execution_count, success_count,
                          avg_duration_ms, created_at, updated_at
            """),
            params,
        )
        await session.commit()
        row = result.mappings().first()
        return dict(row) if row else None


async def soft_delete_process_definition(process_id: str) -> dict | None:
    """Soft-delete a process definition (set is_active=False)."""
    return await update_process_definition(process_id, {"is_active": False})


async def get_execution(execution_id: str) -> dict | None:
    """Get a process execution by execution_id."""
    async with async_session() as session:
        result = await session.execute(
            text("""
                SELECT id, execution_id, process_id, status, current_step_idx,
                       process_context, trigger_params, role, step_log,
                       gate_message, gate_context, started_at, completed_at,
                       error, created_at
                FROM process_executions
                WHERE execution_id = :eid
            """),
            {"eid": execution_id},
        )
        row = result.mappings().first()
        return dict(row) if row else None


async def list_executions(
    process_id: str | None = None,
    status: str | None = None,
    limit: int = 20,
) -> list[dict]:
    """List process executions with optional filters."""
    clauses = []
    params: dict = {"lim": limit}
    if process_id:
        clauses.append("process_id = :pid")
        params["pid"] = process_id
    if status:
        clauses.append("status = :status")
        params["status"] = status

    where = "WHERE " + " AND ".join(clauses) if clauses else ""
    async with async_session() as session:
        result = await session.execute(
            text(f"""
                SELECT id, execution_id, process_id, status, current_step_idx,
                       role, started_at, completed_at, error, created_at
                FROM process_executions
                {where}
                ORDER BY created_at DESC
                LIMIT :lim
            """),
            params,
        )
        return [dict(r) for r in result.mappings().all()]


async def _update_execution(
    execution_id: str,
    status: str,
    process_context: dict | None = None,
    step_log: list | None = None,
    current_step_idx: int | None = None,
    error: str = "",
    gate_message: str = "",
    gate_context: dict | None = None,
) -> None:
    """Update execution state in the database."""
    set_clauses = ["status = :status"]
    params: dict = {"eid": execution_id, "status": status}

    if process_context is not None:
        set_clauses.append("process_context = CAST(:ctx AS jsonb)")
        params["ctx"] = json.dumps(process_context, default=str)
    if step_log is not None:
        set_clauses.append("step_log = CAST(:log AS jsonb)")
        params["log"] = json.dumps(step_log, default=str)
    if current_step_idx is not None:
        set_clauses.append("current_step_idx = :idx")
        params["idx"] = current_step_idx
    if error:
        set_clauses.append("error = :err")
        params["err"] = error
    if gate_message:
        set_clauses.append("gate_message = :gmsg")
        params["gmsg"] = gate_message
    if gate_context is not None:
        set_clauses.append("gate_context = CAST(:gctx AS jsonb)")
        params["gctx"] = json.dumps(gate_context, default=str)
    if status in ("completed", "failed", "cancelled"):
        set_clauses.append("completed_at = NOW()")

    async with async_session() as session:
        await session.execute(
            text(f"""
                UPDATE process_executions
                SET {', '.join(set_clauses)}
                WHERE execution_id = :eid
            """),
            params,
        )
        await session.commit()


async def _update_definition_telemetry(first_step_id: str, execution_id: str) -> None:
    """Update execution_count, success_count, avg_duration_ms on the definition."""
    execution = await get_execution(execution_id)
    if not execution or execution["status"] != "completed":
        return

    process_id = execution["process_id"]
    started = execution["started_at"]
    completed = execution["completed_at"]
    if started and completed:
        duration_ms = (completed - started).total_seconds() * 1000
    else:
        duration_ms = 0

    async with async_session() as session:
        await session.execute(
            text("""
                UPDATE process_definitions
                SET execution_count = execution_count + 1,
                    success_count = success_count + 1,
                    avg_duration_ms = (avg_duration_ms * success_count + :dur) / (success_count + 1),
                    updated_at = NOW()
                WHERE process_id = :pid
            """),
            {"pid": process_id, "dur": duration_ms},
        )
        await session.commit()


# ── Seed Data ─────────────────────────────────────────────────


DAILY_BRIEF_DEFINITION = {
    "process_id": "daily_brief",
    "name": "Daily Intelligence Brief",
    "description": "Gathers weather, calendar, news, and email, then assembles and sends a morning brief",
    "roles": ["cybersecurity_executive", "ai_governance_practitioner"],
    "trigger_config": {"type": "scheduled", "cron": "30 5 * * *"},
    "steps": [
        {"id": "fetch_weather", "type": "skill", "name": "Get weather",
         "skill_id": "weather_lookup",
         "inputs": {"latitude": 38.8462, "longitude": -77.3064, "days": 3},
         "outputs": ["weather_data"], "parallel_group": "gather"},
        {"id": "fetch_calendar", "type": "skill", "name": "Get today's calendar",
         "skill_id": "calendar_read",
         "inputs": {"range": "today"},
         "outputs": ["calendar_events"], "parallel_group": "gather"},
        {"id": "fetch_news", "type": "skill", "name": "Fetch articles",
         "skill_id": "rss_fetch",
         "inputs": {"sources": ["https://feeds.feedburner.com/TheHackersNews"], "hours_back": 24},
         "outputs": ["raw_articles"], "parallel_group": "gather"},
        {"id": "fetch_email", "type": "skill", "name": "Scan inbox",
         "skill_id": "email_read",
         "inputs": {"filter": "unread", "hours_back": 24},
         "outputs": ["email_summaries"], "parallel_group": "gather"},
        {"id": "analyze", "type": "agent", "name": "Score articles and extract actions",
         "agent": "analysis",
         "inputs": {
             "articles": "steps.fetch_news.raw_articles",
             "emails": "steps.fetch_email.email_summaries",
             "role_goals": "context.role.name",
         },
         "outputs": ["scored_articles", "scheduling_recommendations"]},
        {"id": "assemble", "type": "skill", "name": "Render email",
         "skill_id": "template_render",
         "inputs": {
             "template_name": "daily_brief_email",
             "weather": "steps.fetch_weather.weather_data",
             "calendar": "steps.fetch_calendar.calendar_events",
             "articles": "steps.analyze.scored_articles",
             "recommendations": "steps.analyze.scheduling_recommendations",
         },
         "outputs": ["rendered_email"]},
        {"id": "send", "type": "skill", "name": "Send brief",
         "skill_id": "email_send",
         "inputs": {"to": ["mclaurint@gmail.com"], "subject": "Daily Brief",
                     "body": "steps.assemble.rendered_email"},
         "outputs": ["send_confirmation"]},
    ],
}


async def seed_process_definitions() -> None:
    """Seed built-in process definitions if they don't exist."""
    existing = await get_process_definition("daily_brief")
    if not existing:
        await create_process_definition(DAILY_BRIEF_DEFINITION)
        logger.info("seeded_process_definition", extra={"process_id": "daily_brief"})
