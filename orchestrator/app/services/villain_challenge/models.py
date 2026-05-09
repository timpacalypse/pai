"""Villain Challenge System — Models, Enums, and Villain Catalog.

Marvel X-Men universe themed fitness challenge system.
"""

from dataclasses import dataclass, field

# ── Hero Tiers ──

HERO_TIERS = [
    {"name": "Street Level",       "min_hci": 0,  "max_hci": 20},
    {"name": "Enhanced Human",     "min_hci": 21, "max_hci": 35},
    {"name": "Super Soldier",      "min_hci": 36, "max_hci": 50},
    {"name": "Mutant Operative",   "min_hci": 51, "max_hci": 65},
    {"name": "Alpha Mutant",       "min_hci": 66, "max_hci": 78},
    {"name": "Omega Mutant",       "min_hci": 79, "max_hci": 88},
    {"name": "Cosmic Entity",      "min_hci": 89, "max_hci": 95},
    {"name": "Beyond Omega",       "min_hci": 96, "max_hci": 100},
]

# ── HCI Domain Weights ──

HCI_WEIGHTS = {
    "strength":            0.30,
    "physique":            0.20,
    "conditioning":        0.20,
    "recovery":            0.10,
    "consistency":         0.10,
    "nutrition_adherence": 0.05,
    "mobility":            0.05,
}

ALL_DOMAINS = list(HCI_WEIGHTS.keys())

COMBAT_READINESS_INPUTS = ["recovery", "consistency", "strength", "conditioning"]

# ── Battle Outcomes ──

BATTLE_OUTCOMES = [
    {"name": "Overwhelming Victory", "min_score": 90, "xp_mult": 2.0},
    {"name": "Victory",              "min_score": 75, "xp_mult": 1.5},
    {"name": "Narrow Victory",       "min_score": 65, "xp_mult": 1.2},
    {"name": "Stalemate",            "min_score": 50, "xp_mult": 0.5},
    {"name": "Defeat",               "min_score": 35, "xp_mult": 0.2},
    {"name": "Severe Defeat",        "min_score": 0,  "xp_mult": 0.0},
]

BATTLE_STATUSES = [
    {"name": "Dominating", "min_prob": 80},
    {"name": "Advantage",  "min_prob": 65},
    {"name": "Contested",  "min_prob": 45},
    {"name": "Danger",     "min_prob": 25},
    {"name": "Critical",   "min_prob": 0},
]

# ── XP Awards ──

XP_AWARDS = {
    "daily_checkin":         25,
    "workout_completed":     50,
    "strength_session":      60,
    "cardio_session":        50,
    "mobility_session":      30,
    "sleep_target_hit":      35,
    "nutrition_target_hit":  30,
    "pr_achieved":          150,
    "weekly_challenge_won":  200,
    "villain_defeated":      250,
    "nemesis_defeated":      500,
    "perfect_week":          300,
    "streak_7_day":         100,
    "streak_14_day":        200,
    "streak_30_day":        500,
}

XP_PENALTIES = {
    "missed_checkin":       -10,
    "missed_objective":     -15,
    "ignored_recovery":     -20,
}

# ── Archetype Definitions ──

ARCHETYPES = {
    "gamma_mutation": {
        "name": "Gamma Mutation Candidate",
        "description": "Raw strength dominates. You channel pure power like the Hulk.",
        "primary": "strength",
        "secondary": "physique",
        "threshold": {"strength": 70, "physique": 50},
    },
    "spider_hybrid": {
        "name": "Spider-Type Hybrid",
        "description": "Explosive conditioning meets agility. Speed and endurance define you.",
        "primary": "conditioning",
        "secondary": "mobility",
        "threshold": {"conditioning": 65, "mobility": 50},
    },
    "wakandan_elite": {
        "name": "Wakandan Elite",
        "description": "Balanced excellence. Tactical discipline across all domains.",
        "primary": "consistency",
        "secondary": "nutrition_adherence",
        "threshold": {"consistency": 65, "nutrition_adherence": 60},
    },
    "mystic_discipline": {
        "name": "Mystic Discipline Path",
        "description": "Recovery mastery and mental fortitude. You regenerate and adapt.",
        "primary": "recovery",
        "secondary": "mobility",
        "threshold": {"recovery": 70, "mobility": 55},
    },
    "iron_legion": {
        "name": "Iron Legion Tactical Specialist",
        "description": "Precision engineering. Nutrition, tracking, and consistency are your armor.",
        "primary": "nutrition_adherence",
        "secondary": "consistency",
        "threshold": {"nutrition_adherence": 65, "consistency": 65},
    },
    "super_soldier": {
        "name": "Super Soldier",
        "description": "Strength and conditioning in balance. The serum chose well.",
        "primary": "strength",
        "secondary": "conditioning",
        "threshold": {"strength": 60, "conditioning": 60},
    },
    "wolverine_class": {
        "name": "Wolverine-Class Regenerator",
        "description": "Recovery and combat readiness. You heal fast and fight relentlessly.",
        "primary": "recovery",
        "secondary": "strength",
        "threshold": {"recovery": 65, "strength": 60},
    },
    "omega_mutant": {
        "name": "Omega-Level Mutant",
        "description": "Transcendent. All domains elevated. Reality bends to your will.",
        "primary": "strength",
        "secondary": "conditioning",
        "threshold": {d: 75 for d in ALL_DOMAINS},
    },
}

# ── Power Surge Definitions ──

POWER_SURGES = {
    "adrenaline_protocol": {
        "name": "Adrenaline Protocol",
        "trigger": "streak_7",
        "xp_multiplier": 1.15,
        "battle_bonus": 5.0,
        "duration_days": 5,
        "description": "+15% XP gain for 5 days. Increased probability of defeating higher-tier villains.",
    },
    "berserker_rage": {
        "name": "Berserker Rage",
        "trigger": "pr_achieved",
        "xp_multiplier": 1.20,
        "battle_bonus": 8.0,
        "duration_days": 3,
        "description": "+20% XP gain for 3 days. Raw power surge detected.",
    },
    "phoenix_force": {
        "name": "Phoenix Force Manifestation",
        "trigger": "elite_sleep_week",
        "xp_multiplier": 1.25,
        "battle_bonus": 10.0,
        "duration_days": 7,
        "description": "+25% XP for 7 days. Recovery approaching cosmic levels.",
    },
    "vibranium_protocol": {
        "name": "Vibranium Protocol",
        "trigger": "perfect_adherence",
        "xp_multiplier": 1.30,
        "battle_bonus": 12.0,
        "duration_days": 5,
        "description": "+30% XP for 5 days. Tactical perfection detected. Vibranium-grade discipline.",
    },
    "weapon_x_activation": {
        "name": "Weapon X Activation",
        "trigger": "streak_14",
        "xp_multiplier": 1.20,
        "battle_bonus": 7.0,
        "duration_days": 7,
        "description": "+20% XP for 7 days. 14-day consistency. Adamantium-laced resolve.",
    },
    "celestial_awakening": {
        "name": "Celestial Awakening",
        "trigger": "streak_30",
        "xp_multiplier": 1.50,
        "battle_bonus": 15.0,
        "duration_days": 10,
        "description": "+50% XP for 10 days. 30-day streak. Cosmic-tier commitment unlocked.",
    },
}

# ── Narrative Tones ──

NARRATIVE_TONES = {
    "jarvis": {
        "name": "JARVIS",
        "style": "analytical and polished",
        "opener": "Good evening, sir.",
        "mission_prefix": "TACTICAL ANALYSIS:",
    },
    "nick_fury": {
        "name": "Nick Fury",
        "style": "direct and demanding",
        "opener": "Listen up.",
        "mission_prefix": "SITUATION REPORT:",
    },
    "thor": {
        "name": "Thor",
        "style": "intense and heroic",
        "opener": "Hear me, warrior!",
        "mission_prefix": "BY ODIN'S COMMAND:",
    },
    "doom": {
        "name": "Doctor Doom",
        "style": "antagonist accountability",
        "opener": "Doom observes your weakness.",
        "mission_prefix": "DOOM DECREES:",
    },
    "shield_tactical": {
        "name": "S.H.I.E.L.D. Tactical AI",
        "style": "military-style mission control",
        "opener": "AVENGERS PRIORITY ALERT.",
        "mission_prefix": "AVENGERS MISSION BRIEFING:",
    },
}


# ── Villain Catalog ──

@dataclass
class Villain:
    id: str
    name: str
    tier: str
    base_hci: int
    description: str
    domain_weights: dict = field(default_factory=dict)
    weakness_text: str = ""
    victory_text: str = ""
    defeat_text: str = ""
    affiliation: str = ""
    tags: list = field(default_factory=list)


VILLAIN_CATALOG: dict[str, Villain] = {}


def _v(id, name, tier, base_hci, desc, weights, weakness="", victory="", defeat="", affiliation="", tags=None):
    VILLAIN_CATALOG[id] = Villain(
        id=id, name=name, tier=tier, base_hci=base_hci,
        description=desc, domain_weights=weights,
        weakness_text=weakness, victory_text=victory, defeat_text=defeat,
        affiliation=affiliation, tags=tags or [],
    )


# ── Street Level (HCI 15-25) ──

_v("toad", "Toad", "Street Level", 18,
   "Mortimer Toynbee. Agile and irritating. Tests your basic conditioning and consistency.",
   {"conditioning": 0.35, "consistency": 0.30, "mobility": 0.20, "strength": 0.15},
   weakness="Low power ceiling. Overwhelm with any sustained effort.",
   victory="Toad has been subdued. Basic threat neutralized.",
   defeat="Toad evaded your grasp. Your conditioning needs work.",
   affiliation="Brotherhood of Mutants", tags=["agility", "endurance"])

_v("pyro", "Pyro", "Street Level", 20,
   "St. John Allerdyce. Flames test your endurance and recovery under pressure.",
   {"conditioning": 0.35, "recovery": 0.25, "consistency": 0.25, "nutrition_adherence": 0.15},
   weakness="Cannot generate his own flame. Remove his fuel source through sustained conditioning.",
   victory="Pyro's flames are extinguished. You burned brighter.",
   defeat="Pyro's inferno consumed your stamina. Build your endurance.",
   affiliation="Brotherhood of Mutants", tags=["fire", "endurance"])

_v("blob", "Blob", "Street Level", 22,
   "Fred Dukes. Immovable mass. Only raw strength and nutrition discipline will budge him.",
   {"strength": 0.40, "nutrition_adherence": 0.25, "physique": 0.20, "conditioning": 0.15},
   weakness="Immobile. Cannot pursue. Outwork him by maintaining your own movement standards.",
   victory="The Blob has been moved. Your strength prevailed.",
   defeat="The Blob stood firm. You need more raw power.",
   affiliation="Brotherhood of Mutants", tags=["strength", "immovable"])

_v("avalanche", "Avalanche", "Street Level", 21,
   "Dominikos Petrakis. Seismic instability. Tests your consistency and foundation.",
   {"consistency": 0.35, "strength": 0.25, "conditioning": 0.25, "recovery": 0.15},
   weakness="His power destabilizes himself. Stay consistent and he crumbles.",
   victory="The ground has settled. Your consistency held firm.",
   defeat="Avalanche shook your foundation. Build more stable habits.",
   affiliation="Brotherhood of Mutants", tags=["seismic", "consistency"])

_v("caliban", "Caliban", "Street Level", 16,
   "Mutant tracker. Tests your commitment to daily check-ins and self-monitoring.",
   {"consistency": 0.40, "nutrition_adherence": 0.25, "recovery": 0.20, "mobility": 0.15},
   weakness="Fragile on his own. Pure adherence to your routine neutralizes him.",
   victory="Caliban has been tracked down. Your discipline exposed him.",
   defeat="Caliban slipped away undetected. Improve your daily tracking.",
   affiliation="Morlocks", tags=["tracking", "discipline"])

# ── Enhanced Human (HCI 25-40) ──

_v("sabretooth", "Sabretooth", "Enhanced Human", 32,
   "Victor Creed. Feral predator. Your recovery and raw strength must match his regeneration.",
   {"recovery": 0.30, "strength": 0.30, "conditioning": 0.20, "consistency": 0.20},
   weakness="Driven by rage, lacks tactical discipline. Outlast him with smart recovery.",
   victory="Sabretooth has been caged. Your recovery matched his healing factor.",
   defeat="Sabretooth's regeneration outlasted you. Prioritize recovery and strength.",
   affiliation="Brotherhood of Mutants", tags=["feral", "regeneration", "x-force"])

_v("mystique", "Mystique", "Enhanced Human", 30,
   "Raven Darkhölme. Master shapeshifter. Tests your consistency—she exploits any pattern gap.",
   {"consistency": 0.35, "conditioning": 0.25, "mobility": 0.20, "nutrition_adherence": 0.20},
   weakness="She can copy form but not substance. Consistent execution reveals the fraud.",
   victory="Mystique's disguise is shattered. Your consistency was her undoing.",
   defeat="Mystique exploited gaps in your routine. Tighten your discipline.",
   affiliation="Brotherhood of Mutants", tags=["shapeshifter", "deception"])

_v("lady_deathstrike", "Lady Deathstrike", "Enhanced Human", 33,
   "Yuriko Oyama. Adamantium claws and cybernetic precision. Demands strength and mobility.",
   {"strength": 0.30, "mobility": 0.25, "conditioning": 0.25, "recovery": 0.20},
   weakness="Relies on rigid programming. Adaptive training confounds her patterns.",
   victory="Lady Deathstrike's adamantium has been dulled. Your mobility was the edge.",
   defeat="Deathstrike's precision exposed your mobility weaknesses.",
   affiliation="Reavers", tags=["cybernetic", "adamantium"])

_v("silver_samurai", "Silver Samurai", "Enhanced Human", 35,
   "Keniuchio Harada. Tachyon blade demands strength and disciplined technique.",
   {"strength": 0.35, "consistency": 0.25, "conditioning": 0.20, "physique": 0.20},
   weakness="Honorable. He respects strength and discipline. Match him there.",
   victory="The Silver Samurai concedes. Your strength and discipline earned his respect.",
   defeat="The Silver Samurai's blade found weakness in your technique.",
   affiliation="Clan Yashida", tags=["martial", "honor"])

_v("omega_red", "Omega Red", "Enhanced Human", 38,
   "Arkady Rossovich. Carbonadium coils drain life force. Recovery and conditioning are critical.",
   {"recovery": 0.35, "conditioning": 0.30, "strength": 0.20, "nutrition_adherence": 0.15},
   weakness="His death factor drains himself too. Superior recovery outlasts him.",
   victory="Omega Red's coils retracted. Your recovery overpowered his drain.",
   defeat="Omega Red's death factor depleted you. Recover harder.",
   affiliation="Soviet Super-Soldiers", tags=["death_factor", "carbonadium"])

_v("arcade", "Arcade", "Enhanced Human", 28,
   "Murderworld operator. Tests your adaptability across all domains equally.",
   {"conditioning": 0.20, "strength": 0.20, "consistency": 0.20, "recovery": 0.20, "mobility": 0.20},
   weakness="A showman, not a fighter. Balanced effort collapses his Murderworld.",
   victory="Murderworld has been demolished. Your well-rounded fitness was the key.",
   defeat="Arcade's traps caught you off guard. Balance your training.",
   affiliation="Independent", tags=["traps", "balance"])

# ── Mutant Operative (HCI 40-55) ──

_v("juggernaut", "Juggernaut", "Mutant Operative", 48,
   "Cain Marko. Unstoppable once in motion. Only supreme strength and conditioning can redirect him.",
   {"strength": 0.40, "conditioning": 0.25, "physique": 0.20, "recovery": 0.15},
   weakness="Cannot change direction quickly. Use conditioning to outmaneuver his momentum.",
   victory="The Juggernaut has been stopped. An impossible feat of raw power.",
   defeat="The Juggernaut was truly unstoppable. You need overwhelming strength.",
   affiliation="Independent", tags=["unstoppable", "cyttorak"])

_v("mister_sinister", "Mister Sinister", "Mutant Operative", 52,
   "Nathaniel Essex. Geneticist mastermind. Tests nutrition discipline and recovery at an elite level.",
   {"nutrition_adherence": 0.30, "recovery": 0.25, "consistency": 0.25, "conditioning": 0.20},
   weakness="Obsessive about perfection. Match his precision in nutrition and recovery.",
   victory="Sinister's experiments have been shut down. Your genetic discipline was superior.",
   defeat="Mister Sinister catalogued your weaknesses. He will return better informed.",
   affiliation="Marauders", tags=["genetics", "precision"])

_v("stryfe", "Stryfe", "Mutant Operative", 50,
   "Cable's clone. Telekinetic tyrant. Demands strength, conditioning, and unrelenting consistency.",
   {"strength": 0.30, "conditioning": 0.25, "consistency": 0.30, "recovery": 0.15},
   weakness="Arrogance is his flaw. Relentless consistency wears down his focus.",
   victory="Stryfe's timeline has collapsed. Your consistency broke his concentration.",
   defeat="Stryfe's telekinetic assault scattered your routine. Regroup and rebuild.",
   affiliation="Mutant Liberation Front", tags=["telekinetic", "x-force"])

_v("exodus", "Exodus", "Mutant Operative", 53,
   "Bennet du Paris. Apocalypse's zealot. Tests the devotion of your recovery and conditioning.",
   {"recovery": 0.30, "conditioning": 0.30, "strength": 0.20, "consistency": 0.20},
   weakness="Fanaticism blinds him. Steady, disciplined effort outpaces zealotry.",
   victory="Exodus has been grounded. Your disciplined conditioning defeated his fanaticism.",
   defeat="Exodus ascended while you faltered. Match his intensity.",
   affiliation="Acolytes", tags=["zealot", "telekinetic"])

_v("spiral", "Spiral", "Mutant Operative", 45,
   "Rita Wayword. Six-armed sorceress. Demands mobility, conditioning, and nutritional precision.",
   {"mobility": 0.30, "conditioning": 0.25, "nutrition_adherence": 0.25, "consistency": 0.20},
   weakness="She manipulates time, but she can't manipulate your discipline.",
   victory="Spiral's dance has ended. Your mobility matched her six arms.",
   defeat="Spiral outmaneuvered you across dimensions. Improve your mobility.",
   affiliation="Mojoverse", tags=["sorcery", "multiarm"])

_v("sauron", "Sauron", "Mutant Operative", 42,
   "Karl Lykos. Energy vampire pteranodon. Drains your conditioning if recovery isn't maintained.",
   {"conditioning": 0.35, "recovery": 0.30, "strength": 0.20, "nutrition_adherence": 0.15},
   weakness="Energy dependent. Strong recovery and fuel management starve his power.",
   victory="Sauron has been grounded. Proper recovery denied him energy.",
   defeat="Sauron drained your reserves. Improve conditioning and nutrition.",
   affiliation="Savage Land Mutates", tags=["energy_drain", "savage_land"])

# ── Alpha Mutant (HCI 55-75) ──

_v("magneto", "Magneto", "Alpha Mutant", 68,
   "Erik Lehnsherr. Master of Magnetism. Only balanced, iron-willed discipline across all domains can resist his pull.",
   {"strength": 0.25, "conditioning": 0.20, "consistency": 0.20, "physique": 0.15, "recovery": 0.10, "nutrition_adherence": 0.05, "mobility": 0.05},
   weakness="His conviction is absolute. Match it with your own across every domain.",
   victory="Magneto acknowledges your strength. The Brotherhood stands down... for now.",
   defeat="Magneto's magnetic field overwhelmed you. You need balance across all domains.",
   affiliation="Brotherhood of Mutants", tags=["magnetism", "omega_candidate"])

_v("emma_frost", "Emma Frost (Dark)", "Alpha Mutant", 60,
   "White Queen. Telepathic diamond form. Tests mental discipline—consistency and recovery.",
   {"consistency": 0.30, "recovery": 0.25, "nutrition_adherence": 0.20, "physique": 0.15, "conditioning": 0.10},
   weakness="Diamond form sacrifices telepathy. Force her into physical engagement through strength.",
   victory="The White Queen's diamond form has cracked. Your mental discipline held.",
   defeat="Emma Frost shattered your mental defenses. Strengthen your consistency.",
   affiliation="Hellfire Club", tags=["telepath", "diamond"])

_v("shadow_king", "Shadow King", "Alpha Mutant", 63,
   "Amahl Farouk. Psychic parasite on the astral plane. Recovery and mental consistency are your shields.",
   {"recovery": 0.35, "consistency": 0.30, "conditioning": 0.20, "nutrition_adherence": 0.15},
   weakness="He feeds on weakness. Perfect recovery and consistency starve him.",
   victory="The Shadow King retreats to the astral plane. Your mind held firm.",
   defeat="The Shadow King found cracks in your resolve. Recover and rebuild your consistency.",
   affiliation="Astral Plane", tags=["psychic", "parasite"])

_v("vulcan", "Vulcan", "Alpha Mutant", 70,
   "Gabriel Summers. Third Summers brother. Energy manipulation at cosmic scale demands everything.",
   {"conditioning": 0.30, "strength": 0.25, "physique": 0.20, "recovery": 0.15, "consistency": 0.10},
   weakness="Emotionally unstable. Steady, relentless advancement overwhelms his volatile power.",
   victory="Vulcan's energy has been redirected. Raw discipline overcame his raw power.",
   defeat="Vulcan's energy output exceeded your capacity. Train harder across all fronts.",
   affiliation="Shi'ar Empire", tags=["energy", "summers"])

_v("selene", "Selene", "Alpha Mutant", 65,
   "The Black Queen. Psychic vampire queen of the Hellfire Club. Drains your life force through neglected recovery.",
   {"recovery": 0.30, "nutrition_adherence": 0.25, "consistency": 0.25, "mobility": 0.20},
   weakness="She feeds on death energy. Vibrant health and perfect nutrition deny her sustenance.",
   victory="Selene's dark sorcery fails against your vitality. The Black Queen is denied.",
   defeat="Selene drained your essence. Neglected recovery left you vulnerable.",
   affiliation="Hellfire Club", tags=["sorcery", "vampire"])

_v("bastion", "Bastion", "Alpha Mutant", 62,
   "Master Mold reborn. Sentinel supreme. Tests systematic consistency and strength.",
   {"consistency": 0.30, "strength": 0.25, "conditioning": 0.25, "physique": 0.20},
   weakness="Programmed patterns. Unpredictable intensity and varied training confound his protocols.",
   victory="Bastion's Sentinel network has been dismantled. Your consistency outperformed his programming.",
   defeat="Bastion adapted to your patterns. Vary your approach and strengthen your consistency.",
   affiliation="Operation: Zero Tolerance", tags=["sentinel", "anti_mutant"])

# ── Omega Threat (HCI 75-88) ──

_v("apocalypse", "Apocalypse", "Omega Threat", 82,
   "En Sabah Nur. Survival of the fittest incarnate. Tests ALL domains—he demands evolutionary excellence.",
   {"strength": 0.20, "conditioning": 0.20, "physique": 0.15, "recovery": 0.15, "consistency": 0.15, "nutrition_adherence": 0.10, "mobility": 0.05},
   weakness="He respects only the strong. Excel in every domain to prove fitness to survive.",
   victory="Apocalypse acknowledges you as fit. The strongest HAVE survived.",
   defeat="Apocalypse deems you unworthy. Only the fit survive his judgment.",
   affiliation="Clan Akkaba", tags=["celestial_tech", "eternal", "survival"])

_v("dark_phoenix", "Dark Phoenix", "Omega Threat", 85,
   "The Phoenix Force corrupted. Cosmic annihilation. Demands peak conditioning, recovery, and total dedication.",
   {"conditioning": 0.25, "recovery": 0.25, "consistency": 0.20, "strength": 0.15, "physique": 0.10, "nutrition_adherence": 0.05},
   weakness="Love and discipline anchor the Phoenix. Perfect consistency and recovery ground the fire.",
   victory="The Phoenix Force has been contained. Your discipline anchored the cosmic flame.",
   defeat="The Dark Phoenix consumed your efforts. You were not yet ready for cosmic-tier threats.",
   affiliation="Cosmic", tags=["phoenix_force", "cosmic", "omega"])

_v("onslaught", "Onslaught", "Omega Threat", 83,
   "Psionic entity born of Xavier and Magneto. A test of absolute willpower across every domain.",
   {"strength": 0.20, "conditioning": 0.20, "recovery": 0.20, "consistency": 0.20, "physique": 0.10, "nutrition_adherence": 0.10},
   weakness="Born from conflict between two minds. Unity of purpose across all domains defeats him.",
   victory="Onslaught has been dispersed. Total fitness unity dissolved the psionic entity.",
   defeat="Onslaught's psionic assault shattered your discipline. You need unity across all domains.",
   affiliation="Psionic Entity", tags=["psionic", "omega"])

_v("legion", "Legion (Dark)", "Omega Threat", 78,
   "David Haller. Each personality a different power. Your consistency must overcome his chaos.",
   {"consistency": 0.30, "recovery": 0.25, "conditioning": 0.20, "strength": 0.15, "mobility": 0.10},
   weakness="His personalities war with each other. Consistent, focused discipline holds firm.",
   victory="Legion's personalities converge. Your steady consistency silenced the chaos.",
   defeat="Legion's fractured mind overwhelmed your focus. Rebuild your consistency.",
   affiliation="Independent", tags=["reality_warp", "multiple_personalities"])

_v("cassandra_nova", "Cassandra Nova", "Omega Threat", 80,
   "Xavier's parasitic twin. Genocidal psychic power. Tests tactical precision across all domains.",
   {"consistency": 0.25, "recovery": 0.20, "conditioning": 0.20, "strength": 0.15, "nutrition_adherence": 0.10, "physique": 0.10},
   weakness="She exists through hate. Discipline and self-care are antithetical to her nature.",
   victory="Cassandra Nova has been contained. Your disciplined self-care was her kryptonite.",
   defeat="Cassandra Nova exploited neglected domains. Leave no weakness exposed.",
   affiliation="Independent", tags=["psychic", "genocidal"])

# ── Cosmic Entity (HCI 88-95) ──

_v("phoenix_force", "The Phoenix Force", "Cosmic Entity", 92,
   "Primal cosmic force of death and rebirth. A trial requiring perfection across every metric.",
   {d: round(1.0 / len(ALL_DOMAINS), 3) for d in ALL_DOMAINS},
   weakness="It seeks a worthy host. Prove worthiness through absolute balance.",
   victory="The Phoenix Force has chosen you. You burn with cosmic purpose.",
   defeat="The Phoenix Force found you unworthy. Approach perfection across all domains.",
   affiliation="Cosmic", tags=["cosmic", "creation_destruction"])

_v("proteus", "Proteus", "Cosmic Entity", 88,
   "Kevin MacTaggert. Reality warper. Your physical foundation must be unshakeable.",
   {"strength": 0.25, "physique": 0.25, "conditioning": 0.20, "recovery": 0.15, "consistency": 0.15},
   weakness="Burns through physical hosts. Superior physique and strength endure his reality warps.",
   victory="Proteus's reality distortions collapse against your physical foundation.",
   defeat="Proteus warped your reality. Your physical base was insufficient.",
   affiliation="Independent", tags=["reality_warp", "energy"])

# ── Beyond Omega (HCI 95+) ──

_v("the_beyonder", "The Beyonder", "Beyond Omega", 98,
   "Entity from beyond the multiverse. Testing him requires transcendent fitness across every dimension.",
   {d: round(1.0 / len(ALL_DOMAINS), 3) for d in ALL_DOMAINS},
   weakness="Curiosity about human limitation. Demonstrate there ARE no limits.",
   victory="The Beyonder is... impressed. You have exceeded the parameters of his experiment.",
   defeat="The Beyonder finds your universe... lacking. Perfect every domain to challenge him.",
   affiliation="Beyond", tags=["omnipotent", "multiverse"])

_v("mad_jim_jaspers", "Mad Jim Jaspers", "Beyond Omega", 96,
   "Reality warper supreme. The Fury couldn't stop him. Only Omega-level discipline can match his madness.",
   {"consistency": 0.20, "strength": 0.20, "conditioning": 0.20, "recovery": 0.15, "physique": 0.15, "nutrition_adherence": 0.05, "mobility": 0.05},
   weakness="His reality warps are fueled by chaos. Perfect order and discipline counter his madness.",
   victory="Jim Jaspers' reality warps stabilize. Your discipline imposed order on his chaos.",
   defeat="Reality crumbled under Jaspers' influence. Your discipline was insufficient.",
   affiliation="Earth-238", tags=["reality_warp", "omega_plus"])


def get_villain(villain_id: str) -> Villain | None:
    return VILLAIN_CATALOG.get(villain_id)


def get_villains_for_tier(tier: str) -> list[Villain]:
    return [v for v in VILLAIN_CATALOG.values() if v.tier == tier]


def get_villains_in_hci_range(hci_low: float, hci_high: float) -> list[Villain]:
    return [v for v in VILLAIN_CATALOG.values() if hci_low <= v.base_hci <= hci_high]


def get_tier_for_hci(hci: float) -> str:
    for tier in reversed(HERO_TIERS):
        if hci >= tier["min_hci"]:
            return tier["name"]
    return "Street Level"
