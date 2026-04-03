# Identity, Roles, and Goal Model

---

# 1. Purpose

This document defines the **identity model** used by the PAI system.

It enables the orchestrator to:

* interpret user intent in context
* adapt outputs based on active role(s)
* prioritize goals and constraints appropriately
* deliver responses aligned to real-world needs

This is a **core system component**, not optional metadata.

---

# 2. Identity Model Structure

The system uses a 3-layer hierarchy:

```text
Domain → Role → Objective Profile
```

---

## 2.1 Domain

Broad area of life:

* Professional
* Personal
* Family
* Intellectual Growth

---

## 2.2 Role

A specific identity within a domain that:

* defines goals
* influences decision-making
* shapes output behavior

---

## 2.3 Objective Profile

Each role includes:

* goals
* preferences
* constraints
* success criteria

---

# 3. Role Activation Model

## 3.1 Rules

* A request may activate:

  * 1 primary role
  * 0–1 secondary roles
* Roles may be:

  * explicitly specified
  * inferred from context
* Maximum active roles: **2**

---

## 3.2 Priority Logic

1. Explicit role (if provided)
2. Intent-based inference
3. Historical usage (future enhancement)

---

## 3.3 Output Influence

Roles modify:

* tone
* depth
* structure
* recommendations
* risk tolerance
* prioritization

---

# 4. Domains and Roles

---

# 4.1 PROFESSIONAL DOMAIN

## cybersecurity_executive

**Description:**
Senior director-level cybersecurity leader focused on strategy, modernization, and mission value.

**Goals:**

* align cybersecurity to business outcomes
* drive strategic initiatives
* improve organizational effectiveness
* identify high-value innovations

**Preferences:**

* executive summaries
* structured insights
* decision-ready outputs
* high signal-to-noise

**Constraints:**

* credibility
* accuracy
* defensibility
* enterprise relevance

---

## ai_cybersecurity_strategist

**Description:**
Focus on intersection of AI, cybersecurity, governance, and automation.

**Goals:**

* identify AI applications in security
* evaluate emerging tools and patterns
* translate innovation into practice
* develop thought leadership

**Preferences:**

* forward-looking insights
* architecture-oriented thinking
* comparative analysis
* actionable recommendations

**Constraints:**

* avoid hype
* prioritize practicality
* consider risk and governance

---

## ai_governance_practitioner

**Description:**
Focus on AI governance, policy, compliance, and control frameworks.

**Goals:**

* map governance frameworks to implementation
* assess risk and accountability structures
* operationalize AI governance

**Preferences:**

* control mapping
* framework alignment
* structured outputs

**Constraints:**

* compliance sensitivity
* terminology precision
* auditability

---

## educator_scholar

**Description:**
PhD/DSC-level thinker focused on teaching and deep understanding.

**Goals:**

* explain complex ideas clearly
* build layered understanding
* support learning and teaching

**Preferences:**

* conceptual clarity
* structured explanations
* progressive depth

**Constraints:**

* avoid oversimplification
* maintain rigor

---

## solutions_architect

**Description:**
Designs executable technical and operational solutions.

**Goals:**

* translate requirements into architecture
* produce implementable designs
* align tools, workflows, and systems

**Preferences:**

* modular design
* diagrams and decomposition
* roadmap-driven outputs

**Constraints:**

* implementation realism
* integration feasibility

---

## proposal_strategist

**Description:**
Focused on RFP responses and business development for government contracting.

**Goals:**

* produce winning proposals
* align with evaluation criteria
* differentiate solutions
* improve response efficiency

**Preferences:**

* compliance-focused writing
* structured artifacts
* evaluator-centric language

**Constraints:**

* solicitation fidelity
* credibility of claims
* time constraints

---

## Active Professional Projects

* ato_automation
* proposal_automation
* business_development_pipeline

---

# 4.2 PERSONAL DOMAIN

## fitness_longevity_optimist

**Description:**
Focused on long-term health, fitness, and longevity.

**Goals:**

* improve strength and conditioning
* enhance cardiovascular health
* extend healthspan
* maintain sustainable habits

**Preferences:**

* actionable plans
* evidence-informed guidance
* long-term consistency

**Constraints:**

* avoid unsustainable extremes
* balance effort and recovery

---

## aesthetics_focused_builder

**Description:**
Focused on physique and visual outcomes.

**Goals:**

* improve body composition
* enhance physical appearance
* track visible progress

**Preferences:**

* measurable plans
* physique-oriented strategies

**Constraints:**

* must not undermine health goals

---

## family_chef

**Description:**
Responsible for meal planning and cooking.

**Goals:**

* create meals family enjoys
* balance nutrition and taste
* support both daily and gourmet cooking

**Preferences:**

* adaptable recipes
* efficient prep
* kid-friendly options

**Constraints:**

* time
* family acceptance
* ingredient practicality

---

# 4.3 FAMILY DOMAIN

## family_activity_coordinator

**Description:**
Plans and organizes activities and schedules.

**Goals:**

* coordinate schedules effectively
* reduce friction
* ensure meaningful experiences

**Preferences:**

* clear planning
* logistics-focused outputs
* actionable recommendations

**Constraints:**

* time conflicts
* cost
* travel distance

---

## parent

**Description:**
Father focused on engagement, development, and connection.

**Goals:**

* build relationships
* support growth
* create positive experiences

**Preferences:**

* engaging ideas
* age-appropriate framing
* practical execution

**Constraints:**

* time availability
* competing responsibilities

---

# 4.4 INTELLECTUAL GROWTH DOMAIN

## polymath_in_training

**Description:**
Seeks broad and deep knowledge across disciplines.

**Goals:**

* expand knowledge breadth
* build cross-domain connections
* maintain curiosity
* develop intellectual versatility

**Preferences:**

* structured learning paths
* synthesis across fields
* high-value topics

**Constraints:**

* avoid shallow knowledge accumulation
* prioritize meaningful learning

---

# 5. Role Interaction Patterns

## 5.1 Primary + Secondary Role Model

Examples:

### Example 1

**Input:** ATO automation design

* Primary: solutions_architect
* Secondary: ai_cybersecurity_strategist

---

### Example 2

**Input:** Weekly meal plan for health + family

* Primary: family_chef
* Secondary: fitness_longevity_optimist

---

### Example 3

**Input:** Cyber AI trends briefing

* Primary: ai_cybersecurity_strategist
* Secondary: cybersecurity_executive

---

# 6. Output Behavior by Role

## Professional Roles

* structured
* strategic
* concise but high-value
* actionable

---

## Educator Role

* explanatory
* layered
* concept-driven

---

## Fitness/Longevity

* practical
* sustainable
* measurable

---

## Family Roles

* simple
* actionable
* time-aware

---

## Polymath Role

* exploratory
* cross-domain
* synthesis-driven

---

# 7. Implementation Guidance

## 7.1 Storage

Roles should be stored as:

* structured configuration (JSON/YAML)
* not hardcoded in logic

---

## 7.2 Orchestrator Integration

The orchestrator must:

1. detect role(s)
2. retrieve role context
3. inject into prompt construction
4. influence workflow selection

---

## 7.3 Extensibility

* roles must be easily addable
* no code changes required to add roles
* configuration-driven system

---

# 8. Future Enhancements

* dynamic role inference
* role weighting based on history
* role blending strategies
* adaptive behavior tuning

---

# 9. Definition of Success

The identity system is successful when:

* outputs differ meaningfully by role
* responses align with real-world goals
* system behavior feels context-aware
* recommendations are relevant and actionable

---
