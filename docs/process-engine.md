# Process Engine Layer

## The Problem

The orchestrator currently handles single-turn interactions: request in, response out. But the most valuable things I want this system to do are multi-step workflows — a daily intelligence brief that gathers weather, calendar, news, and email before assembling an output; an RFP response pipeline that parses a document, extracts requirements, maps capabilities, and drafts sections; a meeting prep workflow that researches attendees and assembles a brief. These are sequences of steps where each step's output feeds the next, some steps run in parallel, and some require my approval before continuing.

I need a **process engine** — a new layer between the orchestrator and the agents/skills that executes multi-step workflows defined as configuration, not code.

## The Core Concept

A **process** is a sequence of typed steps that share an accumulating context object.

A **process definition** is stored as data (in the database, as JSON configuration). Creating a new workflow means inserting a new process definition row — no code changes needed. This is critical for the product roadmap: when this ships to enterprise customers, their admins define processes for their teams through configuration, not development.

A **process execution** is a running or completed instance of a process. It tracks: which step we're on, the accumulated context (all outputs from all completed steps), timing, errors, and a step-by-step log of what happened.

## The Four Step Types

Every step in a process is one of these types:

**Skill step** — Invokes a skill from the skill registry. Deterministic, no LLM. The engine looks up the skill by skill_id, resolves input parameters from the process context, executes the skill, and writes outputs back to the process context. For this sprint, actual skill execution should be stubbed (log what would execute, return placeholder output). Real implementations come later, one skill at a time.

**Agent step** — Uses one of the existing agents (research, analysis, planning, critic, synthesizer) to reason about data from previous steps. The engine builds an AgentInput from the resolved inputs, runs the agent through the existing model pipeline, and writes the AgentOutput into process context. This is the only step type that touches the LLM.

**Decision step** — A branching point. Evaluates a condition against the process context and routes execution to different subsequent steps based on the result. Some decisions are simple value checks; others might need a lightweight classification. Do NOT use Python's eval() for condition evaluation — implement a safe expression evaluator.

**Gate step** — Pauses the process and waits for human input. The engine sets the execution status to paused, records what to show the human (a message and relevant context data), and stops. A separate API call to resume the execution provides the human's response (approve, reject, modify), and the engine continues from where it left off.

## Context Accumulation and Reference Resolution

This is the connective tissue of the whole system. Each step declares its inputs as either literal values or **references** to outputs from previous steps.

References use dot-notation paths:
- `steps.fetch_weather.weather_data` → resolves to the weather_data output from the fetch_weather step
- `context.role.goals` → resolves to the active role's goals from the identity system
- `trigger.company_name` → resolves to a parameter from the original trigger input

The engine must resolve all references in a step's inputs before executing that step. If a reference points to a step that hasn't run yet, that's an error. The reference resolver should handle nested structures (dicts and lists containing references).

## Parallel Execution

Steps can declare a `parallel_group` identifier. All steps sharing the same parallel_group value execute concurrently (follow the existing asyncio.gather pattern used in multi-agent competition). The engine collects them, runs them in parallel, writes all outputs to context, then moves to the next sequential step.

Example: the daily brief's four data-gathering steps (weather, calendar, news, email) all have parallel_group "gather" and execute simultaneously. The analysis step that follows has no parallel_group and runs after all four complete.

## Process Lifecycle

```
Define → Start → Execute Steps → (Pause at Gates) → Resume → Complete
                                                          ↓
                                                    Fail / Cancel
```

1. **Define**: Process definition is created and stored (via API or seed data)
2. **Start**: A start request creates an execution instance, initializes context with trigger parameters and role context, begins stepping through the definition
3. **Execute**: The engine walks steps in order, dispatching to skills/agents, accumulating context
4. **Pause**: If a gate step is reached, execution pauses. State is persisted so it survives restarts
5. **Resume**: When the human responds to a gate, execution picks up from the next step
6. **Complete/Fail**: Terminal states with full timing and step log recorded

## What a Process Definition Looks Like

For reference, here's the daily brief as a process definition. This is the shape of the JSON that gets stored:

```json
{
  "process_id": "daily_brief",
  "name": "Daily Intelligence Brief",
  "description": "Gathers weather, calendar, news, and email, then assembles and sends a morning brief",
  "roles": ["cybersecurity_executive", "ai_governance_practitioner"],
  "trigger": {"type": "scheduled", "cron": "30 5 * * *"},
  "steps": [
    {"id": "fetch_weather", "type": "skill", "name": "Get weather", "skill_id": "weather_lookup", "inputs": {"latitude": 38.8462, "longitude": -77.3064, "days": 3}, "outputs": ["weather_data"], "parallel_group": "gather"},
    {"id": "fetch_calendar", "type": "skill", "name": "Get today's calendar", "skill_id": "calendar_read", "inputs": {"range": "today"}, "outputs": ["calendar_events"], "parallel_group": "gather"},
    {"id": "fetch_news", "type": "skill", "name": "Fetch articles", "skill_id": "rss_fetch", "inputs": {"sources": ["https://feeds.feedburner.com/TheHackersNews"], "hours_back": 24}, "outputs": ["raw_articles"], "parallel_group": "gather"},
    {"id": "fetch_email", "type": "skill", "name": "Scan inbox", "skill_id": "email_read", "inputs": {"filter": "unread", "hours_back": 24}, "outputs": ["email_summaries"], "parallel_group": "gather"},
    {"id": "analyze", "type": "agent", "name": "Score articles and extract actions", "agent": "analysis", "inputs": {"articles": "steps.fetch_news.raw_articles", "emails": "steps.fetch_email.email_summaries", "role_goals": "context.role.goals"}, "outputs": ["scored_articles", "scheduling_recommendations"]},
    {"id": "assemble", "type": "skill", "name": "Render email", "skill_id": "template_render", "inputs": {"template_name": "daily_brief_email", "context": {"weather": "steps.fetch_weather.weather_data", "calendar": "steps.fetch_calendar.calendar_events", "articles": "steps.analyze.scored_articles", "recommendations": "steps.analyze.scheduling_recommendations"}}, "outputs": ["rendered_email"]},
    {"id": "send", "type": "skill", "name": "Send brief", "skill_id": "email_send", "inputs": {"to": ["tim@example.com"], "subject": "Daily Brief", "body": "steps.assemble.rendered_email"}, "outputs": ["send_confirmation"]}
  ]
}
```

## API Surface

The process engine needs endpoints for:

**Process definitions** — CRUD operations. List (filterable by role/domain), get by ID, create, update, soft-delete. Same patterns as the existing /roles and /skills endpoints.

**Process execution** — Start a process (provide process_id, optional role, and input parameters). Get execution status (full state including current step, context, step log). Respond to a gate (provide the human's decision). Cancel a running execution. List executions with filters (by process_id, status).

## Observability

Every step execution must be logged in the execution's step_log array with: step_id, step_type, skill_id or agent name (if applicable), start time, end time, duration, status, and error (if any). This step_log is the data source for the future Gen 3 learning engine that will analyze execution patterns to optimize processes.

After a process completes successfully, update the process_definition's telemetry counters (execution_count, success_count, avg_duration_ms).

## Testing

Write integration tests following the same pattern as the existing test_integration.py (httpx client against the running stack). Cover:

- CRUD for process definitions
- Starting a process and verifying it runs through all steps
- Parallel step execution (same parallel_group)
- Gate steps pausing execution
- Resuming after gate approval and rejection
- Context accumulation (verify step outputs appear in process_context)
- Error handling (invalid skill_id, missing references)
- Step log completeness

Seed the daily_brief process definition so tests have real data to work with.

## Future State — Do Not Build, But Design For

These capabilities are coming in future sprints. The process engine's design should not block them:

**Process Compiler (Gen 2)**: An LLM-based system will read the skill registry and compose new process definitions from natural language goal descriptions. This means process definitions must be fully self-describing — the step structure, reference patterns, and parallel groups should be understandable to an LLM reading the JSON. No implicit behavior that isn't captured in the definition.

**Learning Engine (Gen 3)**: A system that analyzes execution telemetry across many process runs to identify bottlenecks, failing steps, and optimization opportunities. The step_log and telemetry counters are the inputs for this. Make sure they capture enough data.

**Process Chaining**: One process's completion may trigger another process. For example, the daily brief identifies an urgent article that triggers a deep-research process. Design the completion handler so emitting an event (e.g., to Redis pub/sub) can be added later without restructuring.

**Enterprise Multi-Tenancy**: Process definitions should not embed user-specific values (emails, API keys) as hardcoded literals. The reference resolution system should support resolving from user context, not just step outputs. The daily brief seed can use literals for now, but the architecture must support context-based resolution.

**Scheduling**: A scheduler service will trigger processes on cron schedules. The trigger config in process definitions already captures this. The actual scheduler is a future sprint — for now, processes are started via the API.

## Definition of Done

- Process definitions can be created, read, updated, and soft-deleted via API
- Starting a process creates an execution that walks through all defined steps
- Steps with matching parallel_group values execute concurrently
- Skill steps look up the skill registry and execute (stubbed for now)
- Agent steps use the existing agent pipeline to reason about accumulated context
- Gate steps pause execution; gate responses resume or cancel it
- Context references resolve correctly across steps
- Step log captures timing and status for every step executed
- All new tests pass; all existing tests still pass