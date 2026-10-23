"""Browser/API integration with disposable fixtures, never production state.

The report and screenshots explicitly describe test fixtures. The API lifespan
is not started: no X browser, monitor, capture, worker, or network service runs.
"""

from __future__ import annotations

import argparse
import importlib
import json
import sys
import tempfile
import wave
from datetime import UTC, datetime, timedelta
from pathlib import Path
from urllib.parse import urlsplit

# ruff: noqa: E402
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from fastapi.testclient import TestClient
from playwright.sync_api import sync_playwright

from xsa.analysis_platform import build_space_analysis
from xsa.config import Settings
from xsa.db import MonitoredAccount, Space, get_session, init_db
from xsa.diarization import run_offline_diarization
from xsa.paths import SpacePaths


def verify(output: Path) -> list[dict]:
    output.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix="xsa-workspace-fixture-") as temporary:
        root = Path(temporary)
        api = importlib.import_module("xsa.api.app")
        api._cfg = Settings(_env_file=None, data_dir=root / "data", db_path=root / "state" / "test.sqlite3")
        api._monitor = api._adapter = None
        init_db(api._cfg.db_path)
        now = datetime.now(UTC).replace(tzinfo=None)
        with get_session() as db:
            account = MonitoredAccount(
                username="fixture_host",
                display_name="حساب اختبار معزول",
                last_checked_at=now,
                next_check_at=now + timedelta(minutes=2),
                last_poll_outcome="checked",
                last_poll_duration_seconds=4.2,
                last_poll_request_count=7,
            )
            db.add(account)
            db.flush()
            db.add(
                Space(
                    space_id="fixture123",
                    state="analyzed",
                    title="تقرير اختبار معزول",
                    host_account_id=account.id,
                    started_at=now,
                )
            )
            db.add(
                Space(
                    space_id="fixtureSilent",
                    state="analyzed",
                    title="تسجيل صامت جزئي للاختبار",
                    host_account_id=account.id,
                    started_at=now - timedelta(hours=1),
                )
            )
            db.commit()
        paths = SpacePaths.for_space(api._cfg.data_dir, "fixture123", now)
        paths.ensure()
        paths.segment(0).write_bytes(b"disposable fixture media")
        paths.playback.write_bytes(b"disposable fixture playback")
        silent_paths = SpacePaths.for_space(api._cfg.data_dir, "fixtureSilent", now - timedelta(hours=1))
        silent_paths.ensure()
        silent_paths.segment(0).write_bytes(b"disposable fixture media")
        silent_paths.transcript_json.write_text(
            json.dumps({"segments": [], "recognition_state": "NO_SPEECH"}), encoding="utf-8"
        )
        (silent_paths.root / "recording_manifest.json").write_text(
            json.dumps({"status": "PARTIAL"}), encoding="utf-8"
        )
        segments = [
            {"id": i, "start": i * 2, "end": i * 2 + 1, "text": "Hello hello · نص اختبار"} for i in range(3)
        ]
        paths.transcript_json.write_text(
            json.dumps({"segments": segments, "recognition_state": "DONE"}), encoding="utf-8"
        )
        paths.transcript_segments.parent.mkdir(parents=True, exist_ok=True)
        paths.transcript_segments.write_text(json.dumps(segments), encoding="utf-8")
        paths.summary_json.write_text(
            json.dumps({"executive_summary": "بيانات اختبار معزولة؛ ليست نتائج إنتاج."}), encoding="utf-8"
        )
        audio = paths.root / "audio" / "analysis.wav"
        audio.parent.mkdir(parents=True, exist_ok=True)
        with wave.open(str(audio), "wb") as recording:
            recording.setnchannels(1)
            recording.setsampwidth(2)
            recording.setframerate(16_000)
            recording.writeframes(b"\x00\x00" * 16_000)

        class TestDiarizer:
            model_id = "fixture-only"

            def diarize(self, _audio):
                return [{"start": 0, "end": 5.5, "speaker": "fixture_voice"}]

        run_offline_diarization(
            space_id="fixture123",
            audio_path=audio,
            transcript_path=paths.transcript_segments,
            analysis_dir=paths.analysis_dir,
            diarizer=TestDiarizer(),
            duration=6,
        )
        build_space_analysis(
            paths,
            space={"space_id": "fixture123", "title": "Isolated test fixture"},
            inventory_record={"media": {"duration_seconds": 6}},
        )
        results = []
        # Creating TestClient without a context intentionally skips lifespan.
        client = TestClient(api.app)

        def serve(route):
            parts = urlsplit(route.request.url)
            if parts.path == "/spaces/record-requests/fixtureManual" and route.request.method == "GET":
                route.fulfill(
                    status=200,
                    content_type="application/json",
                    body=json.dumps(
                        {
                            "space_id": "fixtureManual",
                            "status": "queued",
                            "verification_state": "unverified",
                            "title": "مساحة تدريبية للتوثيق",
                            "host_username": "fixture_host",
                            "is_recording": False,
                        }
                    ),
                )
                return
            response = client.request(
                route.request.method, parts.path + ("?" + parts.query if parts.query else "")
            )
            route.fulfill(
                status=response.status_code,
                content_type=response.headers.get("content-type"),
                body=response.content,
            )

        with sync_playwright() as playwright:
            browser = playwright.chromium.launch(channel="chrome", headless=True)
            for section in ("manual", "library", "accounts", "processing", "analytics", "health", "settings"):
                for size, width, height, language in (
                    ("desktop", 1440, 900, "ar"),
                    ("mobile", 390, 844, "ar"),
                    ("desktop-en", 1440, 900, "en"),
                ):
                    page = browser.new_page(viewport={"width": width, "height": height})
                    if language == "en":
                        page.add_init_script("localStorage.setItem('xsa_workspace_language','en')")
                    if section == "manual":
                        page.add_init_script("localStorage.setItem('xsa_manual_last','fixtureManual')")
                    errors = []
                    page.on("pageerror", lambda error, sink=errors: sink.append(str(error)))
                    page.route("http://xsa.local/**", serve)
                    page.goto(f"http://xsa.local/workspace/{section}", wait_until="domcontentloaded")
                    page.wait_for_function("!document.querySelector('#content .skeleton')")
                    checks = {}
                    if section == "manual":
                        page.locator("#manualUrl").fill("https://x.com/i/spaces/fixtureManual")
                        page.locator("#manualRefresh").wait_for()
                        checks["request_status_visible"] = page.locator("#manualResult strong").count() == 1
                        checks["recording_actions_visible"] = page.locator("#manualForm button").count() == 2
                    if section == "accounts":
                        checks["schedule_visible"] = (
                            page.locator("#accountRows").inner_text().find("4.2s / 7") >= 0
                        )
                    if section == "analytics":
                        page.locator("#reportSpace").select_option("fixture123")
                        page.locator("#phraseQuery").wait_for()
                        page.locator("#phraseQuery").fill("hello")
                        page.locator("#phraseForm button").click()
                        page.wait_for_function(
                            "document.querySelector('#phraseResults').textContent.includes('6')"
                        )
                        checks["phrase_timed_hits"] = (
                            page.locator("#phraseResults a[href*='?t=']").count() == 3
                        )
                        checks["acoustic_speaker_visible"] = (
                            "SPEAKER_00" in page.locator("#reportBody").inner_text()
                        )
                    if section == "library":
                        rows = page.locator("#libraryRows .card")
                        checks["two_distinct_states"] = rows.count() == 2
                        page.locator("#state").select_option("ready")
                        checks["ready_requires_media_text_summary"] = (
                            page.locator("#libraryRows .card").count() == 1
                        )
                        page.locator("#state").select_option("no_speech")
                        checks["no_speech_separate"] = page.locator("#libraryRows .card").count() == 1
                        page.locator("#state").select_option("partial")
                        checks["partial_separate"] = page.locator("#libraryRows .card").count() == 1
                        page.locator("#state").select_option("")
                        page.locator("#query").fill("__not_found__")
                        checks["search_empty"] = page.locator("#libraryRows .empty").count() == 1
                        page.locator("#query").fill("")
                    page.screenshot(path=str(output / f"fixture-{section}-{size}.png"), full_page=True)
                    if section == "manual" and size == "desktop":
                        page.locator("#content .panel").screenshot(
                            path=str(output / "fixture-manual-window.png")
                        )
                    results.append(
                        {
                            "page": section,
                            "size": size,
                            "data": "disposable_test_fixtures",
                            "errors": errors,
                            "overflow": page.evaluate("document.documentElement.scrollWidth > innerWidth"),
                            "error_panel": page.locator("#content > .error").count(),
                            "checks": checks,
                        }
                    )
                    page.close()
            for name, url in (
                ("studio", "http://xsa.local/spaces/fixture123/analysis"),
                ("dashboard-player", "http://xsa.local/"),
            ):
                page = browser.new_page(viewport={"width": 390, "height": 844})
                errors = []
                page.on("pageerror", lambda error, sink=errors: sink.append(str(error)))
                page.route("http://xsa.local/**", serve)
                page.goto(url, wait_until="domcontentloaded")
                checks = {}
                if name == "studio":
                    page.wait_for_function("!document.querySelector('#title').classList.contains('loading')")
                    checks["studio_loaded"] = page.locator("#title").inner_text() != ""
                    checks["media_status_supported"] = page.evaluate(
                        "typeof ensurePlayerMedia === 'function'"
                    )
                else:
                    page.wait_for_function("typeof openVideoModal === 'function'")
                    page.evaluate("openVideoModal('fixture123','Fixture','fixture_host')")
                    page.locator("#videoModal").wait_for(state="visible")
                    checks["player_modal_opened"] = page.locator("#videoModal").is_visible()
                page.screenshot(path=str(output / f"fixture-{name}-mobile.png"), full_page=True)
                results.append(
                    {
                        "page": name,
                        "size": "mobile",
                        "data": "disposable_test_fixtures",
                        "errors": errors,
                        "overflow": page.evaluate("document.documentElement.scrollWidth > innerWidth"),
                        "error_panel": 0,
                        "checks": checks,
                    }
                )
                page.close()
            browser.close()
        from xsa.db import database

        database._engine.dispose()
        database._engine = database._SessionLocal = None
    (output / "results.json").write_text(json.dumps(results, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps(results, ensure_ascii=False))
    if any(
        row["errors"] or row["overflow"] or row["error_panel"] or not all(row["checks"].values())
        for row in results
    ):
        raise SystemExit(1)
    return results


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("output", type=Path)
    verify(parser.parse_args().output)
