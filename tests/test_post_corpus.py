"""Realistic LinkedIn C2C posts and the outcome each one must get from the strict filters."""

import pytest

import linkedin_scraper as ls

JAVA = {"allowed_domains": ["java", "fullstack", "backend"]}

CORPUS = [
    # (post text, expected status, expected first email)
    ("#hiring #c2c\nRole: Java Developer\nLocation: Plano, TX (Day 1 onsite)\nDuration: 12+ months\n"
     "Visa: H1B/GC/USC\nSkills: Java 17, Spring Boot, Microservices, Kafka\nShare resumes at priya.k@techvision.com",
     "pending", "priya.k@techvision.com"),
    ("Urgent need: Sr Java Backend Engineer | Remote (USA) | C2C\nMust: Java, AWS Lambda, DynamoDB\n"
     "Please send profiles to RECRUIT@CloudNine.io with rate & visa", "pending", "recruit@cloudnine.io"),
    ("Looking for Java Full Stack Developer (Angular) in Jersey City, NJ - hybrid 3 days. C2C/W2.\n"
     "Email: mike.r@finstaff.com | Ph: (201) 555-0187", "pending", "mike.r@finstaff.com"),
    ("Position: Java Developer with Spring Boot\nCharlotte NC / Atlanta GA\nGC-EAD, H4-EAD, USC only\n"
     "resumes -> hr(at)peakstaff(dot)com", "pending", "hr@peakstaff.com"),
    ("Job Title: Java Microservices Lead\nLocation: Remote - EST hours\nContract: C2C\nContact jen@acme-it.com",
     "pending", "jen@acme-it.com"),
    # bench marketers / hotlists
    ("Hi connections, I have a Java Developer with 10+ yrs available for C2C roles in USA. "
     "Please share your requirements to sales@benchco.com", None, "sales@benchco.com"),
    ("Daily HOTLIST - Java, .NET, Python consultants available on our bench. USA. hotlist@vendor.us", None,
     "hotlist@vendor.us"),
    ("Marketing my consultant: Java Full Stack, 9 yrs, H1B, open to relocate anywhere in US. ravi@itsol.com",
     None, "ravi@itsol.com"),
    # non-USA
    ("Java Developer - C2C - Hyderabad (Hybrid). Mail jobs@techmahi.co.in", None, "jobs@techmahi.co.in"),
    ("Java Developer contract, Toronto, ON. Canadian citizens only. ca@talent.ca", None, "ca@talent.ca"),
    ("Java Developer C2C role, London UK, 6 months. uk@hire.co.uk", None, "uk@hire.co.uk"),
    # role mismatch (stored, never emailed)
    ("Job Title: QA Automation Engineer (Selenium + Java)\nDallas, TX - C2C\nqa@testpros.com", "mismatched_role",
     "qa@testpros.com"),
    ("Role: DevOps Engineer - Java background a plus\nAustin, TX\nC2C\ndevops@cloudops.com", "mismatched_role",
     "devops@cloudops.com"),
    ("Hiring Salesforce Developer (Apex), Chicago, IL, C2C. sf@crmjobs.com", "mismatched_role", "sf@crmjobs.com"),
    ("Need Python Data Engineer, NYC, C2C. data@pyhire.com", "mismatched_role", "data@pyhire.com"),
]


@pytest.mark.parametrize("text,status,email", CORPUS, ids=[f"post{i}" for i in range(len(CORPUS))])
def test_corpus(text, status, email):
    assert ls.matches_strict_filter(text, JAVA)[0] == status
    assert ls.extract_emails(text)[0] == email


def test_corpus_summary():
    statuses = [ls.matches_strict_filter(t, JAVA)[0] for t, _, _ in CORPUS]
    assert statuses.count("pending") == 5 and statuses.count("mismatched_role") == 4 and statuses.count(None) == 6
