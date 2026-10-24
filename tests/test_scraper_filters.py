import pytest

import client_config
import csv_store
import linkedin_scraper as ls
import settings

JAVA = {"allowed_domains": ["java", "fullstack", "backend"]}


def test_query_uses_linkedin_boolean_operators():
    assert ls.build_search_query("Java Developer") == \
        '"Java Developer" AND C2C NOT hotlist NOT fulltime NOT "available consultants" NOT Bench'
    assert ls.relaxed_search_query("Java Developer") == '"Java Developer" C2C'


def test_search_url_filters():
    url = ls.build_search_url("x")
    assert "datePosted=%22past-24h%22" in url and "sortBy=%22date_posted%22" in url
    assert "geoUrn=%5B%22103644278%22%5D" in url


def test_extract_emails_mailto_first_and_obfuscated():
    text = "Reach out: jane@corp.com, or HR (at) corp (dot) io. logo@2x.png"
    assert ls.extract_emails(text, ["mailto:Boss@Corp.com?subject=hi"]) == ["boss@corp.com", "jane@corp.com", "hr@corp.io"]


@pytest.mark.parametrize("text,expected", [
    ("Java Developer C2C, Dallas, TX", True),
    ("Remote - US only, GC/USC", True),
    ("Onsite in Jersey City", True),
    ("Java Developer, Noida", False),
    ("Toronto or Dallas, TX", False),  # any non-USA indicator rejects
    ("Java Developer C2C", False),     # no location signal at all
])
def test_is_usa_job(text, expected):
    assert ls.is_usa_job(text) is expected


@pytest.mark.parametrize("text", ["I have a candidate available for C2C", "Marketing my consultant - Java",
                                  "Today's HOTLIST", "Our bench sales team"])
def test_bench_marketing(text):
    assert ls.is_bench_marketing(text)


def test_detect_job_title():
    assert ls.detect_job_title("Hi all\nJob Title: Sr. Java Engineer\nLocation: TX") == "Sr. Java Engineer"
    assert ls.detect_job_title("#hiring\nLooking for a Salesforce Developer - C2C") == "Salesforce Developer"
    assert ls.detect_job_title("nothing here") == ""


@pytest.mark.parametrize("text,mismatch", [
    ("Job Title: DevOps Engineer\nJava a plus", True),
    ("Role: Salesforce Developer", True),
    ("Position: Java Full Stack Developer", False),
    ("Need a Backend Engineer (Spring Boot)", False),
    ("Great opportunity, C2C, Dallas TX", True),  # no title, nothing Java-related anywhere
])
def test_role_mismatch(text, mismatch):
    assert ls.role_mismatch(text, JAVA) is mismatch


def test_matches_strict_filter():
    assert ls.matches_strict_filter("Java Developer C2C Dallas, TX", JAVA) == ("pending", "")
    assert ls.matches_strict_filter("QA Tester, Java, Dallas, TX", JAVA)[0] == "mismatched_role"
    assert ls.matches_strict_filter("Java hotlist USA", JAVA) == (None, "bench marketing")
    assert ls.matches_strict_filter("Java Developer Pune", JAVA) == (None, "non-USA")


def test_recruiter_name():
    assert ls.recruiter_name_from_profile("https://www.linkedin.com/in/john-smith-4b2a91/") == "John Smith"
    assert ls.recruiter_name_from_profile("https://x/in/a", "Priya Sharma (She/Her), MBA") == "Priya Sharma"
    assert ls.recruiter_name_from_profile("") == ""


def test_pending_recruiters_skips_contacted_and_mismatched(workspace):
    base = {"role": "Java Developer", "job_description": "Java Developer, C2C, Dallas, TX",
            "timestamp": csv_store.now(), "post_url": "u"}
    csv_store.append_rows(settings.recruiters_csv(), csv_store.RECRUITER_FIELDS, [
        {**base, "recruiter_name": "A", "recruiter_email": "a@x.com", "email_sent_status": "pending"},
        {**base, "recruiter_name": "A2", "recruiter_email": "A@x.com", "email_sent_status": "pending"},
        {**base, "recruiter_name": "B", "recruiter_email": "b@x.com", "email_sent_status": "mismatched_role"},
        {**base, "recruiter_name": "C", "recruiter_email": "c@x.com", "email_sent_status": "pending"},
        {**base, "recruiter_name": "D", "recruiter_email": "d@x.com", "email_sent_status": "pending", "role": "QA"},
    ])
    csv_store.append_rows(settings.history_csv(), csv_store.HISTORY_FIELDS, [
        {"recruiter_email": "c@x.com", "candidate": "Jane Doe", "email_sent_status": "sent"},
    ])
    assert [r["recruiter_email"] for r in ls.pending_recruiters("jane")] == ["a@x.com"]


def test_validate_candidate(workspace, monkeypatch):
    assert client_config.validate_candidate("jane") == []
    monkeypatch.setitem(client_config.CLIENTS["jane"], "resume_candidates", ["clients/missing.docx"])
    assert "no resume found" in client_config.validate_candidate("jane")[0]
