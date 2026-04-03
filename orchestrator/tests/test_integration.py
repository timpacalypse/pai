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
    assert resp.json()["workflow"] == "retrieval_augmented"


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
