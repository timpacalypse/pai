-- PAI Database Initialization
-- Enables pgvector extension and creates core tables

CREATE EXTENSION IF NOT EXISTS vector;

-- Episodic Memory: interactions, tasks, outputs
CREATE TABLE IF NOT EXISTS episodic_memory (
    id SERIAL PRIMARY KEY,
    session_id UUID NOT NULL,
    role VARCHAR(50),
    request_type VARCHAR(100),
    input_text TEXT NOT NULL,
    output_text TEXT,
    metadata JSONB DEFAULT '{}',
    created_at TIMESTAMP WITH TIME ZONE DEFAULT NOW()
);

-- Semantic Memory: vector embeddings for knowledge retrieval
CREATE TABLE IF NOT EXISTS semantic_memory (
    id SERIAL PRIMARY KEY,
    content TEXT NOT NULL,
    embedding vector(768),
    source VARCHAR(255),
    metadata JSONB DEFAULT '{}',
    created_at TIMESTAMP WITH TIME ZONE DEFAULT NOW()
);

-- Procedural Memory: workflows and successful patterns
CREATE TABLE IF NOT EXISTS procedural_memory (
    id SERIAL PRIMARY KEY,
    workflow_name VARCHAR(255) NOT NULL,
    workflow_definition JSONB NOT NULL,
    success_rate FLOAT DEFAULT 0.0,
    usage_count INTEGER DEFAULT 0,
    metadata JSONB DEFAULT '{}',
    created_at TIMESTAMP WITH TIME ZONE DEFAULT NOW(),
    updated_at TIMESTAMP WITH TIME ZONE DEFAULT NOW()
);

-- Identity Memory: user roles, preferences, behavior patterns
CREATE TABLE IF NOT EXISTS identity_memory (
    id SERIAL PRIMARY KEY,
    role VARCHAR(100) NOT NULL UNIQUE,
    domain VARCHAR(50) NOT NULL,
    description TEXT DEFAULT '',
    goals JSONB DEFAULT '[]',
    preferences JSONB DEFAULT '[]',
    constraints JSONB DEFAULT '[]',
    behavior_patterns JSONB DEFAULT '{}',
    created_at TIMESTAMP WITH TIME ZONE DEFAULT NOW(),
    updated_at TIMESTAMP WITH TIME ZONE DEFAULT NOW()
);

-- Seed all roles from identity-and-roles spec
INSERT INTO identity_memory (role, domain, description, goals, preferences, constraints) VALUES
    -- Professional Domain
    ('cybersecurity_executive', 'professional',
     'Senior director-level cybersecurity leader focused on strategy, modernization, and mission value.',
     '["align cybersecurity to business outcomes", "drive strategic initiatives", "improve organizational effectiveness", "identify high-value innovations"]',
     '["executive summaries", "structured insights", "decision-ready outputs", "high signal-to-noise"]',
     '["credibility", "accuracy", "defensibility", "enterprise relevance"]'),

    ('ai_cybersecurity_strategist', 'professional',
     'Focus on intersection of AI, cybersecurity, governance, and automation.',
     '["identify AI applications in security", "evaluate emerging tools and patterns", "translate innovation into practice", "develop thought leadership"]',
     '["forward-looking insights", "architecture-oriented thinking", "comparative analysis", "actionable recommendations"]',
     '["avoid hype", "prioritize practicality", "consider risk and governance"]'),

    ('ai_governance_practitioner', 'professional',
     'Focus on AI governance, policy, compliance, and control frameworks.',
     '["map governance frameworks to implementation", "assess risk and accountability structures", "operationalize AI governance"]',
     '["control mapping", "framework alignment", "structured outputs"]',
     '["compliance sensitivity", "terminology precision", "auditability"]'),

    ('educator_scholar', 'professional',
     'PhD/DSC-level thinker focused on teaching and deep understanding.',
     '["explain complex ideas clearly", "build layered understanding", "support learning and teaching"]',
     '["conceptual clarity", "structured explanations", "progressive depth"]',
     '["avoid oversimplification", "maintain rigor"]'),

    ('solutions_architect', 'professional',
     'Designs executable technical and operational solutions.',
     '["translate requirements into architecture", "produce implementable designs", "align tools, workflows, and systems"]',
     '["modular design", "diagrams and decomposition", "roadmap-driven outputs"]',
     '["implementation realism", "integration feasibility"]'),

    ('proposal_strategist', 'professional',
     'Focused on RFP responses and business development for government contracting.',
     '["produce winning proposals", "align with evaluation criteria", "differentiate solutions", "improve response efficiency"]',
     '["compliance-focused writing", "structured artifacts", "evaluator-centric language"]',
     '["solicitation fidelity", "credibility of claims", "time constraints"]'),

    -- Personal Domain
    ('fitness_longevity_optimist', 'personal',
     'Focused on long-term health, fitness, and longevity.',
     '["improve strength and conditioning", "enhance cardiovascular health", "extend healthspan", "maintain sustainable habits"]',
     '["actionable plans", "evidence-informed guidance", "long-term consistency"]',
     '["avoid unsustainable extremes", "balance effort and recovery"]'),

    ('aesthetics_focused_builder', 'personal',
     'Focused on physique and visual outcomes.',
     '["improve body composition", "enhance physical appearance", "track visible progress"]',
     '["measurable plans", "physique-oriented strategies"]',
     '["must not undermine health goals"]'),

    ('family_chef', 'personal',
     'Responsible for meal planning and cooking.',
     '["create meals family enjoys", "balance nutrition and taste", "support both daily and gourmet cooking"]',
     '["adaptable recipes", "efficient prep", "kid-friendly options"]',
     '["time", "family acceptance", "ingredient practicality"]'),

    -- Family Domain
    ('family_activity_coordinator', 'family',
     'Plans and organizes activities and schedules.',
     '["coordinate schedules effectively", "reduce friction", "ensure meaningful experiences"]',
     '["clear planning", "logistics-focused outputs", "actionable recommendations"]',
     '["time conflicts", "cost", "travel distance"]'),

    ('parent', 'family',
     'Father focused on engagement, development, and connection.',
     '["build relationships", "support growth", "create positive experiences"]',
     '["engaging ideas", "age-appropriate framing", "practical execution"]',
     '["time availability", "competing responsibilities"]'),

    -- Intellectual Growth Domain
    ('polymath_in_training', 'intellectual_growth',
     'Seeks broad and deep knowledge across disciplines.',
     '["expand knowledge breadth", "build cross-domain connections", "maintain curiosity", "develop intellectual versatility"]',
     '["structured learning paths", "synthesis across fields", "high-value topics"]',
     '["avoid shallow knowledge accumulation", "prioritize meaningful learning"]')
ON CONFLICT (role) DO UPDATE SET
    domain = EXCLUDED.domain,
    description = EXCLUDED.description,
    goals = EXCLUDED.goals,
    preferences = EXCLUDED.preferences,
    constraints = EXCLUDED.constraints,
    updated_at = NOW();

-- Index for vector similarity search
CREATE INDEX IF NOT EXISTS idx_semantic_memory_embedding
    ON semantic_memory USING ivfflat (embedding vector_cosine_ops)
    WITH (lists = 100);

-- Index for episodic memory lookups
CREATE INDEX IF NOT EXISTS idx_episodic_memory_session
    ON episodic_memory (session_id);

CREATE INDEX IF NOT EXISTS idx_episodic_memory_role
    ON episodic_memory (role);

-- Article ledger: deduplication for scheduled research
CREATE TABLE IF NOT EXISTS article_ledger (
    id SERIAL PRIMARY KEY,
    url_hash VARCHAR(64) NOT NULL UNIQUE,
    url TEXT NOT NULL,
    title TEXT NOT NULL DEFAULT '',
    source VARCHAR(255) DEFAULT '',
    topic VARCHAR(255) DEFAULT '',
    score FLOAT DEFAULT 0.0,
    discovered_at TIMESTAMP WITH TIME ZONE DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS idx_article_ledger_url_hash
    ON article_ledger (url_hash);

CREATE INDEX IF NOT EXISTS idx_article_ledger_discovered
    ON article_ledger (discovered_at DESC);

-- ── Meal Planning Skill ──

-- Family members for meal preference tracking
CREATE TABLE IF NOT EXISTS family_members (
    id SERIAL PRIMARY KEY,
    name VARCHAR(100) NOT NULL UNIQUE,
    age_group VARCHAR(20) DEFAULT 'adult',  -- toddler, child, teen, adult
    dietary_restrictions TEXT[] DEFAULT '{}',
    notes TEXT DEFAULT '',
    created_at TIMESTAMP WITH TIME ZONE DEFAULT NOW()
);

-- Ingredient and food preferences per family member
CREATE TABLE IF NOT EXISTS meal_preferences (
    id SERIAL PRIMARY KEY,
    family_member_id INTEGER NOT NULL REFERENCES family_members(id) ON DELETE CASCADE,
    item VARCHAR(255) NOT NULL,           -- ingredient, dish, or cuisine name
    item_type VARCHAR(50) DEFAULT 'dish', -- dish, ingredient, cuisine, cooking_method
    sentiment VARCHAR(10) NOT NULL,       -- love, like, neutral, dislike, hate, allergy
    notes TEXT DEFAULT '',
    created_at TIMESTAMP WITH TIME ZONE DEFAULT NOW(),
    updated_at TIMESTAMP WITH TIME ZONE DEFAULT NOW(),
    UNIQUE(family_member_id, item, item_type)
);

CREATE INDEX IF NOT EXISTS idx_meal_preferences_member
    ON meal_preferences (family_member_id);

-- Meal plans and history
CREATE TABLE IF NOT EXISTS meal_plans (
    id SERIAL PRIMARY KEY,
    week_label VARCHAR(50) NOT NULL,      -- e.g. "2026-W14"
    plan JSONB NOT NULL,                  -- structured plan: {monday: {dinner: {...}}, ...}
    preferences_snapshot JSONB DEFAULT '{}', -- snapshot of prefs used to generate
    model VARCHAR(50) DEFAULT '',
    created_at TIMESTAMP WITH TIME ZONE DEFAULT NOW()
);

-- Individual meal ratings (feedback)
CREATE TABLE IF NOT EXISTS meal_ratings (
    id SERIAL PRIMARY KEY,
    meal_plan_id INTEGER REFERENCES meal_plans(id) ON DELETE SET NULL,
    meal_name VARCHAR(255) NOT NULL,
    day_of_week VARCHAR(10) DEFAULT '',
    family_member_id INTEGER REFERENCES family_members(id) ON DELETE SET NULL,
    rating INTEGER NOT NULL CHECK (rating BETWEEN 1 AND 5),
    would_repeat BOOLEAN DEFAULT TRUE,
    notes TEXT DEFAULT '',
    created_at TIMESTAMP WITH TIME ZONE DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS idx_meal_ratings_plan
    ON meal_ratings (meal_plan_id);

CREATE INDEX IF NOT EXISTS idx_meal_ratings_member
    ON meal_ratings (family_member_id);

-- ── Home Knowledge Base ────────────────────────────────────────

-- Home items: appliances, systems, areas
CREATE TABLE IF NOT EXISTS home_items (
    id SERIAL PRIMARY KEY,
    name VARCHAR(255) NOT NULL,
    category VARCHAR(100) DEFAULT 'general', -- appliance, hvac, plumbing, electrical, outdoor, vehicle, general
    location VARCHAR(255) DEFAULT '',         -- kitchen, garage, whole house, etc.
    brand VARCHAR(255) DEFAULT '',
    model_info VARCHAR(255) DEFAULT '',
    purchase_date DATE,
    notes TEXT DEFAULT '',
    created_at TIMESTAMP WITH TIME ZONE DEFAULT NOW(),
    updated_at TIMESTAMP WITH TIME ZONE DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS idx_home_items_category
    ON home_items (category);

-- Recurring maintenance tasks
CREATE TABLE IF NOT EXISTS home_tasks (
    id SERIAL PRIMARY KEY,
    home_item_id INTEGER REFERENCES home_items(id) ON DELETE CASCADE,
    description TEXT NOT NULL,             -- "replace air filter"
    recurrence_days INTEGER DEFAULT 0,     -- 0 = one-time, 90 = every 3 months
    last_completed_at TIMESTAMP WITH TIME ZONE,
    next_due_at TIMESTAMP WITH TIME ZONE,
    alert_days_before INTEGER DEFAULT 7,   -- send alert this many days before due
    priority VARCHAR(20) DEFAULT 'normal', -- low, normal, high, critical
    notes TEXT DEFAULT '',
    created_at TIMESTAMP WITH TIME ZONE DEFAULT NOW(),
    updated_at TIMESTAMP WITH TIME ZONE DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS idx_home_tasks_next_due
    ON home_tasks (next_due_at);

CREATE INDEX IF NOT EXISTS idx_home_tasks_item
    ON home_tasks (home_item_id);

-- Task completion log
CREATE TABLE IF NOT EXISTS home_task_log (
    id SERIAL PRIMARY KEY,
    home_task_id INTEGER NOT NULL REFERENCES home_tasks(id) ON DELETE CASCADE,
    completed_at TIMESTAMP WITH TIME ZONE DEFAULT NOW(),
    notes TEXT DEFAULT '',
    cost NUMERIC(10, 2) DEFAULT 0
);

-- Home documents: manuals, warranties, reference info
CREATE TABLE IF NOT EXISTS home_documents (
    id SERIAL PRIMARY KEY,
    home_item_id INTEGER REFERENCES home_items(id) ON DELETE SET NULL,
    title VARCHAR(500) NOT NULL,
    doc_type VARCHAR(50) DEFAULT 'manual', -- manual, warranty, receipt, notes, reference
    content TEXT NOT NULL,                 -- full text content
    source VARCHAR(500) DEFAULT '',        -- URL or file name
    metadata JSONB DEFAULT '{}',
    created_at TIMESTAMP WITH TIME ZONE DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS idx_home_docs_item
    ON home_documents (home_item_id);

CREATE INDEX IF NOT EXISTS idx_home_docs_type
    ON home_documents (doc_type);

-- Quality Metrics: persistent score storage for agent outputs
CREATE TABLE IF NOT EXISTS quality_metrics (
    id SERIAL PRIMARY KEY,
    request_id UUID NOT NULL,
    intent VARCHAR(50) NOT NULL,
    workflow VARCHAR(50) NOT NULL,
    agent_name VARCHAR(50) NOT NULL,
    model VARCHAR(50) DEFAULT '',
    accuracy FLOAT DEFAULT 0,
    relevance FLOAT DEFAULT 0,
    depth FLOAT DEFAULT 0,
    clarity FLOAT DEFAULT 0,
    actionability FLOAT DEFAULT 0,
    consistency FLOAT DEFAULT 0,
    total FLOAT DEFAULT 0,
    was_selected BOOLEAN DEFAULT FALSE,
    created_at TIMESTAMP WITH TIME ZONE DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS idx_quality_agent
    ON quality_metrics (agent_name);

CREATE INDEX IF NOT EXISTS idx_quality_intent
    ON quality_metrics (intent);

CREATE INDEX IF NOT EXISTS idx_quality_created
    ON quality_metrics (created_at);

-- ── Sprint 8: Recipes ──────────────────────────────────────────

CREATE TABLE IF NOT EXISTS recipes (
    id SERIAL PRIMARY KEY,
    title VARCHAR(500) NOT NULL,
    ingredients TEXT[] DEFAULT '{}',
    instructions TEXT[] DEFAULT '{}',
    source VARCHAR(255) DEFAULT '',        -- "family", "serious eats", etc.
    source_url VARCHAR(500) DEFAULT '',
    cuisine VARCHAR(100) DEFAULT '',
    prep_time_min INTEGER DEFAULT 0,
    cook_time_min INTEGER DEFAULT 0,
    servings INTEGER DEFAULT 0,
    tags TEXT[] DEFAULT '{}',
    notes TEXT DEFAULT '',
    family_rating INTEGER CHECK (family_rating IS NULL OR family_rating BETWEEN 1 AND 5),
    created_at TIMESTAMP WITH TIME ZONE DEFAULT NOW(),
    updated_at TIMESTAMP WITH TIME ZONE DEFAULT NOW()
);

CREATE UNIQUE INDEX IF NOT EXISTS idx_recipes_title_lower
    ON recipes (LOWER(title));

-- ── Sprint 8: Medical Records ──────────────────────────────────

CREATE TABLE IF NOT EXISTS medical_records (
    id SERIAL PRIMARY KEY,
    family_member_id INTEGER NOT NULL REFERENCES family_members(id) ON DELETE CASCADE,
    record_date DATE NOT NULL DEFAULT CURRENT_DATE,
    category VARCHAR(50) DEFAULT 'other',  -- checkup, dental, vision, specialist, emergency, lab, vaccination, prescription, surgery, mental_health, other
    provider VARCHAR(255) DEFAULT '',
    summary TEXT NOT NULL DEFAULT '',
    details TEXT DEFAULT '',
    follow_up TEXT DEFAULT '',
    medications TEXT[] DEFAULT '{}',
    vitals JSONB DEFAULT '{}',
    file_references JSONB DEFAULT '[]',    -- [{filename, path}]
    created_at TIMESTAMP WITH TIME ZONE DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS idx_medical_member
    ON medical_records (family_member_id);

CREATE INDEX IF NOT EXISTS idx_medical_date
    ON medical_records (record_date DESC);

CREATE INDEX IF NOT EXISTS idx_medical_category
    ON medical_records (category);

-- ── Sprint 8: Family Events / Calendar ─────────────────────────

CREATE TABLE IF NOT EXISTS family_events (
    id SERIAL PRIMARY KEY,
    title VARCHAR(500) NOT NULL,
    event_date DATE NOT NULL,
    event_time VARCHAR(10) DEFAULT '',     -- HH:MM
    end_time VARCHAR(10) DEFAULT '',
    category VARCHAR(50) DEFAULT 'other',  -- birthday, appointment, school, activity, holiday, travel, deadline, reminder, other
    family_member_id INTEGER REFERENCES family_members(id) ON DELETE SET NULL,
    family_member_name VARCHAR(100) DEFAULT 'family',
    location VARCHAR(500) DEFAULT '',
    recurrence VARCHAR(20) DEFAULT 'none', -- none, weekly, monthly, yearly
    notes TEXT DEFAULT '',
    created_at TIMESTAMP WITH TIME ZONE DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS idx_events_date
    ON family_events (event_date);

CREATE INDEX IF NOT EXISTS idx_events_member
    ON family_events (family_member_id);

-- ── Sprint 8: Learning Experiments ─────────────────────────────

CREATE TABLE IF NOT EXISTS learning_experiments (
    id SERIAL PRIMARY KEY,
    experiment_id VARCHAR(64) NOT NULL UNIQUE,
    improvement JSONB NOT NULL,
    baseline_stats JSONB DEFAULT '[]',
    result_stats JSONB DEFAULT '[]',
    status VARCHAR(20) DEFAULT 'pending',  -- pending, promoted, rejected, inconclusive
    verdict TEXT DEFAULT '',
    created_at TIMESTAMP WITH TIME ZONE DEFAULT NOW(),
    evaluated_at TIMESTAMP WITH TIME ZONE
);

CREATE INDEX IF NOT EXISTS idx_experiments_status
    ON learning_experiments (status);

-- ── Sprint 9: Prompt Overrides (Learning Loop promote/rollback) ──

CREATE TABLE IF NOT EXISTS prompt_overrides (
    id SERIAL PRIMARY KEY,
    target VARCHAR(50) NOT NULL,           -- 'agent_prompt' or 'workflow_rule'
    agent_name VARCHAR(50) DEFAULT '',     -- which agent this targets
    original_value TEXT DEFAULT '',         -- stored on first promote for rollback
    override_value TEXT NOT NULL,           -- the new prompt/rule text
    experiment_id VARCHAR(64) NOT NULL,
    active BOOLEAN DEFAULT TRUE,
    created_at TIMESTAMP WITH TIME ZONE DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS idx_overrides_agent
    ON prompt_overrides (agent_name, active);

CREATE UNIQUE INDEX IF NOT EXISTS idx_overrides_experiment
    ON prompt_overrides (experiment_id);


-- ── Process Engine ──────────────────────────────────────────────

CREATE TABLE IF NOT EXISTS process_definitions (
    id              SERIAL PRIMARY KEY,
    process_id      VARCHAR(120) NOT NULL UNIQUE,
    name            VARCHAR(255) NOT NULL,
    description     TEXT DEFAULT '',
    roles           JSONB DEFAULT '[]',         -- list of role names this process applies to
    trigger_config  JSONB DEFAULT '{}',         -- {"type": "manual|scheduled", "cron": "..."}
    steps           JSONB DEFAULT '[]',         -- ordered list of step definitions
    is_active       BOOLEAN DEFAULT TRUE,
    execution_count INTEGER DEFAULT 0,
    success_count   INTEGER DEFAULT 0,
    avg_duration_ms FLOAT DEFAULT 0,
    created_at      TIMESTAMP WITH TIME ZONE DEFAULT NOW(),
    updated_at      TIMESTAMP WITH TIME ZONE DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS idx_procdef_active ON process_definitions (is_active);

CREATE TABLE IF NOT EXISTS process_executions (
    id               SERIAL PRIMARY KEY,
    execution_id     VARCHAR(64) NOT NULL UNIQUE,   -- uuid
    process_id       VARCHAR(120) NOT NULL REFERENCES process_definitions(process_id),
    status           VARCHAR(20) DEFAULT 'running', -- running, paused, completed, failed, cancelled
    current_step_idx INTEGER DEFAULT 0,
    process_context  JSONB DEFAULT '{}',            -- accumulated outputs from all steps
    trigger_params   JSONB DEFAULT '{}',            -- initial input params
    role             VARCHAR(60) DEFAULT '',
    step_log         JSONB DEFAULT '[]',            -- [{step_id, type, start, end, duration_ms, status, error}]
    gate_message     TEXT DEFAULT '',               -- message to show human when paused at gate
    gate_context     JSONB DEFAULT '{}',            -- relevant data for gate decision
    started_at       TIMESTAMP WITH TIME ZONE DEFAULT NOW(),
    completed_at     TIMESTAMP WITH TIME ZONE,
    error            TEXT DEFAULT '',
    created_at       TIMESTAMP WITH TIME ZONE DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS idx_procexec_process ON process_executions (process_id);
CREATE INDEX IF NOT EXISTS idx_procexec_status  ON process_executions (status);

-- ── Workout Tracking ──────────────────────────────────────────

CREATE TABLE IF NOT EXISTS workout_programs (
    id SERIAL PRIMARY KEY,
    name VARCHAR(255) NOT NULL,
    activity VARCHAR(100) NOT NULL,
    days_of_week INTEGER[] NOT NULL DEFAULT '{}',
    duration_minutes INTEGER DEFAULT 30,
    notes TEXT DEFAULT '',
    is_active BOOLEAN DEFAULT TRUE,
    created_at TIMESTAMP WITH TIME ZONE DEFAULT NOW(),
    updated_at TIMESTAMP WITH TIME ZONE DEFAULT NOW()
);
CREATE INDEX IF NOT EXISTS idx_workout_programs_active ON workout_programs (is_active);

CREATE TABLE IF NOT EXISTS workout_logs (
    id SERIAL PRIMARY KEY,
    workout_program_id INTEGER REFERENCES workout_programs(id) ON DELETE SET NULL,
    activity VARCHAR(100) NOT NULL,
    duration_minutes INTEGER DEFAULT 0,
    log_date DATE NOT NULL DEFAULT CURRENT_DATE,
    notes TEXT DEFAULT '',
    metrics JSONB DEFAULT '{}',
    created_at TIMESTAMP WITH TIME ZONE DEFAULT NOW()
);
CREATE INDEX IF NOT EXISTS idx_workout_logs_date ON workout_logs (log_date DESC);
