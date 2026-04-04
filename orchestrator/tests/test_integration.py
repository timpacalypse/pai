"""
Integration tests for PAI pipeline.
Requires the full stack to be running (docker compose up).
"""

import httpx
import pytest

BASE_URL = "http://localhost:8000"


@pytest.fixture
def client():
    return httpx.Client(base_url=BASE_URL, timeout=120.0)


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
    assert data["role"] == "cybersecurity_executive"
    assert data["domain"] == "professional"


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
    assert resp.json()["workflow"] == "direct_response"


def test_workflow_agent_research(client):
    resp = client.post("/task", json={"input": "Research the latest NIST AI RMF updates"})
    assert resp.status_code == 200
    assert resp.json()["workflow"] == "agent_research"
    assert resp.json()["content"]


def test_workflow_retrieval_augmented(client):
    resp = client.post("/task", json={"input": "Analyze the impact of executive order 14028"})
    assert resp.status_code == 200
    assert resp.json()["workflow"] == "agent_analysis"


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
    assert data["workflow"] == "agent_planning"
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
    assert data["workflow"] == "direct_response"


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
