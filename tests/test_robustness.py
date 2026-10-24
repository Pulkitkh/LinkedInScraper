"""Failure modes that must never break a live run: bad credentials, dropped connections, quota, Excel, Windows."""

import os
import smtplib
import subprocess
import sys
import types

import pytest

import csv_store
import followup_sender
import gmail_sender as gs
import imap_reply_detector
import resume_customizer as rc
import settings
from resume_customizer import CustomizationResult
from test_followup_imap_analytics import NOW, history_row
from test_gmail_sender import FakeSMTP, recruiter


@pytest.fixture
def tailored(workspace):
    pdf = workspace / "tailored.pdf"
    pdf.write_bytes(b"%PDF-1.4 tailored")
    return lambda candidate, jd, role: CustomizationResult(path=pdf, customized=True, note="tailored")


def _seed(*rows):
    csv_store.append_rows(settings.recruiters_csv(), csv_store.RECRUITER_FIELDS, list(rows))


# ------------------------------------------------------------------ SMTP

def test_bad_gmail_password_stops_run_without_marking_anyone_failed(workspace, tailored):
    _seed(recruiter("a@x.com", "p1"), recruiter("b@x.com", "p2"))

    def factory():
        raise gs.SendAborted("Gmail rejected the login")

    report = gs.send_outreach("jane", mode="live", smtp_factory=factory, sleep=lambda s: None, customize=tailored)
    assert report.aborted == "Gmail rejected the login" and report.sent == 0
    assert csv_store.read_rows(settings.history_csv(), csv_store.HISTORY_FIELDS) == []
    assert {r["email_sent_status"] for r in csv_store.read_rows(settings.recruiters_csv(), csv_store.RECRUITER_FIELDS)} == {"pending"}


def test_smtp_connect_explains_auth_failure(workspace, monkeypatch):
    class Server:
        def __init__(self, *a, **k):
            pass

        def starttls(self):
            pass

        def login(self, user, password):
            raise smtplib.SMTPAuthenticationError(535, b"5.7.8 Username and Password not accepted")

    monkeypatch.setattr(smtplib, "SMTP", Server)
    with pytest.raises(gs.SendAborted, match="App Password"):
        gs.smtp_connect()


def test_smtp_unreachable_on_both_ports(workspace, monkeypatch):
    def refuse(*a, **k):
        raise ConnectionRefusedError("refused")

    monkeypatch.setattr(smtplib, "SMTP", refuse)
    monkeypatch.setattr(smtplib, "SMTP_SSL", refuse)
    with pytest.raises(gs.SendAborted, match="587 or 465"):
        gs.smtp_connect()


def test_missing_gmail_settings(workspace, monkeypatch):
    monkeypatch.setenv("GMAIL_APP_PASSWORD", "")
    with pytest.raises(gs.SendAborted, match="GMAIL_APP_PASSWORD"):
        gs.smtp_connect()


def test_dropped_connection_reconnects_and_resends(workspace, tailored):
    servers = []

    class Flaky(FakeSMTP):
        def send_message(self, msg):
            if len(servers) == 1:
                raise smtplib.SMTPServerDisconnected("Connection unexpectedly closed")
            super().send_message(msg)

    def factory():
        servers.append(Flaky())
        return servers[-1]

    report = gs.send_outreach("jane", mode="live", recruiters=[recruiter()], smtp_factory=factory,
                              sleep=lambda s: None, customize=tailored)
    assert report.sent == 1 and len(servers) == 2 and len(servers[1].sent) == 1


def test_gmail_quota_stops_the_run(workspace, tailored):
    class Quota(FakeSMTP):
        def send_message(self, msg):
            if self.sent:
                raise smtplib.SMTPDataError(550, b"5.4.5 Daily user sending quota exceeded")
            super().send_message(msg)

    server = Quota()
    rows = [recruiter(f"r{i}@x.com", f"p{i}") for i in range(4)]
    report = gs.send_outreach("jane", mode="live", recruiters=rows, smtp_factory=lambda: server,
                              sleep=lambda s: None, customize=tailored)
    assert (report.sent, report.failed) == (1, 1) and "quota" in report.aborted
    assert len(csv_store.read_rows(settings.history_csv(), csv_store.HISTORY_FIELDS)) == 2  # r2, r3 never tried


def test_rejected_recipient_does_not_stop_others(workspace, tailored):
    server = FakeSMTP(fail_for=("bad@x.com",))
    rows = [recruiter("bad@x.com", "p1"), recruiter("good@x.com", "p2")]
    report = gs.send_outreach("jane", mode="live", recruiters=rows, smtp_factory=lambda: server,
                              sleep=lambda s: None, customize=tailored)
    assert (report.sent, report.failed, report.aborted) == (1, 1, "")


def test_resume_crash_falls_back_to_original_resume(workspace):
    def broken(candidate, jd, role):
        raise RuntimeError("Word crashed")

    server = FakeSMTP()
    report = gs.send_outreach("jane", mode="live", recruiters=[recruiter()], smtp_factory=lambda: server,
                              sleep=lambda s: None, customize=broken)
    assert report.sent == 1 and "original resume attached" in report.details[0][2]
    assert next(server.sent[0].iter_attachments()).get_filename().startswith("Jane_Doe_Resume_")


def test_followups_stop_on_bad_credentials(workspace):
    csv_store.append_rows(settings.history_csv(), csv_store.HISTORY_FIELDS, [history_row("due@x.com", 8)])

    def factory():
        raise gs.SendAborted("Gmail rejected the login")

    report = followup_sender.send_followups(mode="live", now=NOW, smtp_factory=factory, sleep=lambda s: None,
                                            generate=lambda *a, **k: "Value.")
    assert report.aborted and report.sent == 0
    assert csv_store.read_rows(settings.history_csv(), csv_store.HISTORY_FIELDS)[0]["followup_count"] == "0"


@pytest.mark.parametrize("name,email,greeting", [
    ("", "jobs@x.com", "Hi there,"), ("  ", "john@techcorp.com", "Hi there,"),
    ("mary-ann o'neil", "maryann.oneil@x.com", "Hi Mary-ann,")])
def test_odd_recruiter_names_and_braces_in_jd(workspace, tailored, name, email, greeting):
    rec = {**recruiter(email), "recruiter_name": name,
           "job_description": "Java {Spring} C2C ${salary} %s Dallas, TX"}
    server = FakeSMTP()
    gs.send_outreach("jane", mode="test", recruiters=[rec], smtp_factory=lambda: server, sleep=lambda s: None,
                     customize=tailored)
    assert server.sent[0].get_body(("plain",)).get_content().startswith(greeting)


# ------------------------------------------------------------------ CSV / Excel

def test_reads_csv_saved_by_excel_with_bom(tmp_path):
    path = tmp_path / "recruiters.csv"
    path.write_bytes("﻿recruiter_name,recruiter_email\nJosé,j@x.com\n".encode("utf-8"))
    assert csv_store.read_rows(path, csv_store.RECRUITER_FIELDS)[0]["recruiter_name"] == "José"
    assert not csv_store.migrate(path, ["recruiter_name", "recruiter_email"])


def test_reads_csv_saved_by_excel_as_ansi(tmp_path):
    path = tmp_path / "recruiters.csv"
    path.write_bytes("recruiter_name,recruiter_email\nJosé,j@x.com\n".encode("cp1252"))
    assert csv_store.read_rows(path, csv_store.RECRUITER_FIELDS)[0]["recruiter_name"] == "José"


def test_csv_open_in_excel_raises_clear_error_and_keeps_data(tmp_path, monkeypatch):
    path = tmp_path / "recruiters.csv"
    csv_store.write_rows(path, csv_store.RECRUITER_FIELDS, [{"recruiter_email": "keep@x.com"}])

    def locked(src, dst):
        raise PermissionError("being used by another process")

    monkeypatch.setattr(csv_store.os, "replace", locked)
    monkeypatch.setattr(csv_store.time, "sleep", lambda s: None)
    with pytest.raises(csv_store.CsvLockedError, match="close it in Excel"):
        csv_store.append_rows(path, csv_store.RECRUITER_FIELDS, [{"recruiter_email": "new@x.com"}])
    monkeypatch.undo()
    assert [r["recruiter_email"] for r in csv_store.read_rows(path, csv_store.RECRUITER_FIELDS)] == ["keep@x.com"]
    assert [p.name for p in tmp_path.iterdir()] == ["recruiters.csv"]  # no temp files left behind


def test_long_job_description_roundtrip(tmp_path):
    path = tmp_path / "r.csv"
    jd = "Java, \"Spring\"\nline two, with commas\n" * 5000
    csv_store.append_rows(path, csv_store.RECRUITER_FIELDS, [{"job_description": jd}])
    assert csv_store.read_rows(path, csv_store.RECRUITER_FIELDS)[0]["job_description"] == jd


# ------------------------------------------------------------------ IMAP / Windows

def test_imap_date_is_english_regardless_of_locale():
    from datetime import datetime

    assert [imap_reply_detector.imap_date(datetime(2026, m, 5)) for m in (1, 6, 12)] == \
        ["05-Jan-2026", "05-Jun-2026", "05-Dec-2026"]


def test_windows_word_conversion_retries_after_killing_word(tmp_path, monkeypatch):
    docx = tmp_path / "r.docx"
    docx.write_bytes(b"docx")
    attempts, killed = [], []

    def convert(src, dst):
        attempts.append(src)
        if len(attempts) == 1:
            raise RuntimeError("Word is busy")
        open(dst, "wb").write(b"%PDF")

    monkeypatch.setitem(sys.modules, "docx2pdf", types.SimpleNamespace(convert=convert))
    monkeypatch.setattr(rc.sys, "platform", "win32")
    monkeypatch.setattr(rc.subprocess, "run", lambda args, **k: killed.append(args))
    monkeypatch.setenv("DATA_DIR", str(tmp_path))
    assert rc.docx_to_pdf(docx) == docx.with_suffix(".pdf")
    assert killed == [["taskkill", "/f", "/im", "winword.exe"]] and len(attempts) == 2


def test_libreoffice_timeout_returns_none(tmp_path, monkeypatch):
    docx = tmp_path / "r.docx"
    docx.write_bytes(b"docx")

    def hang(*a, **k):
        raise subprocess.TimeoutExpired("soffice", 180)

    monkeypatch.setattr(rc.sys, "platform", "linux")
    monkeypatch.setattr(rc.shutil, "which", lambda name: "/usr/bin/soffice")
    monkeypatch.setattr(rc.subprocess, "run", hang)
    monkeypatch.setenv("DATA_DIR", str(tmp_path))
    assert rc.docx_to_pdf(docx) is None


def test_corrupt_resume_is_reported_by_validation(workspace, monkeypatch):
    import client_config

    bad = workspace / "bad.docx"
    bad.write_bytes(b"not a docx")
    monkeypatch.setitem(client_config.CLIENTS["jane"], "resume_candidates", [str(bad)])
    server = FakeSMTP()
    report = gs.send_outreach("jane", mode="test", recruiters=[recruiter()], smtp_factory=lambda: server,
                              sleep=lambda s: None)
    # tailoring fails on the corrupt file, the run continues and attaches the file as-is
    assert report.sent == 1 and "original resume attached" in report.details[0][2]


def test_no_stray_files_in_data_dir(workspace):
    assert sorted(p.name for p in workspace.iterdir() if not p.name.startswith("jane_resume")) == []
    assert os.environ["DATA_DIR"] == str(workspace)
