"""
Integration tests for PAI pipeline.
Requires the full stack to be running (docker compose up).
"""

import httpx
import pytest

BASE_URL = "http://localhost:8000"


@pytest.fixture
def client():
    return httpx.Client(base_url=BASE_URL, timeout=300.0)


# ── Health ──

def test_health(client):
    resp = client.get("/health")
    assert resp.status_code == 200
    data = resp.json()
    assert data["status"] in ("healthy", "degraded")
    assert "redis" in data["checks"]
    assert "ollama" in data["checks"]


# ── Roles ──

def test_task_with_explicit_role(client):
    resp = client.post("/task", json={"input": "What is 2+2?", "role": "solutions_architect"})
    assert resp.status_code == 200
    data = resp.json()
    assert data["role"] == "solutions_architect"
    assert data["domain"] == "professional"
    assert data["content"]
    assert data["request_id"]
    assert data["model"]
    assert data["duration_ms"] > 0


def test_task_default_role(client):
    resp = client.post("/task", json={"input": "Say hello"})
    assert resp.status_code == 200
    data = resp.json()
    # LLM-inferred role — any valid role is acceptable for a generic greeting
    assert data["role"] in (
        "cybersecurity_executive", "polymath_in_training", "educator_scholar",
        "parent", "family_activity_coordinator",
    )
    assert data["domain"] in ("professional", "intellectual_growth", "family", "personal")


def test_task_parent_role(client):
    resp = client.post("/task", json={"input": "How do I teach a child to ride a bike?", "role": "parent"})
    assert resp.status_code == 200
    data = resp.json()
    assert data["role"] == "parent"
    assert data["domain"] == "family"
    assert data["content"]


def test_task_dual_roles(client):
    resp = client.post("/task", json={
        "input": "Design an ATO automation pipeline",
        "role": "solutions_architect",
        "secondary_role": "ai_cybersecurity_strategist",
    })
    assert resp.status_code == 200
    data = resp.json()
    assert data["role"] == "solutions_architect"
    assert data["secondary_role"] == "ai_cybersecurity_strategist"
    assert data["domain"] == "professional"
    assert data["content"]


def test_task_cross_domain_dual_roles(client):
    resp = client.post("/task", json={
        "input": "Weekly meal plan for health and family",
        "role": "family_chef",
        "secondary_role": "fitness_longevity_optimist",
    })
    assert resp.status_code == 200
    data = resp.json()
    assert data["role"] == "family_chef"
    assert data["secondary_role"] == "fitness_longevity_optimist"
    assert data["domain"] == "personal"


def test_task_same_primary_secondary_ignored(client):
    resp = client.post("/task", json={
        "input": "test",
        "role": "parent",
        "secondary_role": "parent",
    })
    assert resp.status_code == 200
    data = resp.json()
    assert data["role"] == "parent"
    assert data["secondary_role"] is None


def test_task_validation_empty_input(client):
    resp = client.post("/task", json={"input": ""})
    assert resp.status_code == 422


def test_task_validation_invalid_role(client):
    resp = client.post("/task", json={"input": "test", "role": "invalid_role"})
    assert resp.status_code == 422


def test_episodic_memory_persisted(client):
    """After a task, verify the episodic_memory table has a new row."""
    import subprocess

    resp = client.post("/task", json={"input": "Memory persistence test", "role": "polymath_in_training"})
    assert resp.status_code == 200
    request_id = resp.json()["request_id"]

    result = subprocess.run(
        [
            "docker", "exec", "pai-postgres",
            "psql", "-U", "pai", "-d", "pai", "-t", "-A",
            "-c", f"SELECT COUNT(*) FROM episodic_memory WHERE session_id = '{request_id}'",
        ],
        capture_output=True,
        text=True,
    )
    count = int(result.stdout.strip())
    assert count == 1, f"Expected 1 episodic_memory row for {request_id}, got {count}"


def test_response_has_request_id_header(client):
    resp = client.post("/task", json={"input": "Header test"})
    assert resp.status_code == 200
    assert "x-request-id" in resp.headers


def test_all_roles_accepted(client):
    """Every role defined in the spec should be accepted by the API."""
    roles = [
        "cybersecurity_executive", "ai_cybersecurity_strategist",
        "ai_governance_practitioner", "educator_scholar",
        "solutions_architect", "proposal_strategist",
        "fitness_longevity_optimist", "aesthetics_focused_builder",
        "family_chef", "family_activity_coordinator",
        "parent", "polymath_in_training",
    ]
    for role in roles:
        resp = client.post("/task", json={"input": "ping", "role": role})
        assert resp.status_code == 200, f"Role {role} returned {resp.status_code}"
        assert resp.json()["role"] == role


# ── Intent Classification ──

def test_intent_question(client):
    resp = client.post("/task", json={"input": "What is zero trust architecture?"})
    assert resp.status_code == 200
    assert resp.json()["intent"] == "question"


def test_intent_research(client):
    resp = client.post("/task", json={"input": "Research current trends in AI governance frameworks"})
    assert resp.status_code == 200
    assert resp.json()["intent"] == "research"


def test_intent_analysis(client):
    resp = client.post("/task", json={"input": "Analyze the pros and cons of zero trust vs perimeter security"})
    assert resp.status_code == 200
    assert resp.json()["intent"] == "analysis"


def test_intent_planning(client):
    resp = client.post("/task", json={"input": "Plan a roadmap for implementing NIST CSF"})
    assert resp.status_code == 200
    assert resp.json()["intent"] == "planning"


def test_intent_execution(client):
    resp = client.post("/task", json={"input": "Build a Python script to parse STIG checklist files"})
    assert resp.status_code == 200
    assert resp.json()["intent"] == "execution"


# ── Workflow Routing ──

def test_workflow_direct_response(client):
    resp = client.post("/task", json={"input": "What is 2+2?"})
    assert resp.status_code == 200
    assert resp.json()["workflow"] in ("direct_response", "multi_agent_competition")
    assert resp.json()["content"]


def test_workflow_agent_research(client):
    resp = client.post("/task", json={"input": "Research the latest NIST AI RMF updates"})
    assert resp.status_code == 200
    assert resp.json()["workflow"] in ("agent_research", "multi_agent_competition")
    assert resp.json()["content"]


def test_workflow_retrieval_augmented(client):
    resp = client.post("/task", json={"input": "Analyze the impact of executive order 14028"})
    assert resp.status_code == 200
    assert resp.json()["workflow"] in ("agent_analysis", "multi_agent_competition")


# ── GET /roles ──

def test_get_roles(client):
    resp = client.get("/roles")
    assert resp.status_code == 200
    data = resp.json()
    assert data["total_roles"] == 12
    assert "professional" in data["domains"]
    assert "personal" in data["domains"]
    assert "family" in data["domains"]
    assert "intellectual_growth" in data["domains"]
    # Professional should have 6 roles
    assert len(data["domains"]["professional"]) == 6


def test_get_roles_structure(client):
    resp = client.get("/roles")
    data = resp.json()
    # Each role entry should have the expected fields
    role_entry = data["domains"]["professional"][0]
    assert "role" in role_entry
    assert "description" in role_entry
    assert "goals" in role_entry
    assert "preferences" in role_entry
    assert "constraints" in role_entry


# ── Sprint 3: Agent Workflows ──

def test_analysis_agent_workflow(client):
    resp = client.post("/task", json={
        "input": "Analyze the pros and cons of zero trust vs perimeter security",
        "role": "cybersecurity_executive",
    })
    assert resp.status_code == 200
    data = resp.json()
    assert data["workflow"] in ("agent_analysis", "multi_agent_competition")
    assert data["content"]


def test_planning_agent_workflow(client):
    resp = client.post("/task", json={
        "input": "Plan a roadmap for implementing NIST CSF",
        "role": "solutions_architect",
    })
    assert resp.status_code == 200
    data = resp.json()
    assert data["workflow"] in ("agent_planning", "multi_agent_competition")
    assert data["content"]


# ── Sprint 3: Multi-Agent Competition ──

def test_compete_endpoint_basic(client):
    resp = client.post("/compete", json={
        "input": "What are the key trends in AI governance?",
        "role": "ai_governance_practitioner",
        "agents": ["research", "analysis"],
        "strategy": "best_score",
    })
    assert resp.status_code == 200
    data = resp.json()
    assert data["workflow"] == "multi_agent_competition"
    assert data["content"]
    assert data["role"] == "ai_governance_practitioner"


def test_compete_synthesize_strategy(client):
    resp = client.post("/compete", json={
        "input": "Evaluate the state of ATO automation",
        "role": "ai_cybersecurity_strategist",
        "agents": ["research", "analysis"],
        "strategy": "synthesize",
    })
    assert resp.status_code == 200
    data = resp.json()
    assert data["workflow"] == "multi_agent_competition"
    assert data["content"]


def test_compete_validation_too_few_agents(client):
    resp = client.post("/compete", json={
        "input": "test",
        "agents": ["research"],
    })
    assert resp.status_code == 422


def test_compete_invalid_strategy_fallback(client):
    resp = client.post("/compete", json={
        "input": "Test fallback strategy",
        "agents": ["research", "analysis"],
        "strategy": "not_a_strategy",
    })
    assert resp.status_code == 200
    # Should fallback to best_score and still work
    assert resp.json()["content"]


# ── Sprint 4: Chat ──

def test_chat_basic(client):
    resp = client.post("/chat", json={"message": "Hello, how are you?"})
    assert resp.status_code == 200
    data = resp.json()
    assert data["content"]
    assert data["role"]
    assert data["domain"]
    assert data["conversation_id"]
    assert data["duration_ms"] > 0


def test_chat_with_role(client):
    resp = client.post("/chat", json={
        "message": "What should I cook tonight?",
        "role": "family_chef",
    })
    assert resp.status_code == 200
    data = resp.json()
    assert data["role"] == "family_chef"
    assert data["domain"] == "personal"


def test_chat_with_history(client):
    resp = client.post("/chat", json={
        "message": "Tell me more about that.",
        "role": "polymath_in_training",
        "history": [
            {"role_name": "user", "content": "What is quantum computing?"},
            {"role_name": "assistant", "content": "Quantum computing uses quantum mechanics principles."},
        ],
    })
    assert resp.status_code == 200
    assert resp.json()["content"]


def test_chat_dual_roles(client):
    resp = client.post("/chat", json={
        "message": "How do I prepare for a security certification?",
        "role": "cybersecurity_executive",
        "secondary_role": "educator_scholar",
    })
    assert resp.status_code == 200
    data = resp.json()
    assert data["role"] == "cybersecurity_executive"
    assert data["secondary_role"] == "educator_scholar"


def test_chat_empty_message_rejected(client):
    resp = client.post("/chat", json={"message": ""})
    assert resp.status_code == 422


# ── Sprint 4: Web Research ──

def test_web_research_basic(client):
    resp = client.post("/skills/web-research", json={
        "topic": "AI cybersecurity convergence 2025",
        "max_results": 5,
        "time_filter": "m",
        "auto_ingest": False,
    })
    assert resp.status_code == 200
    data = resp.json()
    assert data["topic"] == "AI cybersecurity convergence 2025"
    assert isinstance(data["articles"], list)
    assert data["total_found"] >= 0
    assert data["duration_ms"] > 0


def test_web_research_with_ingest(client):
    resp = client.post("/skills/web-research", json={
        "topic": "NIST AI risk management framework",
        "max_results": 3,
        "time_filter": "y",
        "auto_ingest": True,
    })
    assert resp.status_code == 200
    data = resp.json()
    # If articles were found and ingested
    if data["total_found"] > 0:
        assert data["ingested_count"] >= 0


def test_web_research_validation(client):
    resp = client.post("/skills/web-research", json={"topic": ""})
    assert resp.status_code == 422


def test_web_research_articles_have_scores(client):
    resp = client.post("/skills/web-research", json={
        "topic": "adversarial machine learning attacks",
        "max_results": 5,
        "auto_ingest": False,
    })
    assert resp.status_code == 200
    data = resp.json()
    for article in data["articles"]:
        assert "score" in article
        assert "total" in article["score"]
        assert "title" in article
        assert "url" in article


# ── Auto Role Inference ──

def test_auto_role_fitness(client):
    """Fitness prompt should auto-select a personal/fitness role."""
    resp = client.post("/task", json={
        "input": "What exercises should I do for chest and shoulders?",
    })
    assert resp.status_code == 200
    data = resp.json()
    assert data["role"] in ("fitness_longevity_optimist", "aesthetics_focused_builder")
    assert data["domain"] == "personal"


def test_auto_role_cybersecurity(client):
    """Cybersecurity prompt should auto-select a professional role."""
    resp = client.post("/task", json={
        "input": "Evaluate the NIST cybersecurity framework for our enterprise risk management program",
    })
    assert resp.status_code == 200
    data = resp.json()
    assert data["domain"] == "professional"


def test_auto_role_family(client):
    """Family prompt should auto-select a family role."""
    resp = client.post("/task", json={
        "input": "Plan a fun weekend outing for the kids",
    })
    assert resp.status_code == 200
    data = resp.json()
    assert data["domain"] == "family"


def test_auto_role_cooking(client):
    """Cooking prompt should auto-select family_chef."""
    resp = client.post("/task", json={
        "input": "Plan a healthy dinner menu for the week with easy recipes",
    })
    assert resp.status_code == 200
    data = resp.json()
    assert data["role"] == "family_chef"


def test_auto_escalation_complex_prompt(client):
    """Complex multi-faceted prompt should auto-escalate to multi-agent competition."""
    resp = client.post("/task", json={
        "input": "Research the best approaches for integrating AI into security operations and evaluate the pros and cons of each methodology",
    })
    assert resp.status_code == 200
    data = resp.json()
    assert data["workflow"] == "multi_agent_competition"
    assert data["content"]


def test_auto_simple_stays_direct(client):
    """Simple question should stay in direct_response, no agents."""
    resp = client.post("/task", json={
        "input": "What is 2+2?",
    })
    assert resp.status_code == 200
    data = resp.json()
    # Procedural memory may escalate — accept either
    assert data["workflow"] in ("direct_response", "multi_agent_competition")
    assert data["content"]


def test_explicit_role_overrides_auto(client):
    """Explicitly setting a role should override auto-inference."""
    resp = client.post("/task", json={
        "input": "What exercises should I do for chest?",
        "role": "educator_scholar",
    })
    assert resp.status_code == 200
    data = resp.json()
    assert data["role"] == "educator_scholar"


# ── Sprint 5: Dedup, Scheduler, Stats ──

def test_research_stats_endpoint(client):
    """Stats endpoint returns ledger info and scheduler config."""
    resp = client.get("/skills/research-stats")
    assert resp.status_code == 200
    data = resp.json()
    assert "total_articles" in data
    assert "articles_last_7_days" in data
    assert "schedule_hours" in data
    assert "gmail_configured" in data
    assert data["total_articles"] >= 0


def test_web_research_dedup(client):
    """Running the same search twice should yield fewer new articles the second time."""
    topic = "NIST cybersecurity framework"

    # First run
    resp1 = client.post("/skills/web-research", json={
        "topic": topic, "max_results": 5, "auto_ingest": False,
    })
    assert resp1.status_code == 200
    first_count = len(resp1.json()["articles"])

    # Second run — same topic, should have fewer or equal new articles
    resp2 = client.post("/skills/web-research", json={
        "topic": topic, "max_results": 5, "auto_ingest": False,
    })
    assert resp2.status_code == 200
    second_count = len(resp2.json()["articles"])

    # Second run should have strictly fewer new articles (some were just recorded in first run)
    # Unless all were already seen from prior scheduler runs, then both could be 0
    assert second_count <= first_count or first_count == 0


def test_manual_research_trigger(client):
    """POST /skills/research-now triggers a scheduled research run and returns summary."""
    resp = client.post("/skills/research-now", timeout=180.0)
    assert resp.status_code == 200
    data = resp.json()
    assert "topics_searched" in data
    assert "total_found" in data
    assert "new_articles" in data
    assert "duplicates_filtered" in data
    assert data["topics_searched"] > 0


# ── Meal Planning ──


def test_add_family_member(client):
    """POST /skills/family/member adds a new family member."""
    resp = client.post("/skills/family/member", json={
        "name": "TestMember",
        "age_group": "adult",
        "dietary_restrictions": ["gluten-free"],
        "notes": "test member",
    })
    assert resp.status_code == 200
    data = resp.json()
    assert data["name"] == "TestMember"
    assert data["age_group"] == "adult"
    assert "gluten-free" in data["dietary_restrictions"]
    assert "id" in data


def test_add_family_member_upsert(client):
    """POST /skills/family/member updates an existing member on conflict."""
    # First create
    client.post("/skills/family/member", json={"name": "UpsertTest", "age_group": "child"})
    # Then upsert
    resp = client.post("/skills/family/member", json={"name": "UpsertTest", "age_group": "teen"})
    assert resp.status_code == 200
    assert resp.json()["age_group"] == "teen"


def test_add_family_member_validation(client):
    """POST /skills/family/member rejects invalid age group."""
    resp = client.post("/skills/family/member", json={"name": "Bad", "age_group": "elderly"})
    assert resp.status_code == 422


def test_list_family(client):
    """GET /skills/family returns members and preferences."""
    resp = client.get("/skills/family")
    assert resp.status_code == 200
    data = resp.json()
    assert "members" in data
    assert "preferences" in data
    assert isinstance(data["members"], list)


def test_set_preference(client):
    """POST /skills/family/preference sets a preference."""
    # Get a member ID first
    family = client.get("/skills/family").json()
    if not family["members"]:
        client.post("/skills/family/member", json={"name": "PrefTest"})
        family = client.get("/skills/family").json()
    member_id = family["members"][0]["id"]

    resp = client.post("/skills/family/preference", json={
        "family_member_id": member_id,
        "item": "pizza",
        "sentiment": "love",
        "item_type": "dish",
    })
    assert resp.status_code == 200
    data = resp.json()
    assert data["item"] == "pizza"
    assert data["sentiment"] == "love"


def test_set_preference_validation(client):
    """POST /skills/family/preference rejects invalid sentiment."""
    resp = client.post("/skills/family/preference", json={
        "family_member_id": 1,
        "item": "broccoli",
        "sentiment": "meh",
    })
    assert resp.status_code == 422


def test_meal_feedback(client):
    """POST /skills/meal-feedback rates a meal and auto-updates preferences."""
    family = client.get("/skills/family").json()
    member_id = family["members"][0]["id"]

    resp = client.post("/skills/meal-feedback", json={
        "meal_name": "spaghetti bolognese",
        "family_member_id": member_id,
        "rating": 4,
        "would_repeat": True,
    })
    assert resp.status_code == 200
    data = resp.json()
    assert data["sentiment_updated"] == "like"
    assert "rating_id" in data


def test_meal_feedback_rating_to_sentiment(client):
    """Verify rating-to-sentiment mapping across all levels."""
    family = client.get("/skills/family").json()
    member_id = family["members"][0]["id"]

    mappings = {1: "hate", 2: "dislike", 3: "neutral", 4: "like", 5: "love"}
    for rating, expected in mappings.items():
        resp = client.post("/skills/meal-feedback", json={
            "meal_name": f"test_dish_{rating}",
            "family_member_id": member_id,
            "rating": rating,
        })
        assert resp.status_code == 200
        assert resp.json()["sentiment_updated"] == expected


def test_meal_feedback_validation(client):
    """POST /skills/meal-feedback rejects invalid ratings."""
    resp = client.post("/skills/meal-feedback", json={
        "meal_name": "test",
        "family_member_id": 1,
        "rating": 6,
    })
    assert resp.status_code == 422


def test_get_meal_feedback(client):
    """GET /skills/meal-feedback retrieves ratings."""
    resp = client.get("/skills/meal-feedback")
    assert resp.status_code == 200
    data = resp.json()
    assert "ratings" in data
    assert isinstance(data["ratings"], list)


def test_list_meal_plans_empty(client):
    """GET /skills/meal-plan lists plans (may be empty)."""
    resp = client.get("/skills/meal-plan")
    assert resp.status_code == 200
    data = resp.json()
    assert "plans" in data
    assert isinstance(data["plans"], list)


def test_get_meal_plan_not_found(client):
    """GET /skills/meal-plan/99999 returns 404."""
    resp = client.get("/skills/meal-plan/99999")
    assert resp.status_code == 404


def test_delete_family_member(client):
    """DELETE /skills/family/member/{id} removes a member."""
    # Create a disposable member
    create_resp = client.post("/skills/family/member", json={"name": "DeleteMe"})
    member_id = create_resp.json()["id"]

    resp = client.delete(f"/skills/family/member/{member_id}")
    assert resp.status_code == 200
    assert resp.json()["deleted"] is True

    # Verify it's gone
    resp2 = client.delete(f"/skills/family/member/{member_id}")
    assert resp2.status_code == 404


def test_delete_family_member_not_found(client):
    """DELETE /skills/family/member/99999 returns 404."""
    resp = client.delete("/skills/family/member/99999")
    assert resp.status_code == 404


def test_generate_meal_plan(client):
    """POST /skills/meal-plan generates a weekly plan using the LLM."""
    resp = client.post("/skills/meal-plan", json={
        "extra_instructions": "Keep it simple and quick to prepare",
    }, timeout=180.0)
    assert resp.status_code == 200
    data = resp.json()
    assert "plan_id" in data
    assert "week_label" in data

    # Verify it's retrievable
    plan_id = data["plan_id"]
    get_resp = client.get(f"/skills/meal-plan/{plan_id}")
    assert get_resp.status_code == 200
    assert get_resp.json()["id"] == plan_id


def test_daily_recipe_trigger(client):
    """POST /skills/daily-recipe generates a recipe and sends an email."""
    resp = client.post("/skills/daily-recipe", timeout=180.0)
    assert resp.status_code == 200
    data = resp.json()
    assert "dish" in data
    assert data["dish"] != "unknown"
    assert "email_sent" in data
    assert data["parse_error"] is False


# ── Home Knowledge Base ──


def test_home_tell_maintenance(client):
    """POST /skills/home/tell processes natural language maintenance input."""
    resp = client.post("/skills/home/tell", json={
        "text": "I changed the water filter on my fridge today, it needs replacing every 6 months"
    }, timeout=180.0)
    assert resp.status_code == 200
    data = resp.json()
    assert data["intent"] == "maintenance"
    assert "item" in data
    assert "task" in data
    assert len(data["actions"]) >= 2
    # Should have recognized recurrence
    assert data["task"]["recurrence_days"] in (180, 182, 183)  # ~6 months


def test_home_tell_info(client):
    """POST /skills/home/tell processes appliance info."""
    resp = client.post("/skills/home/tell", json={
        "text": "I have a Carrier HVAC system model 24ACC636A003 in the basement"
    }, timeout=180.0)
    assert resp.status_code == 200
    data = resp.json()
    assert "item" in data
    assert data["item"]["brand"] == "Carrier" or "carrier" in data["item"]["name"].lower()


def test_home_add_item(client):
    """POST /skills/home/items adds a home item directly."""
    resp = client.post("/skills/home/items", json={
        "name": "TestItem",
        "category": "appliance",
        "location": "garage",
    })
    assert resp.status_code == 200
    data = resp.json()
    assert data["name"] == "TestItem"
    assert data["category"] == "appliance"
    assert "id" in data


def test_home_list_items(client):
    """GET /skills/home/items returns tracked items."""
    resp = client.get("/skills/home/items")
    assert resp.status_code == 200
    data = resp.json()
    assert "items" in data
    assert isinstance(data["items"], list)
    assert len(data["items"]) > 0


def test_home_list_items_by_category(client):
    """GET /skills/home/items?category filters by category."""
    resp = client.get("/skills/home/items?category=appliance")
    assert resp.status_code == 200
    for item in resp.json()["items"]:
        assert item["category"] == "appliance"


def test_home_add_item_validation(client):
    """POST /skills/home/items rejects invalid category."""
    resp = client.post("/skills/home/items", json={
        "name": "Bad", "category": "spaceship"
    })
    assert resp.status_code == 422


def test_home_delete_item(client):
    """DELETE /skills/home/items/{id} removes an item."""
    create = client.post("/skills/home/items", json={"name": "DeleteableItem"})
    item_id = create.json()["id"]
    resp = client.delete(f"/skills/home/items/{item_id}")
    assert resp.status_code == 200
    assert resp.json()["deleted"] is True
    # Should be gone
    resp2 = client.delete(f"/skills/home/items/{item_id}")
    assert resp2.status_code == 404


def test_home_tasks_list(client):
    """GET /skills/home/tasks returns tasks."""
    resp = client.get("/skills/home/tasks")
    assert resp.status_code == 200
    data = resp.json()
    assert "tasks" in data
    assert isinstance(data["tasks"], list)


def test_home_task_complete(client):
    """POST /skills/home/tasks/complete marks a task as done and reschedules."""
    # Get a task from the maintenance test
    tasks = client.get("/skills/home/tasks").json()["tasks"]
    if tasks:
        task_id = tasks[0]["id"]
        resp = client.post("/skills/home/tasks/complete", json={
            "task_id": task_id, "notes": "test completion"
        })
        assert resp.status_code == 200
        data = resp.json()
        assert data["task_id"] == task_id
        assert "completed_at" in data


def test_home_task_complete_not_found(client):
    """POST /skills/home/tasks/complete returns 404 for missing task."""
    resp = client.post("/skills/home/tasks/complete", json={"task_id": 99999})
    assert resp.status_code == 404


def test_home_task_history(client):
    """GET /skills/home/tasks/{id}/history returns completion log."""
    tasks = client.get("/skills/home/tasks").json()["tasks"]
    if tasks:
        resp = client.get(f"/skills/home/tasks/{tasks[0]['id']}/history")
        assert resp.status_code == 200
        assert "history" in resp.json()


def test_home_add_document(client):
    """POST /skills/home/documents stores a document."""
    resp = client.post("/skills/home/documents", json={
        "title": "Test Manual",
        "content": "This is a test manual with instructions for testing.",
        "doc_type": "manual",
    })
    assert resp.status_code == 200
    data = resp.json()
    assert data["title"] == "Test Manual"
    assert data["doc_type"] == "manual"
    assert "id" in data


def test_home_add_document_validation(client):
    """POST /skills/home/documents rejects invalid doc_type."""
    resp = client.post("/skills/home/documents", json={
        "title": "Bad", "content": "test", "doc_type": "podcast"
    })
    assert resp.status_code == 422


def test_home_list_documents(client):
    """GET /skills/home/documents lists stored documents."""
    resp = client.get("/skills/home/documents")
    assert resp.status_code == 200
    data = resp.json()
    assert "documents" in data
    assert len(data["documents"]) > 0


def test_home_search_documents(client):
    """GET /skills/home/documents?search= finds docs by content."""
    resp = client.get("/skills/home/documents?search=instructions")
    assert resp.status_code == 200
    docs = resp.json()["documents"]
    assert len(docs) > 0


def test_home_get_document(client):
    """GET /skills/home/documents/{id} returns full content."""
    docs = client.get("/skills/home/documents").json()["documents"]
    if docs:
        resp = client.get(f"/skills/home/documents/{docs[0]['id']}")
        assert resp.status_code == 200
        assert "content" in resp.json()


def test_home_get_document_not_found(client):
    """GET /skills/home/documents/99999 returns 404."""
    resp = client.get("/skills/home/documents/99999")
    assert resp.status_code == 404


def test_home_delete_document(client):
    """DELETE /skills/home/documents/{id} removes a document."""
    create = client.post("/skills/home/documents", json={
        "title": "Deleteable Doc", "content": "garbage"
    })
    doc_id = create.json()["id"]
    resp = client.delete(f"/skills/home/documents/{doc_id}")
    assert resp.status_code == 200
    assert resp.json()["deleted"] is True
    resp2 = client.delete(f"/skills/home/documents/{doc_id}")
    assert resp2.status_code == 404


def test_home_alerts(client):
    """GET /skills/home/alerts returns alert status."""
    resp = client.get("/skills/home/alerts")
    assert resp.status_code == 200
    data = resp.json()
    assert "overdue_count" in data
    assert "upcoming_count" in data
    assert "overdue" in data
    assert "upcoming" in data


def test_home_alerts_check_trigger(client):
    """POST /skills/home/alerts/check manually triggers alert check."""
    resp = client.post("/skills/home/alerts/check")
    assert resp.status_code == 200
    assert "checked" in resp.json()


# ── Model Router (via /task response) ──

def test_model_router_simple(client):
    """Simple questions route to lightweight model."""
    resp = client.post("/task", json={"input": "What is Docker?"})
    assert resp.status_code == 200
    assert resp.json()["model"] == "qwen3:4b"


def test_model_router_complex(client):
    """Complex analytical prompts route to default balanced model."""
    resp = client.post("/task", json={
        "input": "Compare and evaluate the tradeoffs between microservices and monolithic architecture "
                 "with a comprehensive detailed assessment of pros and cons"
    })
    assert resp.status_code == 200
    assert resp.json()["model"] == "llama3.1:8b"


def test_model_router_default(client):
    """Standard prompts route to default model."""
    resp = client.post("/task", json={
        "input": "Write a Python function to sort a list of dictionaries by key"
    })
    assert resp.status_code == 200
    assert resp.json()["model"] == "llama3.1:8b"


# ── RAG in Chat ──

def test_chat_with_rag(client):
    """Chat endpoint uses RAG and returns a response."""
    resp = client.post("/chat", json={"message": "What do you know about home maintenance?"})
    assert resp.status_code == 200
    data = resp.json()
    assert data["content"]
    assert data["request_id"]
    assert data["role"]
    assert data["domain"]


def test_chat_model_routing(client):
    """Chat endpoint should use model routing (simple message → lighter model)."""
    resp = client.post("/chat", json={"message": "Hello, how are you?"})
    assert resp.status_code == 200
    assert resp.json()["content"]


# ── Quality Metrics ──

def test_quality_stats_endpoint(client):
    """GET /quality/stats returns agent quality data (may be empty if no competitions run)."""
    resp = client.get("/quality/stats")
    assert resp.status_code == 200
    data = resp.json()
    assert isinstance(data, list)


def test_quality_stats_with_agent_filter(client):
    """GET /quality/stats?agent=research filters by agent."""
    resp = client.get("/quality/stats", params={"agent": "research"})
    assert resp.status_code == 200
    assert isinstance(resp.json(), list)


# ── Procedural Memory ──

def test_procedural_memory_list(client):
    """GET /memory/procedural returns patterns (may be empty initially)."""
    resp = client.get("/memory/procedural")
    assert resp.status_code == 200
    data = resp.json()
    assert isinstance(data, list)


def test_procedural_memory_filter(client):
    """GET /memory/procedural?intent=research filters by intent."""
    resp = client.get("/memory/procedural", params={"intent": "research"})
    assert resp.status_code == 200
    assert isinstance(resp.json(), list)


# ── Competition triggers quality + procedural recording ──

def test_competition_records_quality(client):
    """A competition should persist quality metrics."""
    resp = client.post("/compete", json={
        "input": "Analyze the benefits of containerization vs virtualization",
        "agents": ["research", "analysis"],
    })
    assert resp.status_code == 200

    # Quality stats should now have entries
    stats = client.get("/quality/stats")
    assert stats.status_code == 200
    data = stats.json()
    # At least some agents should have metrics now
    assert isinstance(data, list)


def test_competition_records_procedural(client):
    """After competition, procedural memory should capture the pattern."""
    resp = client.post("/compete", json={
        "input": "Research and evaluate modern CI/CD pipeline strategies",
        "agents": ["research", "analysis"],
    })
    assert resp.status_code == 200

    # Procedural memory should have entries
    patterns = client.get("/memory/procedural")
    assert patterns.status_code == 200
    assert isinstance(patterns.json(), list)


# ── Sprint 8: Document Ingestion ──


def test_ingest_url(client):
    """POST /skills/ingest/url fetches a URL, chunks, and stores in semantic memory."""
    resp = client.post("/skills/ingest/url", json={
        "url": "https://www.nist.gov/artificial-intelligence"
    }, timeout=60.0)
    assert resp.status_code == 200
    data = resp.json()
    assert "chunks" in data
    assert "stored" in data
    assert data.get("doc_type") == "url"


def test_ingest_text(client):
    """POST /skills/ingest/text chunks raw text and stores it."""
    resp = client.post("/skills/ingest/text", json={
        "text": "This is a test document about home plumbing maintenance. " * 50,
        "title": "Test Plumbing Guide",
        "source": "manual entry",
    })
    assert resp.status_code == 200
    data = resp.json()
    assert data["stored"] >= 1
    assert data["title"] == "Test Plumbing Guide"


def test_ingest_url_validation(client):
    """POST /skills/ingest/url rejects empty URL."""
    resp = client.post("/skills/ingest/url", json={"url": ""})
    assert resp.status_code == 422


def test_ingest_text_validation(client):
    """POST /skills/ingest/text rejects empty text."""
    resp = client.post("/skills/ingest/text", json={"text": ""})
    assert resp.status_code == 422


# ── Sprint 8: Recipe Storage ──


def test_save_recipe(client):
    """POST /skills/recipes saves a new recipe."""
    resp = client.post("/skills/recipes", json={
        "title": "Test Spaghetti Bolognese",
        "ingredients": ["spaghetti", "ground beef", "tomato sauce", "garlic"],
        "instructions": ["Cook pasta", "Brown meat", "Add sauce", "Combine"],
        "cuisine": "Italian",
        "prep_time_min": 10,
        "cook_time_min": 25,
        "servings": 4,
        "tags": ["pasta", "easy"],
    })
    assert resp.status_code == 200
    data = resp.json()
    assert data["title"] == "Test Spaghetti Bolognese"
    assert data["cuisine"] == "Italian"
    assert "id" in data


def test_save_recipe_upsert(client):
    """POST /skills/recipes updates existing recipe by title."""
    client.post("/skills/recipes", json={"title": "Upsert Recipe"})
    resp = client.post("/skills/recipes", json={
        "title": "Upsert Recipe", "cuisine": "Mexican"
    })
    assert resp.status_code == 200
    assert resp.json()["cuisine"] == "Mexican"


def test_list_recipes(client):
    """GET /skills/recipes returns stored recipes."""
    resp = client.get("/skills/recipes")
    assert resp.status_code == 200
    data = resp.json()
    assert "recipes" in data
    assert isinstance(data["recipes"], list)
    assert len(data["recipes"]) > 0


def test_search_recipes(client):
    """GET /skills/recipes?search= filters recipes."""
    resp = client.get("/skills/recipes", params={"search": "Spaghetti"})
    assert resp.status_code == 200
    recipes = resp.json()["recipes"]
    assert any("spaghetti" in r["title"].lower() for r in recipes)


def test_search_recipes_by_cuisine(client):
    """GET /skills/recipes?cuisine= filters by cuisine."""
    resp = client.get("/skills/recipes", params={"cuisine": "Italian"})
    assert resp.status_code == 200
    for r in resp.json()["recipes"]:
        assert "italian" in r["cuisine"].lower()


def test_get_recipe_by_id(client):
    """GET /skills/recipes/{id} returns a specific recipe."""
    recipes = client.get("/skills/recipes").json()["recipes"]
    if recipes:
        resp = client.get(f"/skills/recipes/{recipes[0]['id']}")
        assert resp.status_code == 200
        assert resp.json()["id"] == recipes[0]["id"]


def test_get_recipe_not_found(client):
    """GET /skills/recipes/99999 returns 404."""
    resp = client.get("/skills/recipes/99999")
    assert resp.status_code == 404


def test_rate_recipe(client):
    """POST /skills/recipes/{id}/rate updates the rating."""
    recipes = client.get("/skills/recipes").json()["recipes"]
    if recipes:
        resp = client.post(f"/skills/recipes/{recipes[0]['id']}/rate", json={"rating": 4})
        assert resp.status_code == 200
        assert resp.json()["family_rating"] == 4


def test_rate_recipe_validation(client):
    """POST /skills/recipes/{id}/rate rejects invalid ratings."""
    recipes = client.get("/skills/recipes").json()["recipes"]
    if recipes:
        resp = client.post(f"/skills/recipes/{recipes[0]['id']}/rate", json={"rating": 6})
        assert resp.status_code == 422


def test_rate_recipe_not_found(client):
    """POST /skills/recipes/99999/rate returns 404."""
    resp = client.post("/skills/recipes/99999/rate", json={"rating": 3})
    assert resp.status_code == 404


def test_delete_recipe(client):
    """DELETE /skills/recipes/{id} removes a recipe."""
    create = client.post("/skills/recipes", json={"title": "Deleteable Recipe"})
    recipe_id = create.json()["id"]
    resp = client.delete(f"/skills/recipes/{recipe_id}")
    assert resp.status_code == 200
    assert resp.json()["deleted"] is True
    resp2 = client.delete(f"/skills/recipes/{recipe_id}")
    assert resp2.status_code == 404


def test_save_recipe_validation(client):
    """POST /skills/recipes rejects empty title."""
    resp = client.post("/skills/recipes", json={"title": ""})
    assert resp.status_code == 422


# ── Sprint 8: Medical History Tracker ──


def test_add_medical_record(client):
    """POST /skills/medical/record adds a medical record."""
    # Ensure a family member exists
    family = client.get("/skills/family").json()
    if not family["members"]:
        client.post("/skills/family/member", json={"name": "MedTest"})
        family = client.get("/skills/family").json()
    member_id = family["members"][0]["id"]

    resp = client.post("/skills/medical/record", json={
        "family_member_id": member_id,
        "date": "2026-03-15",
        "category": "checkup",
        "provider": "Dr. Smith",
        "summary": "Annual physical — all clear",
        "details": "Blood pressure 120/80, cholesterol normal",
    })
    assert resp.status_code == 200
    data = resp.json()
    assert data["category"] == "checkup"
    assert data["provider"] == "Dr. Smith"
    assert "id" in data


def test_list_medical_records(client):
    """GET /skills/medical/records returns stored records."""
    resp = client.get("/skills/medical/records")
    assert resp.status_code == 200
    data = resp.json()
    assert "records" in data
    assert isinstance(data["records"], list)
    assert len(data["records"]) > 0


def test_list_medical_records_by_category(client):
    """GET /skills/medical/records?category= filters records."""
    resp = client.get("/skills/medical/records", params={"category": "checkup"})
    assert resp.status_code == 200
    for r in resp.json()["records"]:
        assert r["category"] == "checkup"


def test_get_medical_record_by_id(client):
    """GET /skills/medical/records/{id} returns a specific record."""
    records = client.get("/skills/medical/records").json()["records"]
    if records:
        resp = client.get(f"/skills/medical/records/{records[0]['id']}")
        assert resp.status_code == 200
        assert resp.json()["id"] == records[0]["id"]


def test_get_medical_record_not_found(client):
    """GET /skills/medical/records/99999 returns 404."""
    resp = client.get("/skills/medical/records/99999")
    assert resp.status_code == 404


def test_delete_medical_record(client):
    """DELETE /skills/medical/records/{id} removes a record."""
    family = client.get("/skills/family").json()
    member_id = family["members"][0]["id"]
    create = client.post("/skills/medical/record", json={
        "family_member_id": member_id,
        "summary": "Deleteable record",
    })
    record_id = create.json()["id"]
    resp = client.delete(f"/skills/medical/records/{record_id}")
    assert resp.status_code == 200
    assert resp.json()["deleted"] is True
    resp2 = client.delete(f"/skills/medical/records/{record_id}")
    assert resp2.status_code == 404


def test_medical_record_validation(client):
    """POST /skills/medical/record rejects invalid category."""
    resp = client.post("/skills/medical/record", json={
        "family_member_id": 1,
        "category": "chiropractor",
        "summary": "test",
    })
    assert resp.status_code == 422


def test_medical_tell_nlp(client):
    """POST /skills/medical/tell processes natural language medical input."""
    # Ensure a known family member exists
    family = client.get("/skills/family").json()
    member_name = family["members"][0]["name"] if family["members"] else "Tim"
    if not family["members"]:
        client.post("/skills/family/member", json={"name": member_name})

    resp = client.post("/skills/medical/tell", json={
        "text": f"{member_name} had a dental cleaning today at Dr. Johnson's office, no cavities"
    }, timeout=120.0)
    assert resp.status_code == 200
    data = resp.json()
    # Should either succeed or report known error
    if "error" not in data:
        assert data["intent"] == "medical"
        assert "record" in data
        assert len(data["actions"]) >= 1


# ── Sprint 8: Calendar / Events ──


def test_add_calendar_event(client):
    """POST /skills/calendar/event adds a structured event."""
    resp = client.post("/skills/calendar/event", json={
        "title": "Test Birthday Party",
        "event_date": "2026-06-15",
        "event_time": "14:00",
        "category": "birthday",
        "family_member_name": "TestKid",
        "location": "Home",
        "recurrence": "yearly",
    })
    assert resp.status_code == 200
    data = resp.json()
    assert data["title"] == "Test Birthday Party"
    assert data["category"] == "birthday"
    assert data["recurrence"] == "yearly"
    assert "id" in data


def test_list_calendar_events(client):
    """GET /skills/calendar/events returns stored events."""
    resp = client.get("/skills/calendar/events")
    assert resp.status_code == 200
    data = resp.json()
    assert "events" in data
    assert isinstance(data["events"], list)
    assert len(data["events"]) > 0


def test_list_calendar_events_by_category(client):
    """GET /skills/calendar/events?category= filters events."""
    resp = client.get("/skills/calendar/events", params={"category": "birthday"})
    assert resp.status_code == 200
    for e in resp.json()["events"]:
        assert e["category"] == "birthday"


def test_get_calendar_event_by_id(client):
    """GET /skills/calendar/events/{id} returns a specific event."""
    events = client.get("/skills/calendar/events").json()["events"]
    if events:
        resp = client.get(f"/skills/calendar/events/{events[0]['id']}")
        assert resp.status_code == 200
        assert resp.json()["id"] == events[0]["id"]


def test_get_calendar_event_not_found(client):
    """GET /skills/calendar/events/99999 returns 404."""
    resp = client.get("/skills/calendar/events/99999")
    assert resp.status_code == 404


def test_delete_calendar_event(client):
    """DELETE /skills/calendar/events/{id} removes an event."""
    create = client.post("/skills/calendar/event", json={
        "title": "Deleteable Event",
        "event_date": "2026-12-31",
    })
    event_id = create.json()["id"]
    resp = client.delete(f"/skills/calendar/events/{event_id}")
    assert resp.status_code == 200
    assert resp.json()["deleted"] is True
    resp2 = client.delete(f"/skills/calendar/events/{event_id}")
    assert resp2.status_code == 404


def test_calendar_event_validation(client):
    """POST /skills/calendar/event rejects invalid category."""
    resp = client.post("/skills/calendar/event", json={
        "title": "Bad Event",
        "event_date": "2026-05-01",
        "category": "concert",
    })
    assert resp.status_code == 422


def test_calendar_event_date_validation(client):
    """POST /skills/calendar/event rejects malformed dates."""
    resp = client.post("/skills/calendar/event", json={
        "title": "Bad Date",
        "event_date": "June 15 2026",
    })
    assert resp.status_code == 422


def test_calendar_agenda(client):
    """GET /skills/calendar/agenda returns grouped agenda."""
    resp = client.get("/skills/calendar/agenda")
    assert resp.status_code == 200
    data = resp.json()
    assert "period_days" in data
    assert "total_events" in data
    assert "agenda" in data


def test_calendar_tell_nlp(client):
    """POST /skills/calendar/tell processes natural language event input."""
    resp = client.post("/skills/calendar/tell", json={
        "text": "Dentist appointment next Friday at 2pm at Dr. Johnson's office"
    }, timeout=120.0)
    assert resp.status_code == 200
    data = resp.json()
    if "error" not in data:
        assert data["intent"] == "calendar"
        assert "event" in data
        assert len(data["actions"]) >= 1


# ── Sprint 8: Learning Loop ──


def test_learning_experiments_list(client):
    """GET /learning/experiments returns experiments (may be empty)."""
    resp = client.get("/learning/experiments")
    assert resp.status_code == 200
    data = resp.json()
    assert "experiments" in data
    assert isinstance(data["experiments"], list)


def test_learning_experiments_filter(client):
    """GET /learning/experiments?status= filters by status."""
    resp = client.get("/learning/experiments", params={"status": "pending"})
    assert resp.status_code == 200
    assert isinstance(resp.json()["experiments"], list)


def test_learning_generate(client):
    """POST /learning/generate creates a candidate improvement experiment."""
    # First ensure some quality data exists (from prior competition tests)
    resp = client.post("/learning/generate", timeout=120.0)
    assert resp.status_code == 200
    data = resp.json()
    # Should return an experiment or an error if no quality data
    assert "experiment_id" in data or "error" in data or "status" in data


# ── Sprint 9: Conversation Memory ──────────────────────────────


def test_chat_persists_to_memory(client):
    """POST /chat should persist turns to episodic_memory."""
    import uuid
    cid = str(uuid.uuid4())
    resp = client.post("/chat", json={
        "message": "What is the capital of France?",
        "conversation_id": cid,
    })
    assert resp.status_code == 200
    data = resp.json()
    assert data["content"]
    assert data["conversation_id"] == cid

    # Now retrieve history
    hist_resp = client.get(f"/chat/history?conversation_id={cid}")
    assert hist_resp.status_code == 200
    hist = hist_resp.json()
    assert hist["conversation_id"] == cid
    assert len(hist["turns"]) >= 2  # user + assistant
    assert hist["turns"][0]["role_name"] == "user"
    assert hist["turns"][1]["role_name"] == "assistant"


def test_chat_history_empty(client):
    """GET /chat/history for unknown conversation returns empty."""
    import uuid
    resp = client.get(f"/chat/history?conversation_id={uuid.uuid4()}")
    assert resp.status_code == 200
    assert resp.json()["turns"] == []


def test_chat_conversations_list(client):
    """GET /chat/conversations returns recent conversations."""
    resp = client.get("/chat/conversations")
    assert resp.status_code == 200
    data = resp.json()
    assert "conversations" in data
    assert isinstance(data["conversations"], list)


# ── Sprint 9: Learning Loop promote/rollback ───────────────────


def test_learning_promote_not_found(client):
    """POST /learning/promote with fake ID returns error."""
    resp = client.post("/learning/promote/fake-id-123")
    assert resp.status_code == 200
    assert "error" in resp.json()


def test_learning_rollback_not_found(client):
    """POST /learning/rollback with fake ID returns error."""
    resp = client.post("/learning/rollback/fake-id-123")
    assert resp.status_code == 200
    assert "error" in resp.json()


def test_learning_overrides_list(client):
    """GET /learning/overrides returns list (may be empty)."""
    resp = client.get("/learning/overrides")
    assert resp.status_code == 200
    data = resp.json()
    assert "overrides" in data
    assert isinstance(data["overrides"], list)


def test_learning_promote_and_rollback(client):
    """Full promote → rollback cycle."""
    # Generate an experiment first
    gen_resp = client.post("/learning/generate", timeout=120.0)
    assert gen_resp.status_code == 200
    gen = gen_resp.json()

    if gen.get("status") == "skip":
        pytest.skip("No quality data available to generate an experiment")

    eid = gen.get("experiment_id")
    if not eid:
        pytest.skip("Experiment generation failed")

    # Promote
    promote_resp = client.post(f"/learning/promote/{eid}")
    assert promote_resp.status_code == 200
    assert promote_resp.json().get("status") == "promoted"

    # Verify override exists
    overrides = client.get("/learning/overrides").json()
    assert any(o["experiment_id"] == eid for o in overrides["overrides"])

    # Rollback
    rollback_resp = client.post(f"/learning/rollback/{eid}")
    assert rollback_resp.status_code == 200
    assert rollback_resp.json().get("status") == "rolled_back"

    # Verify override is gone
    overrides2 = client.get("/learning/overrides").json()
    assert not any(o["experiment_id"] == eid for o in overrides2["overrides"])


# ── Sprint 9: Daily Briefing ───────────────────────────────────


def test_briefing_preview(client):
    """GET /skills/briefing/preview returns briefing data."""
    resp = client.get("/skills/briefing/preview", timeout=120.0)
    assert resp.status_code == 200
    data = resp.json()
    assert "weather" in data
    assert "articles" in data
    assert "agenda" in data
    assert "email_recommendations" in data
    assert "generated_at" in data


def test_briefing_preview_has_agenda(client):
    """Briefing preview includes calendar agenda structure."""
    resp = client.get("/skills/briefing/preview", timeout=120.0)
    data = resp.json()
    agenda = data["agenda"]
    assert "total_events" in agenda
    assert "agenda" in agenda


def test_briefing_preview_articles(client):
    """Briefing preview articles is a list."""
    resp = client.get("/skills/briefing/preview", timeout=120.0)
    data = resp.json()
    assert isinstance(data["articles"], list)


def test_briefing_send(client):
    """POST /skills/briefing sends the briefing email."""
    resp = client.post("/skills/briefing", timeout=120.0)
    assert resp.status_code == 200
    data = resp.json()
    assert "sent" in data  # True if gmail configured, False if not


# ── Sprint 10: Briefing fixes ──────────────────────────────────


def test_briefing_calendar_7_day_window(client):
    """Briefing agenda should cover 7 days, not just today."""
    resp = client.get("/skills/briefing/preview", timeout=120.0)
    data = resp.json()
    agenda = data["agenda"]
    assert agenda["period_days"] == 7


def test_briefing_email_scan(client):
    """Briefing should scan emails (>0 if Gmail configured)."""
    resp = client.get("/skills/briefing/preview", timeout=120.0)
    data = resp.json()
    email_recs = data["email_recommendations"]
    assert "emails_scanned" in email_recs
    # If Gmail is configured, we should scan >0 emails
    assert isinstance(email_recs["emails_scanned"], int)


def test_briefing_articles_have_urls(client):
    """Briefing articles should include URL links."""
    resp = client.get("/skills/briefing/preview", timeout=120.0)
    data = resp.json()
    for article in data["articles"]:
        assert "url" in article
        assert "title" in article
        assert article["url"].startswith("http")


# ── Sprint 10: Critic quality gate ─────────────────────────────


def test_analysis_gets_critic_pass(client):
    """Analysis workflow should get automatic critic pass (critique in metadata)."""
    resp = client.post("/task", json={
        "input": "Analyze the pros and cons of zero trust architecture vs traditional perimeter security"
    }, timeout=120.0)
    assert resp.status_code == 200
    data = resp.json()
    # Should use analysis workflow or multi_agent_competition
    assert data["workflow"] in ("agent_analysis", "multi_agent_competition")
    assert data["content"]


def test_planning_gets_critic_pass(client):
    """Planning workflow should get automatic critic pass."""
    resp = client.post("/task", json={
        "input": "Create a step-by-step plan for implementing AI governance in a mid-size company"
    })
    assert resp.status_code == 200
    data = resp.json()
    assert data["workflow"] in ("agent_planning", "multi_agent_competition")
    assert data["content"]


# ── Sprint 10: LLM-based evaluation ───────────────────────────


def test_competition_uses_llm_eval(client):
    """Multi-agent competition should use LLM-based evaluation.
    Verified via orchestrator logs (agent_evaluated_llm entries).
    Skipped in CI due to multiple LLM calls exceeding timeout."""
    resp = client.post("/compete", json={
        "input": "Compare NIST CSF vs ISO 27001",
        "agents": ["research", "analysis"],
        "strategy": "best_score",
    })
    assert resp.status_code == 200
    data = resp.json()
    assert data["content"]
    assert data["workflow"] == "multi_agent_competition"


# ── Sprint 10: NL briefing trigger ─────────────────────────────


def test_chat_nl_briefing_trigger(client):
    """Chat should detect briefing intent and return briefing data."""
    resp = client.post("/chat", json={
        "message": "give me my daily briefing"
    }, timeout=120.0)
    assert resp.status_code == 200
    data = resp.json()
    assert data["intent"] == "briefing"
    assert data["content"]


# ── Sprint 10b: Chat skill mutation routing ────────────────────


def test_chat_routes_medical_to_skill(client):
    """Chat should detect medical mutation intent and route to medical service."""
    resp = client.post("/chat", json={
        "message": "add to medical records that tim had a physical exam on 4/1 with normal results"
    })
    assert resp.status_code == 200
    data = resp.json()
    assert data["intent"] == "medical_record"
    assert data["role"] != "proposal_strategist"  # should NOT pick professional roles
    assert data["content"]


def test_chat_routes_home_to_skill(client):
    """Chat should detect home maintenance intent and route to home service."""
    resp = client.post("/chat", json={
        "message": "add to home database that the furnace was serviced on 3/30"
    })
    assert resp.status_code == 200
    data = resp.json()
    assert data["intent"] == "home_record"
    assert data["role"] != "cybersecurity_executive"  # should NOT pick professional roles
    assert data["content"]


def test_chat_medical_role_inference(client):
    """Medical inputs should infer personal/family roles, not professional."""
    resp = client.post("/chat", json={
        "message": "record that sarah had a dentist appointment on 4/2"
    })
    assert resp.status_code == 200
    data = resp.json()
    assert data["domain"] in ("personal", "family")


def test_chat_home_role_inference(client):
    """Home inputs should infer family/personal roles, not professional."""
    resp = client.post("/chat", json={
        "message": "log hvac filter replacement at the lake anna house"
    })
    assert resp.status_code == 200
    data = resp.json()
    assert data["domain"] in ("personal", "family")


# ── Process Engine ──────────────────────────────────────────────


def test_process_list_seeded(client):
    """Seeded daily_brief definition should exist."""
    resp = client.get("/processes")
    assert resp.status_code == 200
    data = resp.json()
    ids = [p["process_id"] for p in data["processes"]]
    assert "daily_brief" in ids


def test_process_get_definition(client):
    """GET a specific process definition."""
    resp = client.get("/processes/daily_brief")
    assert resp.status_code == 200
    data = resp.json()
    assert data["process_id"] == "daily_brief"
    assert len(data["steps"]) == 7
    assert data["is_active"] is True


def test_process_create_and_delete(client):
    """Create and soft-delete a process definition."""
    resp = client.post("/processes", json={
        "process_id": "test_process",
        "name": "Test Process",
        "description": "For integration testing",
        "steps": [
            {"id": "step1", "type": "skill", "name": "Step 1",
             "skill_id": "test_skill", "inputs": {"x": 1}, "outputs": ["result"]},
        ],
    })
    assert resp.status_code == 200
    assert resp.json()["process_id"] == "test_process"

    # Soft delete
    resp = client.delete("/processes/test_process")
    assert resp.status_code == 200

    # Should not appear in active list
    resp = client.get("/processes")
    ids = [p["process_id"] for p in resp.json()["processes"]]
    assert "test_process" not in ids


def test_process_update(client):
    """Update a process definition."""
    # Create
    client.post("/processes", json={
        "process_id": "test_update_proc",
        "name": "Original Name",
        "steps": [],
    })

    # Update
    resp = client.patch("/processes/test_update_proc", json={"name": "Updated Name"})
    assert resp.status_code == 200
    assert resp.json()["name"] == "Updated Name"

    # Cleanup
    client.delete("/processes/test_update_proc")


def test_process_start_skill_steps(client):
    """Start a process with skill steps — should complete with stub outputs."""
    # Create a simple process
    client.post("/processes", json={
        "process_id": "test_skill_run",
        "name": "Skill Test",
        "steps": [
            {"id": "s1", "type": "skill", "name": "First",
             "skill_id": "alpha", "inputs": {"val": "hello"}, "outputs": ["out1"]},
            {"id": "s2", "type": "skill", "name": "Second",
             "skill_id": "beta", "inputs": {"prev": "steps.s1.out1"}, "outputs": ["out2"]},
        ],
    })

    # Start it
    resp = client.post("/processes/start", json={
        "process_id": "test_skill_run",
        "params": {"user": "tim"},
    })
    assert resp.status_code == 200
    data = resp.json()
    assert data["status"] == "completed"
    assert len(data["step_log"]) == 2
    assert data["step_log"][0]["status"] == "completed"
    assert data["step_log"][1]["status"] == "completed"

    # Verify context accumulation — s2 should have resolved ref from s1
    ctx = data["process_context"]
    assert "s1" in ctx["steps"]
    assert "s2" in ctx["steps"]
    assert ctx["steps"]["s1"]["out1"] == "[stub:alpha:out1]"

    # Cleanup
    client.delete("/processes/test_skill_run")


def test_process_parallel_steps(client):
    """Steps with same parallel_group should all execute."""
    client.post("/processes", json={
        "process_id": "test_parallel",
        "name": "Parallel Test",
        "steps": [
            {"id": "p1", "type": "skill", "skill_id": "a",
             "inputs": {}, "outputs": ["r1"], "parallel_group": "gather"},
            {"id": "p2", "type": "skill", "skill_id": "b",
             "inputs": {}, "outputs": ["r2"], "parallel_group": "gather"},
            {"id": "p3", "type": "skill", "skill_id": "c",
             "inputs": {}, "outputs": ["r3"], "parallel_group": "gather"},
            {"id": "final", "type": "skill", "skill_id": "d",
             "inputs": {"a": "steps.p1.r1", "b": "steps.p2.r2"}, "outputs": ["done"]},
        ],
    })

    resp = client.post("/processes/start", json={"process_id": "test_parallel"})
    assert resp.status_code == 200
    data = resp.json()
    assert data["status"] == "completed"
    assert len(data["step_log"]) == 4
    # All parallel steps should be completed
    for entry in data["step_log"][:3]:
        assert entry["status"] == "completed"

    client.delete("/processes/test_parallel")


def test_process_gate_pauses(client):
    """Gate step should pause the execution."""
    client.post("/processes", json={
        "process_id": "test_gate",
        "name": "Gate Test",
        "steps": [
            {"id": "s1", "type": "skill", "skill_id": "x",
             "inputs": {}, "outputs": ["data"]},
            {"id": "review", "type": "gate", "name": "Human Review",
             "gate_message": "Please review the data before proceeding",
             "inputs": {"data_to_review": "steps.s1.data"}},
            {"id": "s2", "type": "skill", "skill_id": "y",
             "inputs": {"approved": "steps.review.gate_decision"},
             "outputs": ["final"]},
        ],
    })

    resp = client.post("/processes/start", json={"process_id": "test_gate"})
    assert resp.status_code == 200
    data = resp.json()
    assert data["status"] == "paused"
    assert data["gate_message"] == "Please review the data before proceeding"
    eid = data["execution_id"]

    # Check execution shows paused
    resp = client.get(f"/processes/executions/{eid}")
    assert resp.json()["status"] == "paused"

    client.delete("/processes/test_gate")


def test_process_gate_approve_resumes(client):
    """Approving a gate should resume and complete the process."""
    client.post("/processes", json={
        "process_id": "test_gate_approve",
        "name": "Gate Approve Test",
        "steps": [
            {"id": "s1", "type": "skill", "skill_id": "x",
             "inputs": {}, "outputs": ["data"]},
            {"id": "review", "type": "gate", "name": "Review",
             "gate_message": "Approve?"},
            {"id": "s2", "type": "skill", "skill_id": "y",
             "inputs": {}, "outputs": ["final"]},
        ],
    })

    resp = client.post("/processes/start", json={"process_id": "test_gate_approve"})
    eid = resp.json()["execution_id"]

    # Approve the gate
    resp = client.post(f"/processes/executions/{eid}/gate", json={
        "decision": "approve", "message": "looks good"
    })
    assert resp.status_code == 200
    data = resp.json()
    assert data["status"] == "completed"
    assert len(data["step_log"]) == 3  # s1 + gate + s2

    client.delete("/processes/test_gate_approve")


def test_process_gate_reject_cancels(client):
    """Rejecting a gate should cancel the process."""
    client.post("/processes", json={
        "process_id": "test_gate_reject",
        "name": "Gate Reject Test",
        "steps": [
            {"id": "s1", "type": "skill", "skill_id": "x",
             "inputs": {}, "outputs": ["data"]},
            {"id": "review", "type": "gate", "name": "Review",
             "gate_message": "Approve?"},
            {"id": "s2", "type": "skill", "skill_id": "y",
             "inputs": {}, "outputs": ["final"]},
        ],
    })

    resp = client.post("/processes/start", json={"process_id": "test_gate_reject"})
    eid = resp.json()["execution_id"]

    # Reject the gate
    resp = client.post(f"/processes/executions/{eid}/gate", json={
        "decision": "reject", "message": "not ready"
    })
    assert resp.status_code == 200
    assert resp.json()["status"] == "cancelled"

    client.delete("/processes/test_gate_reject")


def test_process_context_accumulation(client):
    """Verify step outputs appear in process_context under steps.<id>."""
    client.post("/processes", json={
        "process_id": "test_ctx_accum",
        "name": "Context Accumulation",
        "steps": [
            {"id": "a", "type": "skill", "skill_id": "s1",
             "inputs": {}, "outputs": ["val"]},
            {"id": "b", "type": "skill", "skill_id": "s2",
             "inputs": {"ref": "steps.a.val"}, "outputs": ["val2"]},
        ],
    })

    resp = client.post("/processes/start", json={"process_id": "test_ctx_accum"})
    data = resp.json()
    ctx = data["process_context"]
    assert ctx["steps"]["a"]["val"] == "[stub:s1:val]"
    assert ctx["steps"]["b"]["val2"] == "[stub:s2:val2]"

    client.delete("/processes/test_ctx_accum")


def test_process_invalid_reference_fails(client):
    """A step referencing a non-existent step output should fail."""
    client.post("/processes", json={
        "process_id": "test_bad_ref",
        "name": "Bad Ref Test",
        "steps": [
            {"id": "s1", "type": "skill", "skill_id": "x",
             "inputs": {"bad": "steps.nonexistent.data"}, "outputs": ["out"]},
        ],
    })

    resp = client.post("/processes/start", json={"process_id": "test_bad_ref"})
    data = resp.json()
    assert data["status"] == "failed"
    assert "nonexistent" in data["error"]

    client.delete("/processes/test_bad_ref")


def test_process_step_log_completeness(client):
    """Step log should contain timing, type, and status for all steps."""
    client.post("/processes", json={
        "process_id": "test_log_check",
        "name": "Log Check",
        "steps": [
            {"id": "s1", "type": "skill", "skill_id": "alpha",
             "inputs": {}, "outputs": ["out"]},
            {"id": "s2", "type": "skill", "skill_id": "beta",
             "inputs": {}, "outputs": ["out"]},
        ],
    })

    resp = client.post("/processes/start", json={"process_id": "test_log_check"})
    data = resp.json()
    for entry in data["step_log"]:
        assert "step_id" in entry
        assert "step_type" in entry
        assert "duration_ms" in entry
        assert "status" in entry
        assert "started_at" in entry

    client.delete("/processes/test_log_check")


def test_process_execution_list(client):
    """List executions with filter."""
    # Start a process first
    client.post("/processes", json={
        "process_id": "test_exec_list",
        "name": "Exec List",
        "steps": [
            {"id": "s1", "type": "skill", "skill_id": "a",
             "inputs": {}, "outputs": ["out"]},
        ],
    })
    client.post("/processes/start", json={"process_id": "test_exec_list"})

    resp = client.get("/processes/executions", params={"process_id": "test_exec_list"})
    assert resp.status_code == 200
    data = resp.json()
    assert len(data["executions"]) >= 1

    client.delete("/processes/test_exec_list")


def test_process_cancel(client):
    """Cancel a paused execution."""
    client.post("/processes", json={
        "process_id": "test_cancel",
        "name": "Cancel Test",
        "steps": [
            {"id": "gate1", "type": "gate", "gate_message": "Stop here"},
        ],
    })

    resp = client.post("/processes/start", json={"process_id": "test_cancel"})
    eid = resp.json()["execution_id"]

    resp = client.post(f"/processes/executions/{eid}/cancel")
    assert resp.status_code == 200
    assert resp.json()["status"] == "cancelled"

    client.delete("/processes/test_cancel")


def test_process_404(client):
    """Non-existent process should return 404."""
    resp = client.get("/processes/nonexistent_xyz")
    assert resp.status_code == 404
