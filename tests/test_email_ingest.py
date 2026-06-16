from __future__ import annotations

from app.email_ingest import EmailIngestService, email_matches_source, parse_email_message
from app.models import EmailSourceRuntime


def make_source(**overrides) -> EmailSourceRuntime:
    values = {
        "feed_key": "isw-newsletter",
        "display_name": "Email: ISW",
        "imap_host_env": "EMAIL_IMAP_HOST",
        "imap_port_env": "EMAIL_IMAP_PORT",
        "username_env": "EMAIL_USERNAME",
        "password_env": "EMAIL_PASSWORD",
        "mailbox": "INBOX",
        "from_contains": ("understandingwar.org",),
        "list_id_contains": (),
        "subject_contains": (),
        "url": "imap://EMAIL_IMAP_HOST/INBOX/isw-newsletter",
        "normalized_url": "imap://email_imap_host/INBOX/isw-newsletter",
        "interval_seconds": 300,
        "channel_ids": (),
        "channel_keys": (),
    }
    values.update(overrides)
    return EmailSourceRuntime(**values)


def test_plain_text_email_prefers_text_body_and_extracts_metadata() -> None:
    raw = (
        b"From: Institute for the Study of War <updates@understandingwar.org>\r\n"
        b"Subject: Russian Offensive Campaign Assessment\r\n"
        b"Date: Fri, 12 Jun 2026 12:30:00 +0000\r\n"
        b"Message-ID: <abc123@understandingwar.org>\r\n"
        b"List-ID: ISW Updates <updates.understandingwar.org>\r\n"
        b"Content-Type: text/plain; charset=utf-8\r\n"
        b"\r\n"
        b"Today's assessment is available at https://understandingwar.org/backgrounder/russian-offensive-campaign-assessment\r\n"
    )

    parsed = parse_email_message("42", raw)

    assert parsed.uid == "42"
    assert parsed.message_id == "abc123@understandingwar.org"
    assert parsed.subject == "Russian Offensive Campaign Assessment"
    assert parsed.sender_email == "updates@understandingwar.org"
    assert parsed.list_id == "ISW Updates <updates.understandingwar.org>"
    assert parsed.canonical_url == "https://understandingwar.org/backgrounder/russian-offensive-campaign-assessment"
    assert "Today's assessment" in (parsed.body or "")
    assert email_matches_source(parsed, make_source()) is True


def test_html_email_uses_clean_html_fallback_and_ignores_attachment() -> None:
    raw = (
        b"From: updates@understandingwar.org\r\n"
        b"Subject: Iran Update\r\n"
        b"Message-ID: <html1@understandingwar.org>\r\n"
        b"MIME-Version: 1.0\r\n"
        b"Content-Type: multipart/mixed; boundary=outer\r\n"
        b"\r\n"
        b"--outer\r\n"
        b"Content-Type: text/html; charset=utf-8\r\n"
        b"\r\n"
        b"<html><body><h1>Iran Update</h1><p>Read it <a href=\"https://understandingwar.org/iran-update\">online</a>.</p></body></html>\r\n"
        b"--outer\r\n"
        b"Content-Type: application/pdf\r\n"
        b"Content-Disposition: attachment; filename=report.pdf\r\n"
        b"\r\n"
        b"%PDF-ignored\r\n"
        b"--outer--\r\n"
    )

    parsed = parse_email_message("43", raw)

    assert parsed.subject == "Iran Update"
    assert parsed.body == "Iran Update\nRead it online."
    assert parsed.formatted_body == "**Iran Update**\nRead it [online](https://understandingwar.org/iran-update)."
    assert parsed.canonical_url == "https://understandingwar.org/iran-update"
    assert email_matches_source(parsed, make_source(subject_contains=("iran update",))) is True


def test_email_match_rules_are_all_required_when_configured() -> None:
    raw = (
        b"From: alerts@example.com\r\n"
        b"Subject: Russian Offensive Campaign Assessment\r\n"
        b"List-ID: ISW Updates <updates.understandingwar.org>\r\n"
        b"\r\n"
        b"Body\r\n"
    )
    parsed = parse_email_message("44", raw)

    assert email_matches_source(
        parsed,
        make_source(from_contains=("understandingwar.org",), list_id_contains=("understandingwar.org",)),
    ) is False
    assert email_matches_source(
        parsed,
        make_source(from_contains=(), list_id_contains=("understandingwar.org",)),
    ) is True


def test_email_match_all_accepts_any_sender() -> None:
    raw = (
        b"From: CISA <cisa@messages.cisa.gov>\r\n"
        b"Subject: Cybersecurity Advisory\r\n"
        b"\r\n"
        b"Body\r\n"
    )
    parsed = parse_email_message("45", raw)

    assert email_matches_source(
        parsed,
        make_source(from_contains=(), subject_contains=(), list_id_contains=(), match_all=True),
    ) is True


def test_plain_text_urls_are_hidden_behind_markdown_links() -> None:
    raw = (
        b"From: Alerts <alerts@example.com>\r\n"
        b"Subject: Daily Alert\r\n"
        b"Content-Type: text/plain; charset=utf-8\r\n"
        b"\r\n"
        b"Read the update at https://example.com/very/long/path?utm_source=email\r\n"
    )

    parsed = parse_email_message("46", raw)

    assert "[example.com/very/long/path](https://example.com/very/long/path)" in (
        parsed.formatted_body or ""
    )
    assert parsed.canonical_url == "https://example.com/very/long/path"


def test_newsletter_with_multiple_article_links_splits_into_feed_entries(monkeypatch) -> None:
    raw = (
        b"From: Briefing <briefing@example.com>\r\n"
        b"Subject: Morning Security Brief\r\n"
        b"Message-ID: <brief1@example.com>\r\n"
        b"Date: Sat, 13 Jun 2026 12:00:00 +0000\r\n"
        b"MIME-Version: 1.0\r\n"
        b"Content-Type: text/html; charset=utf-8\r\n"
        b"\r\n"
        b"<html><body>"
        b"<h2>Allied ships enter the Red Sea for air defense drills</h2>"
        b"<p>Naval forces began a week of exercises. <a href=\"https://example.com/red-sea-drills\">Read more</a></p>"
        b"<h2>Cyber agency warns of new infrastructure campaign</h2>"
        b"<p>Officials urged immediate patching. <a href=\"https://example.com/cyber-warning\">Full story</a></p>"
        b"<p><a href=\"https://example.com/unsubscribe\">Unsubscribe</a></p>"
        b"</body></html>\r\n"
    )
    service = EmailIngestService(timeout_seconds=10, max_messages_per_source=10)
    monkeypatch.setattr(service, "_fetch_sync", lambda _source, _since_uid: (parse_email_message("47", raw),))

    import asyncio

    fetch_result = asyncio.run(service.fetch(make_source(from_contains=(), match_all=True), since_uid=None))

    assert len(fetch_result.entries) == 2
    titles = [entry.raw_title for entry in fetch_result.entries]
    assert titles == [
        "Allied ships enter the Red Sea for air defense drills",
        "Cyber agency warns of new infrastructure campaign",
    ]
    assert fetch_result.entries[0].raw_url == "https://example.com/red-sea-drills"
    assert fetch_result.entries[1].raw_url == "https://example.com/cyber-warning"
    assert fetch_result.entries[0].raw_guid != fetch_result.entries[1].raw_guid
    assert fetch_result.entries[0].rich_metadata["split"] is True
    assert "Cyber agency warns" not in (fetch_result.entries[0].summary or "")
    assert "Unsubscribe" not in (fetch_result.entries[1].summary or "")


def test_multipart_newsletter_uses_html_for_split_links(monkeypatch) -> None:
    raw = (
        b"From: Briefing <briefing@example.com>\r\n"
        b"Subject: Multipart Security Brief\r\n"
        b"Message-ID: <brief2@example.com>\r\n"
        b"MIME-Version: 1.0\r\n"
        b"Content-Type: multipart/alternative; boundary=alt\r\n"
        b"\r\n"
        b"--alt\r\n"
        b"Content-Type: text/plain; charset=utf-8\r\n"
        b"\r\n"
        b"Plain fallback without useful article links.\r\n"
        b"--alt\r\n"
        b"Content-Type: text/html; charset=utf-8\r\n"
        b"\r\n"
        b"<h2>Ukraine command post struck during overnight raid</h2>"
        b"<p>Officials described the operation. <a href=\"https://example.com/ukraine-raid\">Read more</a></p>"
        b"<h2>Taiwan tracks Chinese aircraft near median line</h2>"
        b"<p>Defense officials released updated figures. <a href=\"https://example.com/taiwan-aircraft\">Full story</a></p>"
        b"--alt--\r\n"
    )
    service = EmailIngestService(timeout_seconds=10, max_messages_per_source=10)
    monkeypatch.setattr(service, "_fetch_sync", lambda _source, _since_uid: (parse_email_message("48", raw),))

    import asyncio

    fetch_result = asyncio.run(service.fetch(make_source(from_contains=(), match_all=True), since_uid=None))

    assert len(fetch_result.entries) == 2
    assert fetch_result.entries[0].summary
    assert "Plain fallback" not in fetch_result.entries[0].summary
    assert fetch_result.entries[0].raw_url == "https://example.com/ukraine-raid"


def test_newsletter_split_ignores_tracker_event_and_survey_links(monkeypatch) -> None:
    raw = (
        b"From: Briefing <briefing@example.com>\r\n"
        b"Subject: Defense Daily Brief\r\n"
        b"Message-ID: <brief3@example.com>\r\n"
        b"MIME-Version: 1.0\r\n"
        b"Content-Type: text/html; charset=utf-8\r\n"
        b"\r\n"
        b"<html><body>"
        b"<h2>Army tests new counter-drone package in Europe</h2>"
        b"<p>Officials said the prototype is moving into field trials. "
        b"<a href=\"https://defensescoop.com/2026/06/14/army-counter-drone-package-europe/\">"
        b"defensescoop.com/2026/06/14/army-counter-drone-package-europe/</a></p>"
        b"<p><a href=\"https://d598k304.na1.hubspotlinks.com/e3t/Ctc/ABC\">Tracking redirect</a></p>"
        b"<p><a href=\"https://fedtalks.upgather.com/register\">Register now</a></p>"
        b"<p><a href=\"https://surveys.hotjar.com/abc\">Take our survey</a></p>"
        b"<h2>Navy awards destroyer modernization contract</h2>"
        b"<p>The contract covers combat systems work. "
        b"<a href=\"https://fedscoop.com/navy-awards-destroyer-modernization-contract/\">Read more</a></p>"
        b"</body></html>\r\n"
    )
    service = EmailIngestService(timeout_seconds=10, max_messages_per_source=10)
    monkeypatch.setattr(service, "_fetch_sync", lambda _source, _since_uid: (parse_email_message("49", raw),))

    import asyncio

    fetch_result = asyncio.run(service.fetch(make_source(from_contains=(), match_all=True), since_uid=None))

    assert [entry.raw_title for entry in fetch_result.entries] == [
        "Army tests new counter-drone package in Europe",
        "Navy awards destroyer modernization contract",
    ]
    assert [entry.raw_url for entry in fetch_result.entries] == [
        "https://defensescoop.com/2026/06/14/army-counter-drone-package-europe",
        "https://fedscoop.com/navy-awards-destroyer-modernization-contract",
    ]
    combined = "\n".join(entry.summary or "" for entry in fetch_result.entries)
    assert "hubspotlinks" not in combined
    assert "upgather" not in combined
    assert "hotjar" not in combined


def test_email_unwraps_tracker_redirect_to_wanted_article_url() -> None:
    raw = (
        b"From: Briefing <briefing@example.com>\r\n"
        b"Subject: War on the Rocks Daily\r\n"
        b"Content-Type: text/html; charset=utf-8\r\n"
        b"\r\n"
        b"<h2>How drones are changing the battlefield</h2>"
        b"<p>Analysis of the military implications. "
        b"<a href=\"https://track.dripemail2.com/c/abc?url=https%3A%2F%2Fwarontherocks.com%2F2026%2F06%2Fhow-drones-are-changing-the-battlefield%2F%3Futm_source%3Demail\">"
        b"Read more</a></p>\r\n"
    )

    parsed = parse_email_message("51", raw)

    assert parsed.canonical_url == "https://warontherocks.com/2026/06/how-drones-are-changing-the-battlefield"
    assert "dripemail" not in (parsed.formatted_body or "")
    assert "[Read more](https://warontherocks.com/2026/06/how-drones-are-changing-the-battlefield)" in (
        parsed.formatted_body or ""
    )


def test_split_email_entry_keeps_short_display_summary_and_richer_routing_summary(monkeypatch) -> None:
    raw = (
        b"From: Briefing <briefing@example.com>\r\n"
        b"Subject: Morning Security Brief\r\n"
        b"Message-ID: <brief5@example.com>\r\n"
        b"MIME-Version: 1.0\r\n"
        b"Content-Type: text/html; charset=utf-8\r\n"
        b"\r\n"
        b"<h2>Navy tracks Russian submarine near allied waters</h2>"
        b"<p>Officials said allied maritime patrol aircraft monitored the transit.</p>"
        b"<p>Commanders said the activity would inform future undersea surveillance planning.</p>"
        b"<p><a href=\"https://example.com/navy-submarine\">Read more</a></p>"
        b"<h2>Cyber agency warns of new infrastructure campaign</h2>"
        b"<p>Officials urged immediate patching across critical infrastructure operators.</p>"
        b"<p><a href=\"https://example.com/cyber-warning\">Full story</a></p>"
    )
    service = EmailIngestService(timeout_seconds=10, max_messages_per_source=10)
    monkeypatch.setattr(service, "_fetch_sync", lambda _source, _since_uid: (parse_email_message("52", raw),))

    import asyncio

    fetch_result = asyncio.run(service.fetch(make_source(from_contains=(), match_all=True), since_uid=None))
    entry = fetch_result.entries[0]

    assert entry.summary
    assert "(https://example.com/navy-submarine)" in entry.summary
    assert entry.rich_metadata["routing_summary"]
    assert "undersea surveillance planning" in entry.rich_metadata["routing_summary"]


def test_email_does_not_split_when_only_urlish_and_utility_links_exist(monkeypatch) -> None:
    raw = (
        b"From: Briefing <briefing@example.com>\r\n"
        b"Subject: Event and Sponsor Links\r\n"
        b"Message-ID: <brief4@example.com>\r\n"
        b"Content-Type: text/html; charset=utf-8\r\n"
        b"\r\n"
        b"<p><a href=\"https://d598k304.na1.hubspotlinks.com/e3t/Ctc/ABC\">"
        b"d598k304.na1.hubspotlinks.com/e3t/Ctc/ABC</a></p>"
        b"<p><a href=\"https://defensetalks.upgather.com/register\">DefenseTalks</a></p>"
        b"<p><a href=\"https://surveys.hotjar.com/abc\">Take our survey</a></p>\r\n"
    )
    service = EmailIngestService(timeout_seconds=10, max_messages_per_source=10)
    monkeypatch.setattr(service, "_fetch_sync", lambda _source, _since_uid: (parse_email_message("50", raw),))

    import asyncio

    fetch_result = asyncio.run(service.fetch(make_source(from_contains=(), match_all=True), since_uid=None))

    assert len(fetch_result.entries) == 1
    assert fetch_result.entries[0].raw_title == "Event and Sponsor Links"
    assert fetch_result.entries[0].raw_url is None
