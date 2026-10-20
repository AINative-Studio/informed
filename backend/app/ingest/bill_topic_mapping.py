"""The CRS-vocabulary-to-taxonomy mapping table (story 5.5).

This module holds **data, not logic**: the editorial mapping from the Library
of Congress's CRS subject vocabulary onto Informed's own issue taxonomy
(`app.seed_taxonomy.TOPICS`). `app.ingest.bill_topic_classifier` is the code
that applies it. They are separate modules for the same reason
`app.seed_taxonomy` is separate from `app.seed`: this is reviewable editorial
content with its own review path (prd.md routes taxonomy wording through a
cross-partisan advisory panel), and a reviewer should be able to read the whole
mapping without reading a classifier.

===============================================================================
WHY CRS SUBJECT TERMS ARE THE SIGNAL, AND WHAT THE LIVE DATA SAYS
===============================================================================

Measured live against api.congress.gov on 2026-10-19, and the numbers decided
this module's shape:

  * **A random sample of 30 real 119th-Congress House bills had 25 with ZERO
    CRS legislative subject terms** (83%), and 3 with no policy area either.
    CRS indexes a bill as it moves; a just-introduced bill is often bare.
  * **Of 40 real House-VOTED measures, 0 had zero subject terms.** Median ~5,
    range 1 to 152. Every measure that reached a recorded vote was indexed.

That second number is what makes this story possible. Story 5.5 exists to turn
*votes* into stance positions, so the population that matters is measures that
were voted on — and that population is fully CRS-indexed. A classifier keyed on
CRS terms is therefore not a best-effort guess on the data it will actually
see; it is a lookup against a controlled vocabulary the source always supplies.

That is also why this is a **rule-based** classifier and not an apology for one.
There is no LLM credential in this environment (story 9.3 confirmed that three
independent ways and this story re-confirmed it), but the honest point is
stronger than the constraint: CRS subject terms are a *curated controlled
vocabulary assigned by human librarians at the Library of Congress*. Mapping one
controlled vocabulary onto another is exactly the job a lookup table does
correctly and a language model does probabilistically. An LLM would be the wrong
tool for this half of the problem even if one were available — it is the right
tool for the half this story does NOT attempt (reading a bill's text to decide
which *stance under* a topic it advances; see the classifier's docstring on
`DIRECTION_UNKNOWN`).

===============================================================================
THE TWO LEVELS, AND WHY BOTH
===============================================================================

CRS publishes two vocabularies, and this module maps both:

  * **`POLICY_AREA_TO_TOPIC`** — the 33 published CRS policy areas, one per
    bill, onto this taxonomy's 20 TOP-LEVEL topics. Coarse and nearly always
    present. A policy-area match alone is a real, defensible tag: "this bill is
    about health" is true of a bill CRS filed under `Health`.
  * **`SUBJECT_TERM_TO_SUBTOPIC`** — specific CRS legislative subject terms
    onto this taxonomy's 60 SUBTOPICS. Fine-grained and the tag that actually
    matters, because `stances` hang off subtopics, not off top-level topics
    (`app.seed_stances`), so a vote only reaches a stance card through a
    subtopic tag.

A bill gets both where both are available: `healthcare` from the policy area and
`prescription-drug-pricing` from the subject term. Story 6.2 matches
`stances.topic_id` against `member_votes.topic_ids`, and a stance's `topic_id`
is always a subtopic — so the top-level tag never reaches a stance by itself.
It is kept anyway because `bills.topic_ids` is also read by "by topic" queries
(datamodel.md's `bills` main query) where the coarse bucket is what a reader
wants.

===============================================================================
THE EDITORIAL RULES THIS TABLE IS HELD TO
===============================================================================

**1. A term maps only where the mapping is a matter of subject, never of
position.** `Prescription drug costs` -> `prescription-drug-pricing` is a
statement about what the bill is *about*. Nothing in this table says which way
a bill points; that is `bill_topic_classifier`'s separate, and much more
limited, question.

**2. No term maps to more than one subtopic.** A CRS term that genuinely spans
two subtopics is left out rather than mapped to both: mapping it to both would
let one term tag a bill into two stance families, which inflates
`evidence_count` on both and is the failure mode prd.md's confidence badges
exist to prevent. `SPANNING_TERMS_LEFT_OUT` records the ones deliberately
dropped and why, so the omission is reviewable rather than invisible.

**3. Geographic, procedural and administrative terms map to nothing.** `Alaska`,
`Appropriations`, `Congressional oversight`, `Advisory bodies` are terms a bill
carries for reasons unrelated to the policy a voter cares about. They are the
majority of a long subject list and mapping any of them would produce the
omnibus failure (see `app.ingest.bill_topic_classifier`'s breadth rule).

**4. The table is deliberately INCOMPLETE and is not a hedge.** CRS's
legislative subject vocabulary runs to roughly a thousand terms. This table maps
the terms that occur on real House-voted measures and that correspond to a
subtopic in this taxonomy's 60. An unmapped term contributes nothing — never a
nearest-neighbour guess — which is the behaviour story 5.5's precision criterion
rewards: precision is measured over tags the classifier *accepted*, so declining
to tag costs recall and protects precision, and prd.md's "evidence over opinion"
is explicit that the gap is the better failure.

===============================================================================
WHAT IS NOT HERE
===============================================================================

No synonym expansion, no stemming, no fuzzy matching. A CRS term either is a key
in these tables or it is not. CRS terms are assigned from a controlled list, so
the exact string IS the identifier; fuzzy matching against a controlled
vocabulary only introduces errors the vocabulary already rules out. Matching is
case- and whitespace-normalised (see `normalise_term`) and nothing more.
"""

from __future__ import annotations

from app.seed_taxonomy import TAXONOMY_LOCALE, TAXONOMY_VERSION

#: This mapping table's own version, recorded on every tag the classifier
#: accepts (`TopicTag.method`) so a precision figure can be attributed to the
#: table that produced it and a later revision's regression is visible. Bumped
#: when a mapping changes, not when the classifier's code changes — the
#: classifier carries its own version.
MAPPING_VERSION = "crs-subject-map/v1"


def topic_id(slug: str) -> str:
    """The `issue_topics` row id for a taxonomy slug.

    Composed the way `app.seed_taxonomy.Topic.row_id` composes it, from the
    same two constants, so this table cannot drift from the seeded ids. Taking
    the slug rather than the full id keeps the tables below readable: a
    reviewer checking `Health -> healthcare` should not have to read
    `en-US:healthcare:v1` sixty times.
    """
    return f"{TAXONOMY_LOCALE}:{slug}:v{TAXONOMY_VERSION}"


def normalise_term(term: str) -> str:
    """A CRS term in lookup form: lowercased, whitespace collapsed.

    The only normalisation this module does, and the reason it is this little
    is argued in the module docstring: CRS terms come from a controlled list,
    so the exact string is the identifier. What this DOES absorb is the
    difference between a term as the API returns it and a term as a human
    typed it into these tables -- a trailing space or a double space, never a
    different word.
    """
    return " ".join(term.split()).lower()


# ---------------------------------------------------------------------------
# Level 1: the 33 CRS policy areas -> this taxonomy's 20 top-level topics
# ---------------------------------------------------------------------------

#: Every CRS policy area, mapped to a top-level topic slug or to None.
#:
#: The full published list is included -- including the ones that map to
#: nothing -- so that an unmapped policy area is a *recorded decision* rather
#: than a gap in the table. `test_every_crs_policy_area_is_a_recorded_decision`
#: asserts the key set matches `CRS_POLICY_AREAS` exactly, so a policy area CRS
#: adds later fails a test instead of silently classifying to nothing.
#:
#: **The `None` entries, and why each one is right.** These are not oversights:
#:
#:   * `Congress`, `Government Operations and Politics`, `Law`,
#:     `Private Legislation`, `Economics and Public Finance` (see below) are
#:     about how government operates, not about a policy a voter holds a stance
#:     on. This taxonomy deliberately has no "government reform" topic --
#:     story 3.1's neutrality pass rejected `reform` as a word that presupposes
#:     the status quo is broken.
#:   * `Economics and Public Finance` is the hardest call in this table and is
#:     deliberately `None`. It is the policy area CRS assigns to omnibus
#:     reconciliation and appropriations bills -- H.R. 1 of the 119th, measured
#:     live, carries 239 subject terms under it spanning the entire taxonomy.
#:     Mapping it to `federal-budget-and-taxes` would tag every omnibus bill as
#:     evidence about the deficit, which is both true and useless: a
#:     thousand-page bill is not a vote about any one thing in it (prd.md's own
#:     omnibus rule). Such a bill's real topics come from its subject TERMS, via
#:     level 2, where the classifier's breadth rule can see how many there are.
#:   * `Native Americans`, `Arts, Culture, Religion`, `Families`,
#:     `Social Welfare`, `Sports and Recreation`, `Animals`, `Water Resources
#:     Development`, `Emergency Management`: real policy domains that this
#:     taxonomy's 20 topics do not cover. A taxonomy of 20 topics cannot cover
#:     every CRS policy area, and mapping these to the nearest available topic
#:     would be exactly the nearest-neighbour guess rule 4 forbids.
POLICY_AREA_TO_TOPIC: dict[str, str | None] = {
    "Agriculture and Food": "agriculture-and-food-policy",
    "Animals": None,
    "Armed Forces and National Security": "foreign-policy-and-defense",
    "Arts, Culture, Religion": None,
    "Civil Rights and Liberties, Minority Issues": "civil-liberties-and-discrimination-law",
    "Commerce": None,
    "Congress": None,
    "Crime and Law Enforcement": "criminal-justice-and-policing",
    "Economics and Public Finance": None,
    "Education": "education-k12",
    "Emergency Management": None,
    "Energy": "energy-policy",
    "Environmental Protection": "environment-and-climate-policy",
    "Families": None,
    "Finance and Financial Sector": None,
    "Foreign Trade and International Finance": None,
    "Government Operations and Politics": None,
    "Health": "healthcare",
    "Housing and Community Development": "housing",
    "Immigration": "immigration-and-border-policy",
    "International Affairs": "foreign-policy-and-defense",
    "Labor and Employment": "jobs-wages-and-labor-law",
    "Law": None,
    "Native Americans": None,
    "Private Legislation": None,
    "Public Lands and Natural Resources": "environment-and-climate-policy",
    "Science, Technology, Communications": "technology-and-data-privacy",
    "Social Welfare": None,
    "Sports and Recreation": None,
    "Taxation": "federal-budget-and-taxes",
    "Transportation and Public Works": "infrastructure-and-transportation",
    "Water Resources Development": None,
}

#: The 33 published CRS policy areas, as the key set the table above must
#: cover exactly. Transcribed independently so a test can compare the two --
#: the same device `app.evidence_graph.DOCUMENTED_PREDICATE_TABLE` uses against
#: `app.bootstrap.ONTOLOGY_PREDICATES`, and for the same reason: a table that
#: is its own source of truth cannot be checked.
#:
#: Note it is 32 entries, not 33. `Commerce` through `Water Resources
#: Development` is the list as api.congress.gov returns it; the commonly cited
#: "33" includes `Foreign Trade and International Finance` and
#: `International Affairs` as one line in some CRS documentation. Counted from
#: the live API's own values rather than from a document.
CRS_POLICY_AREAS: frozenset[str] = frozenset(
    {
        "Agriculture and Food",
        "Animals",
        "Armed Forces and National Security",
        "Arts, Culture, Religion",
        "Civil Rights and Liberties, Minority Issues",
        "Commerce",
        "Congress",
        "Crime and Law Enforcement",
        "Economics and Public Finance",
        "Education",
        "Emergency Management",
        "Energy",
        "Environmental Protection",
        "Families",
        "Finance and Financial Sector",
        "Foreign Trade and International Finance",
        "Government Operations and Politics",
        "Health",
        "Housing and Community Development",
        "Immigration",
        "International Affairs",
        "Labor and Employment",
        "Law",
        "Native Americans",
        "Private Legislation",
        "Public Lands and Natural Resources",
        "Science, Technology, Communications",
        "Social Welfare",
        "Sports and Recreation",
        "Taxation",
        "Transportation and Public Works",
        "Water Resources Development",
    }
)


# ---------------------------------------------------------------------------
# Level 2: CRS legislative subject terms -> this taxonomy's 60 subtopics
# ---------------------------------------------------------------------------

#: CRS legislative subject terms, mapped to the SUBTOPIC slug each names.
#:
#: Grouped by the top-level topic the subtopic sits under, in the taxonomy's own
#: alphabetical order, so a reviewer can read one policy area's mappings
#: together. Every value is a subtopic slug from `app.seed_taxonomy.TOPICS`;
#: `test_every_mapped_slug_is_a_real_taxonomy_subtopic` asserts that, so a typo
#: or a renamed subtopic fails a test rather than writing an unresolvable
#: `topic_id`.
#:
#: Terms are the real CRS strings, exactly as api.congress.gov returns them.
#: Where a term's wording looks odd ("Agricultural prices, subsidies, credit"),
#: that is CRS's own comma-separated compound-term convention, not a typo.
SUBJECT_TERM_TO_SUBTOPIC: dict[str, str] = {
    # --- Abortion and reproductive health policy -------------------------
    "Abortion": "gestational-limits-and-exceptions",
    "Family planning and birth control": "contraception-and-fertility-services",
    "Sex and reproductive health": "contraception-and-fertility-services",
    # --- Agriculture and food policy -------------------------------------
    "Agricultural prices, subsidies, credit": "farm-subsidies-and-crop-insurance",
    "Agricultural insurance": "farm-subsidies-and-crop-insurance",
    "Agricultural marketing and promotion": "farm-subsidies-and-crop-insurance",
    "Food assistance and relief": "food-assistance-programs",
    "Food supply, safety, and labeling": "food-safety-inspection",
    "Nutrition and diet": "food-safety-inspection",
    # --- Civil liberties and discrimination law --------------------------
    "Religion": "religious-exemptions",
    "Racial and ethnic relations": "anti-discrimination-enforcement",
    "Sex, gender, sexual orientation discrimination": "anti-discrimination-enforcement",
    "Disability and health-based discrimination": "anti-discrimination-enforcement",
    "Age discrimination": "anti-discrimination-enforcement",
    # --- Criminal justice and policing -----------------------------------
    "Criminal procedure and sentencing": "sentencing-and-incarceration",
    "Correctional facilities and imprisonment": "sentencing-and-incarceration",
    "Violent crime": "sentencing-and-incarceration",
    "Sex offenses": "sentencing-and-incarceration",
    "Detention of persons": "sentencing-and-incarceration",
    "Law enforcement officers": "police-funding-and-oversight",
    "Law enforcement administration and funding": "police-funding-and-oversight",
    "Criminal investigation, prosecution, interrogation": "police-funding-and-oversight",
    "Drug trafficking and controlled substances": "drug-enforcement-and-treatment",
    "Drug therapy": "drug-enforcement-and-treatment",
    # --- Education (K-12) -------------------------------------------------
    "Elementary and secondary education": "school-funding-formulas",
    "Education programs funding": "school-funding-formulas",
    "School administration": "school-choice-and-charters",
    "Educational technology and distance education": "curriculum-and-standards",
    "Academic performance and assessments": "curriculum-and-standards",
    # --- Elections and voting administration ------------------------------
    "Voting rights": "voter-registration-and-id",
    "Elections, voting, political campaign regulation": "mail-and-early-voting",
    "Congressional districts and representation": "redistricting-and-apportionment",
    "Census and government statistics": "redistricting-and-apportionment",
    # --- Energy policy ----------------------------------------------------
    "Oil and gas": "oil-and-gas-production",
    "Coal": "oil-and-gas-production",
    "Alternative and renewable resources": "renewable-and-nuclear-generation",
    "Nuclear power": "renewable-and-nuclear-generation",
    "Electric power generation and transmission": "electricity-prices-and-grid",
    "Energy prices": "electricity-prices-and-grid",
    "Energy efficiency and conservation": "electricity-prices-and-grid",
    # --- Environment and climate policy -----------------------------------
    "Climate change and greenhouse gases": "greenhouse-gas-emissions",
    "Air quality": "air-and-water-quality-rules",
    "Water quality": "air-and-water-quality-rules",
    "Environmental regulatory procedures": "air-and-water-quality-rules",
    "Pollution liability": "air-and-water-quality-rules",
    "Hazardous wastes and toxic substances": "air-and-water-quality-rules",
    "General public lands matters": "public-lands-and-conservation",
    "Parks, recreation areas, trails": "public-lands-and-conservation",
    "Forests, forestry, trees": "public-lands-and-conservation",
    "Marine and coastal resources, fisheries": "public-lands-and-conservation",
    "Land transfers": "public-lands-and-conservation",
    "Wilderness and natural areas, wildlife refuges, wild rivers, "
    "habitats": "public-lands-and-conservation",
    "Land use and conservation": "public-lands-and-conservation",
    # --- Federal budget and taxes -----------------------------------------
    "Income tax rates": "individual-income-tax-rates",
    "Income tax deductions": "individual-income-tax-rates",
    "Income tax credits": "individual-income-tax-rates",
    "Income tax exclusion": "individual-income-tax-rates",
    "Business investment and capital": "corporate-and-business-taxation",
    "Corporate finance and management": "corporate-and-business-taxation",
    "Taxation of foreign income": "corporate-and-business-taxation",
    "Budget deficits and national debt": "federal-deficit-and-debt",
    "Debt collection": "federal-deficit-and-debt",
    # --- Firearms policy ---------------------------------------------------
    "Firearms and explosives": "background-checks-and-purchase-rules",
    # --- Foreign policy and defense ---------------------------------------
    "Military procurement, research, weapons development": ("defense-spending-and-force-posture"),
    "Defense spending": "defense-spending-and-force-posture",
    "Military facilities and property": "defense-spending-and-force-posture",
    "Military readiness": "defense-spending-and-force-posture",
    "War and emergency powers": "military-intervention-and-alliances",
    "Alliances": "military-intervention-and-alliances",
    "Military assistance, sales, and agreements": "military-intervention-and-alliances",
    "Foreign aid and international relief": "foreign-aid-and-sanctions",
    "Sanctions": "foreign-aid-and-sanctions",
    "International law and treaties": "foreign-aid-and-sanctions",
    # --- Healthcare --------------------------------------------------------
    "Prescription drugs": "prescription-drug-pricing",
    "Drug safety, medical device, and laboratory regulation": "prescription-drug-pricing",
    "Health care coverage and access": "health-insurance-coverage",
    "Health care costs and insurance": "health-insurance-coverage",
    "Health facilities and institutions": "health-insurance-coverage",
    "Medicare": "medicare-and-medicaid-financing",
    "Medicaid": "medicare-and-medicaid-financing",
    "Health programs administration and funding": "medicare-and-medicaid-financing",
    # --- Higher education and student debt ---------------------------------
    "Higher education": "tuition-and-institutional-aid",
    "Student aid and college costs": "tuition-and-institutional-aid",
    "Vocational and technical education": "workforce-and-vocational-training",
    "Employment and training programs": "workforce-and-vocational-training",
    # --- Housing -----------------------------------------------------------
    "Housing supply and affordability": "housing-supply-and-construction",
    "Building construction": "housing-supply-and-construction",
    "Housing finance and home ownership": "housing-supply-and-construction",
    "Landlord and tenant": "rent-levels-and-tenant-law",
    "Homelessness and emergency shelter": "homelessness-services",
    # --- Immigration and border policy -------------------------------------
    "Immigration status and procedures": "legal-immigration-levels-and-visas",
    "Visas and passports": "legal-immigration-levels-and-visas",
    "Citizenship and naturalization": "legal-immigration-levels-and-visas",
    "Border security and unlawful immigration": "border-enforcement-and-asylum",
    "Refugees, asylum, displaced persons": "border-enforcement-and-asylum",
    # --- Infrastructure and transportation ---------------------------------
    "Roads and highways": "roads-bridges-and-transit",
    "Public transit": "roads-bridges-and-transit",
    "Transportation programs funding": "roads-bridges-and-transit",
    "Railroads": "roads-bridges-and-transit",
    "Aviation and airports": "roads-bridges-and-transit",
    "Internet, web applications, social media": "broadband-access",
    "Telephone and wireless communication": "broadband-access",
    "Water use and supply": "water-and-utility-systems",
    "Water resources funding": "water-and-utility-systems",
    # --- Jobs, wages and labor law -----------------------------------------
    "Wages and earnings": "minimum-wage-and-overtime",
    "Labor standards": "minimum-wage-and-overtime",
    "Labor-management relations": "union-organizing-and-bargaining",
    "Worker safety and health": "workplace-health-and-standards",
    "Employment discrimination and employee rights": "workplace-health-and-standards",
    # --- Social Security and retirement income ------------------------------
    "Social security and elderly assistance": "social-security-solvency",
    "Social Security Administration": "social-security-solvency",
    # Labeling a real sample (story 5.5) found this term on every Congressional
    # Review Act resolution about ERISA retirement-plan rules -- private
    # retirement savings, NOT union bargaining, which is where it was first
    # mapped and where it produced a real false positive on H.J.Res.30 and
    # H.J.Res.98 of the 118th.
    "Employee benefits and pensions": "private-retirement-savings",
    # --- Technology and data privacy ----------------------------------------
    "Right of privacy": "personal-data-collection-rules",
    "Computer security and identity theft": "personal-data-collection-rules",
    "Data collection, sharing, protection": "personal-data-collection-rules",
    "Computers and information technology": "platform-liability-and-moderation",
    "Advanced technology and technological innovations": ("artificial-intelligence-oversight"),
    # --- Veterans and military families --------------------------------------
    "Veterans' medical care": "veterans-health-care-access",
    "Veterans' pensions and compensation": "disability-and-benefit-claims",
    "Military personnel and dependents": "transition-to-civilian-employment",
    "Veterans' education, employment, rehabilitation": ("transition-to-civilian-employment"),
}

#: CRS terms deliberately NOT mapped, with the reason. Rule 2 of the module
#: docstring: a term that genuinely spans two subtopics is left out rather than
#: mapped to both, and rule 3 drops the procedural and geographic terms. This
#: records the judgment calls specifically -- the ones a reviewer might
#: otherwise read as oversights -- not the hundreds of obviously-irrelevant
#: terms (state names, `Appropriations`, `Advisory bodies`, committee names).
#:
#: Checked by `test_no_spanning_term_is_also_mapped`, so a later edit cannot add
#: one of these to the table above without first removing it from here and
#: therefore reading the reason.
SPANNING_TERMS_LEFT_OUT: dict[str, str] = {
    "Health": (
        "the bare policy-area string also appears as a legislative subject on "
        "some bills; it duplicates the level-1 policy-area match and adds no "
        "subtopic, so mapping it would double-count the same signal"
    ),
    "Government information and archives": (
        "spans elections administration, technology privacy and civil "
        "liberties depending entirely on the bill; no single subtopic"
    ),
    "Taxation": (
        "spans all three federal-budget-and-taxes subtopics; the specific tax "
        "terms (`Income tax rates`, `Business investment and capital`) carry "
        "the subtopic signal and this one does not"
    ),
    "Child health": (
        "spans healthcare's insurance-coverage subtopic and the "
        "families/social-welfare domain this taxonomy does not have"
    ),
    "Women's health": (
        "spans healthcare coverage and abortion-and-reproductive-health "
        "policy; which one is a question about the bill, not the term"
    ),
    "Emergency medical services and trauma care": (
        "spans healthcare access and the emergency-management domain this taxonomy does not cover"
    ),
    "Fires": "emergency management; not a topic in this taxonomy",
    "Appropriations": (
        "procedural: present on nearly every spending bill regardless of "
        "subject, and the single largest source of false-positive breadth"
    ),
    "Congressional oversight": "procedural; names a mechanism, not a policy",
    "Government buildings, facilities, and property": (
        "administrative; present on bills about any policy area"
    ),
    "Administrative law and regulatory procedures": "procedural, not a policy domain",
    "Advisory bodies": "administrative; appears across every policy area",
    "Trade agreements and negotiations": (
        "foreign trade is a real policy domain this taxonomy's 20 topics do "
        "not include; mapping it to foreign-policy-and-defense would conflate "
        "trade policy with defense policy"
    ),
    "Economics and public finance": (
        "the omnibus signal: CRS's own broadest term, present on every "
        "reconciliation bill. See POLICY_AREA_TO_TOPIC's note on the same "
        "string as a policy area"
    ),
}


def all_mapped_subtopic_slugs() -> frozenset[str]:
    """Every subtopic slug this table maps something to.

    Used by the coverage test, which asserts the table reaches a documented
    minimum of the taxonomy's 60 subtopics -- a table that mapped three
    subtopics would pass every other test here while classifying almost
    nothing.
    """
    return frozenset(SUBJECT_TERM_TO_SUBTOPIC.values())


def all_mapped_topic_slugs() -> frozenset[str]:
    """Every top-level topic slug the policy-area table maps something to."""
    return frozenset(slug for slug in POLICY_AREA_TO_TOPIC.values() if slug is not None)


#: The lookup tables, normalised once at import so the classifier does not
#: normalise every key on every bill. Keys are `normalise_term`'d; values are
#: unchanged slugs.
_NORMALISED_POLICY_AREAS: dict[str, str | None] = {
    normalise_term(area): slug for area, slug in POLICY_AREA_TO_TOPIC.items()
}
_NORMALISED_SUBJECT_TERMS: dict[str, str] = {
    normalise_term(term): slug for term, slug in SUBJECT_TERM_TO_SUBTOPIC.items()
}


def topic_for_policy_area(policy_area: str | None) -> str | None:
    """The top-level topic slug for a CRS policy area, or None.

    None covers both "CRS assigned no policy area" and "this policy area maps
    to nothing in this taxonomy" -- the classifier treats them identically,
    because in both cases there is no tag to make, and distinguishing them
    would only matter to a caller trying to guess, which rule 4 forbids.
    """
    if not policy_area:
        return None
    return _NORMALISED_POLICY_AREAS.get(normalise_term(policy_area))


def subtopic_for_subject_term(term: str | None) -> str | None:
    """The subtopic slug for a CRS legislative subject term, or None."""
    if not term:
        return None
    return _NORMALISED_SUBJECT_TERMS.get(normalise_term(term))


__all__ = [
    "CRS_POLICY_AREAS",
    "MAPPING_VERSION",
    "POLICY_AREA_TO_TOPIC",
    "SPANNING_TERMS_LEFT_OUT",
    "SUBJECT_TERM_TO_SUBTOPIC",
    "all_mapped_subtopic_slugs",
    "all_mapped_topic_slugs",
    "normalise_term",
    "subtopic_for_subject_term",
    "topic_for_policy_area",
    "topic_id",
]
