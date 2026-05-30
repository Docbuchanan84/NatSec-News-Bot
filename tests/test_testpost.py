from __future__ import annotations

from pathlib import Path

from app.database import Database


def test_create_test_article_returns_post_job_and_records_article(tmp_path: Path) -> None:
    db = Database(tmp_path / "rss.sqlite")
    db.initialize()
    job = db.create_test_article("111111111111111111")
    assert job.title == "RSS Dispatch Bot test post"
    assert job.channel_id == "111111111111111111"
    assert db.counts()["articles"] == 1
