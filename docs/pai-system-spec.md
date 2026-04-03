# Personal AI Infrastructure (PAI) — System Specification

# 1. Mission

This system is a **containerized, local-first Personal AI Orchestration Platform** that:

* Understands the user across multiple real-world roles:

  * Professional
  * Father
  * Partner
  * Friend
* Adapts outputs based on role, goals, and context
* Uses an orchestrator to coordinate:

  * agents
  * models
  * tools (skills)
  * memory
* Employs **multi-agent reasoning and evaluation**
* Continuously improves through **controlled learning loops**

---

# 2. Core Design Principles

## 2.1 Orchestrator-Centric Control

* The orchestrator is the **single source of decision authority**
* No other component may:

  * call models directly
  * execute tools independently
  * modify system behavior autonomously

---

## 2.2 Role-Aware Intelligence

All outputs must be influenced by:

* active role (explicit or inferred)
* role-specific:

  * goals
  * preferences
  * constraints

---

## 2.3 Separation of Concerns

| Component    | Responsibility                    |
| ------------ | --------------------------------- |
| Orchestrator | Decision-making, workflow control |
| Agents       | Reasoning units                   |
| Skills       | Deterministic execution           |
| Memory       | Storage + retrieval               |
| Evaluation   | Scoring + selection               |
| Models       | Text generation only              |

---

## 2.4 Deterministic + Agentic Hybrid

* Deterministic workflows where reliability is required
* Agentic reasoning where flexibility adds value
* All agentic behavior is governed and observable

---

## 2.5 Container-First Architecture

* All components must run in containers
* No hidden local dependencies
* Services communicate via APIs

---

## 2.6 Structured Outputs

* All system outputs must be:

  * structured (JSON preferred)
  * validated against schemas
* No free-form outputs in system workflows

---

# 3. System Architecture

## 3.1 High-Level Flow

```text
User Request
   ↓
Orchestrator
   ↓
(Role Detection + Intent Classification)
   ↓
Workflow Planning
   ↓
Agent Execution (optional parallel)
   ↓
Evaluation & Adjudication (if multi-agent)
   ↓
Synthesis (optional)
   ↓
Response
   ↓
Memory + Learning System
```

---

## 3.2 Core Components

### Orchestrator (Control Plane)

Responsibilities:

* request intake
* role detection
* intent classification
* workflow selection
* agent orchestration
* model routing
* memory interaction
* evaluation coordination

---

### Agent Layer

Agents are **specialized reasoning units**, not autonomous systems.

Initial agent types:

* Research Agent
* Analysis Agent
* Planning Agent
* Critic / Red Team Agent
* Synthesizer Agent

Rules:

* Agents do NOT call tools directly
* Agents do NOT access models directly
* Agents operate only via orchestrator

---

### Skill System

Skills are **deterministic execution units**

Examples:

* web retrieval
* parsing
* summarization pipeline
* scheduling
* file operations

Rules:

* Skills are invoked ONLY by orchestrator
* Skills must be stateless
* Skills must return structured outputs

---

### Memory System

#### Types

1. Episodic Memory

   * interactions
   * tasks
   * outputs

2. Semantic Memory

   * vector embeddings
   * knowledge retrieval

3. Procedural Memory

   * workflows
   * successful patterns

4. Identity Memory

   * user roles
   * preferences
   * behavior patterns

---

### Evaluation System

Used for:

* scoring outputs
* selecting best result
* feeding learning loop

Capabilities:

* multi-agent comparison
* structured scoring
* adjudication logic

---

### Model Layer

* Runs via Ollama
* Supports multiple models
* Models are selected by orchestrator based on task

---

# 4. Role & Identity Engine

## 4.1 Purpose

To dynamically adapt system behavior based on user role.

---

## 4.2 Role Model

```json
{
  "role": "professional",
  "goals": ["efficiency", "innovation"],
  "preferences": ["technical depth", "structured"],
  "constraints": ["accuracy", "compliance"]
}
```

---

## 4.3 Behavior Influence

Role impacts:

* tone
* depth
* recommendations
* prioritization
* risk tolerance

---

# 5. Workflow Types

## 5.1 Direct Response

* simple request
* single model call

---

## 5.2 Retrieval-Augmented

* requires memory lookup
* combines retrieval + generation

---

## 5.3 Multi-Agent Competition

* multiple agents solve same task
* outputs evaluated
* best selected or synthesized

---

## 5.4 Execution Workflow

* involves tools/skills
* deterministic steps

---

## 5.5 Background Workflow

* ingestion
* learning
* scheduled tasks

---

# 6. Multi-Agent Competition System

## 6.1 Flow

```text
Task
 ↓
Spawn Agents (parallel)
 ↓
Collect Outputs
 ↓
Evaluation Engine
 ↓
Adjudication Engine
 ↓
(Optional) Synthesis
 ↓
Final Output
```

---

## 6.2 Evaluation Criteria

* accuracy
* relevance
* depth
* clarity
* actionability
* consistency

---

## 6.3 Adjudication Rules

* best score wins OR
* weighted scoring OR
* synthesis of top outputs

---

# 7. Learning System

## 7.1 Purpose

Continuously improve system performance through:

* evaluation
* experimentation
* validation
* promotion

---

## 7.2 Learning Loop

```text
Execution
 ↓
Capture Data
 ↓
Evaluate
 ↓
Generate Improvements
 ↓
Test Improvements
 ↓
Promote or Reject
```

---

## 7.3 Constraints

* no direct self-modification
* all changes must be validated
* rollback capability required

---

# 8. Container Architecture

## 8.1 Core Services

* orchestrator
* ollama
* postgres (pgvector)
* redis
* optional:

  * ingestion service
  * evaluation service
  * scheduler

---

## 8.2 Communication

* REST APIs
* internal service networking
* no direct cross-layer coupling

---

# 9. Project Structure

```text
/pai
  /orchestrator
    /app
      /api
      /core
      /services
      /models
      /agents
      /evaluation
      /memory
      main.py
    requirements.txt
    Dockerfile

  /workers
    /ingestion
    /evaluation
    /learning

  /infra
    docker-compose.yml
    .env

  /docs
    pai-system-spec.md
    orchestrator-design.md

  /scripts
    init_db.sql
```

---

# 10. MVP Scope (Phase 1)

Included:

* orchestrator (FastAPI)
* single agent
* ollama integration
* role-aware prompt shaping
* structured output

Excluded:

* multi-agent competition
* learning system
* advanced memory
* hooks

---

# 11. Non-Negotiable Rules

* Orchestrator is the ONLY decision-maker
* No direct model calls outside orchestrator
* No tool execution outside orchestrator
* All outputs must be structured
* All workflows must be observable and logged
* All improvements must be validated before promotion

---

# 12. Development Guidelines

* modular architecture
* strongly typed models (Pydantic)
* configuration-driven design
* no hardcoded logic
* clear separation between layers

---

# 13. First Capability to Build

## Objective

Implement a basic request pipeline:

```text
Input → Orchestrator → Model → Output
```

## Requirements

* POST /task endpoint
* role-aware processing
* model selection
* structured JSON response

---

# 14. Future Expansion

* multi-agent orchestration
* evaluation + adjudication engine
* continuous learning system
* proactive hooks
* UI layer
* distributed execution

---

# 15. Definition of Success

The system is successful when it:

* adapts responses based on user role
* produces consistent, structured outputs
* can orchestrate multi-step workflows
* improves performance over time
* remains fully observable and controllable

---
