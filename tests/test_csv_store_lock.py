import csv
import os
import threading
import time

import pytest

import csv_store
import lock_util


def test_migrate_adds_missing_columns(tmp_path):
    path = tmp_path / "email_sent_history.csv"
    path.write_text("recruiter_email,candidate,email_sent_status\nx@y.com,Jane,sent\n")
    assert csv_store.migrate(path, csv_store.HISTORY_FIELDS)
    with open(path, newline="", encoding="utf-8-sig") as fh:
        reader = csv.DictReader(fh)
        assert reader.fieldnames == csv_store.HISTORY_FIELDS
        row = next(reader)
    assert row["recruiter_email"] == "x@y.com" and row["followup_count"] == "0" and row["replied"] == "0"
    assert not csv_store.migrate(path, csv_store.HISTORY_FIELDS)


def test_update_rows(tmp_path):
    path = tmp_path / "r.csv"
    csv_store.append_rows(path, csv_store.RECRUITER_FIELDS, [{"recruiter_email": "a"}, {"recruiter_email": "b"}])
    assert csv_store.update_rows(path, csv_store.RECRUITER_FIELDS, lambda r: r["recruiter_email"] == "b",
                                 {"email_sent_status": "sent"}) == 1
    assert [r["email_sent_status"] for r in csv_store.read_rows(path, csv_store.RECRUITER_FIELDS)] == ["pending", "sent"]


def test_file_lock_is_exclusive(tmp_path):
    lock = tmp_path / "x.lock"
    order = []

    def worker(name):
        with lock_util.file_lock(lock, timeout=5, poll=0.01):
            order.append(f"{name}-in")
            time.sleep(0.05)
            order.append(f"{name}-out")

    threads = [threading.Thread(target=worker, args=(n,)) for n in "ab"]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert order[0][0] == order[1][0] and order[2][0] == order[3][0]  # never interleaved
    assert not lock.exists()


def test_file_lock_timeout_and_stale(tmp_path):
    lock = tmp_path / "x.lock"
    lock.write_text("123")
    with pytest.raises(lock_util.LockTimeout):
        with lock_util.file_lock(lock, timeout=0.1, poll=0.01):
            pass
    old = time.time() - 3600
    os.utime(lock, (old, old))
    with lock_util.file_lock(lock, timeout=0.1, stale_after=60):
        pass
