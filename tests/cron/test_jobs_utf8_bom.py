from __future__ import annotations

from cron.jobs import current_cron_store, load_jobs, use_cron_store


def test_load_jobs_accepts_utf8_bom(tmp_path):
    with use_cron_store(tmp_path):
        jobs_file = current_cron_store().jobs_file
        jobs_file.parent.mkdir(parents=True)
        jobs_file.write_bytes(
            b'\xef\xbb\xbf{"jobs": [{"id": "bom-job", "name": "BOM", "enabled": true}]}'
        )

        jobs = load_jobs()

    assert jobs == [{"id": "bom-job", "name": "BOM", "enabled": True}]


def test_load_jobs_accepts_bomless_utf8(tmp_path):
    with use_cron_store(tmp_path):
        jobs_file = current_cron_store().jobs_file
        jobs_file.parent.mkdir(parents=True)
        jobs_file.write_text(
            '{"jobs": [{"id": "plain-job", "enabled": true}]}',
            encoding="utf-8",
        )

        jobs = load_jobs()

    assert jobs == [{"id": "plain-job", "enabled": True}]
