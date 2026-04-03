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
    embedding vector(1536),
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
