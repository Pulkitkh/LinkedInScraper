"""Client candidates, their target roles, C2C keyword exclusions and resume assets.

To add a candidate: add an entry under CANDIDATE_ROLES and CLIENTS with the same key,
and drop their resume (.docx preferred, .pdf works) into clients/.
"""

from __future__ import annotations

import settings

# Copied on every outreach email (in addition to the candidate) and BCC'd respectively.
TEAM_LEAD_EMAIL = settings.env("TEAM_LEAD_EMAIL")
ADMIN_EMAIL = settings.env("ADMIN_EMAIL")

# United States geo filter used in the LinkedIn content search URL.
USA_GEO_URN = "103644278"

# Appended to every search as  -keyword  so LinkedIn drops bench-marketing posts up front.
DEFAULT_EXCLUSION_KEYWORDS = ["hotlist", "fulltime", "available consultants", "Bench"]

# Posts containing any of these are bench marketers selling candidates, not recruiters hiring.
BENCH_MARKETING_PHRASES = [
    "on my bench", "on our bench", "candidate available", "candidates available",
    "marketing my consultant", "marketing our consultant", "available consultants",
    "consultants available", "available consultant", "hotlist", "hot list", "bench sales",
    "bench profiles", "my consultant is available", "looking for c2c requirements",
    "looking for c2c roles", "share your requirements", "send me your requirements",
]

# Job-title domains. A post whose title targets a domain outside the candidate's
# allowed_domains is stored as "mismatched_role" and never emailed.
ROLE_DOMAINS = {
    "java": r"\bjava\b|\bj2ee\b|spring\s*boot",
    "fullstack": r"full[\s-]?stack",
    "backend": r"back[\s-]?end",
    "frontend": r"front[\s-]?end|\breact(?:\.?js)?\b|\bangular(?:\.?js)?\b|\bvue(?:\.?js)?\b|\bui developer\b",
    "python": r"\bpython\b|\bdjango\b",
    "dotnet": r"\.net\b|\bc#",
    "data": r"data scientist|data engineer|machine learning|\bml engineer\b|\bai engineer\b",
    "devops": r"devops|\bsre\b|site reliability|cloud engineer|platform engineer",
    "qa": r"\bqa\b|quality assurance|\bsdet\b|\btester\b|test automation|automation tester",
    "salesforce": r"salesforce|\bsfdc\b",
    "sap": r"\bsap\b",
    "erp": r"oracle\s+(?:otm|ebs|fusion|apps|cloud|scm|hcm|erp)|\berp\b|workday|servicenow|peoplesoft|netsuite|"
           r"dynamics\s*365|guidewire|pega\b|mainframe|cobol",
    "business_analyst": r"business analyst|\bba\b",
    "project_manager": r"project manager|scrum master|program manager",
}

# Domains that define the job on their own: a title containing one of these is a mismatch
# unless the candidate allows it, even if the title also mentions an allowed technology.
EXCLUSIVE_DOMAINS = ["qa", "devops", "salesforce", "sap", "erp", "business_analyst", "project_manager", "data"]

CANDIDATE_ROLES = {
    "java_candidate": {
        "primary_role": "Java Developer",
        "search_roles": ["Java Developer", "Java Full Stack Developer", "Java Backend Developer"],
        "allowed_domains": ["java", "fullstack", "backend"],
        "exclusion_keywords": DEFAULT_EXCLUSION_KEYWORDS,
    },
}

CLIENTS = {
    "java_candidate": {
        "name": "Jane Doe",
        "email": "janedoe@example.com",
        "phone": "555-019-2834",
        "linkedin": "https://www.linkedin.com/in/janedoe",
        "location": "Dallas, TX",
        "relocation": "Yes",
        "work_auth": "H1B",
        "availability": "Immediate",
        "experience": "8+ Years",
        "resume_candidates": ["clients/java_candidate_resume.docx", "clients/sample_java_resume.docx"],
        # Name of a customizer registered in gmail_sender.CUSTOMIZERS ("default" = resume_customizer).
        "customizer": "default",
    },
}


def candidate_keys() -> list[str]:
    return [k for k in CLIENTS if k in CANDIDATE_ROLES]


def get_candidate(key: str) -> dict:
    """Merged profile + role config for one candidate."""
    if key not in CLIENTS or key not in CANDIDATE_ROLES:
        raise KeyError(f"Candidate {key!r} must exist in both CLIENTS and CANDIDATE_ROLES")
    return {"key": key, **CANDIDATE_ROLES[key], **CLIENTS[key]}


def resolve_resume(candidate: dict):
    """First resume in resume_candidates that exists on disk, or None."""
    for rel in candidate.get("resume_candidates", []):
        path = settings.ROOT / rel
        if path.exists():
            return path
    return None


def validate_candidate(key: str) -> list[str]:
    """Human-readable problems with a candidate's configuration (empty list = OK)."""
    try:
        c = get_candidate(key)
    except KeyError as exc:
        return [str(exc)]
    problems = []
    for field in ("name", "email", "primary_role", "search_roles"):
        if not c.get(field):
            problems.append(f"{key}: missing {field}")
    unknown = [d for d in c.get("allowed_domains", []) if d not in ROLE_DOMAINS]
    if unknown:
        problems.append(f"{key}: unknown allowed_domains {unknown}")
    if resolve_resume(c) is None:
        problems.append(f"{key}: no resume found in {c.get('resume_candidates')}")
    return problems
