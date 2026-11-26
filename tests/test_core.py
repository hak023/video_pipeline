"""네트워크/브라우저 없이 검증 가능한 핵심 로직 테스트 (표준 unittest).

실행: python -m unittest discover -s tests -v
"""
from __future__ import annotations

import json
import sys
import tempfile
import unittest
import zipfile
from contextlib import nullcontext
from pathlib import Path
from typing import Any
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import diagnostics as dg                                    # noqa: E402
import discover as discovery                                # noqa: E402
import download as downloader                                # noqa: E402
from ad_blacklist import AdBlacklistStack                    # noqa: E402
from media_classifier import classify_candidate              # noqa: E402
from media_grouping import group_candidates                  # noqa: E402

SECRET = "SUPERSECRETTOKENVALUE1234567890"


class MaskingTests(unittest.TestCase):
    def test_url_keeps_host_path_and_masks_credentials(self):
        u = f"https://cdn.example.com/hls/video/master.m3u8?token={SECRET}&quality=720&sig=abc"
        m = dg.mask_url(u)
        self.assertIn("cdn.example.com/hls/video/master.m3u8", m)
        self.assertIn("quality=720", m)
        self.assertNotIn(SECRET, m)
        self.assertNotIn("sig=abc", m)

    def test_token_like_path_segment_and_userinfo_masked(self):
        seg = "a1b2c3d4e5f60718293a4b5c6d7e8f90a1b2"
        m = dg.mask_url(f"https://user:pw@host.example/{seg}/master.m3u8")
        self.assertNotIn(seg, m)
        self.assertNotIn("user:pw", m)
        # 같은 값은 같은 태그 → 로그 간 상관 분석 가능
        self.assertEqual(dg.mask_url(f"https://h/{seg}/a"), dg.mask_url(f"https://h/{seg}/a"))

    def test_text_and_headers(self):
        t = dg.mask_text(f"GET https://h.example/x.mp4?key={SECRET} Cookie: sid=abc123")
        self.assertNotIn(SECRET, t)
        self.assertNotIn("abc123", t)
        h = dg.mask_headers({"Cookie": "sid=1", "Authorization": "Bearer x", "Referer": f"https://a/b?token={SECRET}"})
        self.assertEqual(h["cookie"], "<masked>")
        self.assertNotIn(SECRET, h["referer"])

    def test_sanitize_drops_cookies_and_request_headers(self):
        # request_headers 구조는 보존하되 내부 민감 키(cookie/authorization 등)는 제거됩니다.
        out = dg.sanitize({"cookies": [{"value": SECRET}], "request_headers": {"authorization": SECRET, "range": "bytes=0-123", "x-goog-user-project": "my-proj"},
                           "url": f"https://h/x?token={SECRET}", "n": 1})
        self.assertNotIn(SECRET, json.dumps(out))
        self.assertEqual(out["n"], 1)
        # request_headers는 보존되어야 하며, 내부 authorization은 제거됩니다.
        self.assertIn("request_headers", out)
        self.assertNotIn("authorization", out["request_headers"])
        self.assertEqual(out["request_headers"].get("range"), "bytes=0-123")
        self.assertEqual(out["request_headers"].get("x-goog-user-project"), "my-proj")
        # cookies는 항상 제거됩니다.
        self.assertNotIn("cookies", out)

    def test_candidate_view_is_allowlist(self):
        v = dg.candidate_view({"url": "https://h/x.mp4", "cookies": [{"value": SECRET}],
                               "request_headers": {"authorization": SECRET}, "user_agent": "ua", "classification": "MAIN"})
        self.assertNotIn("cookies", v)
        self.assertNotIn("request_headers", v)
        self.assertNotIn("user_agent", v)
        self.assertEqual(v["classification"], "MAIN")


class DownloadDiagnosticsTests(unittest.TestCase):
    def test_attempt_log_records_replay_values_but_not_credentials(self):
        attempts = []

        def fake_download_once(*args, **kwargs):
            kwargs["events"].append({"engine": "yt-dlp", "ok": False})
            return False, "network error"

        with (
            mock.patch.object(downloader, "validate_url"),
            mock.patch.object(downloader, "_write_cookie_file", return_value=None),
            mock.patch.object(downloader, "_download_once", side_effect=fake_download_once),
        ):
            ok, _ = downloader.download(
                {
                    "url": "https://storage.googleapis.com/bucket/video.mp4",
                    "content_length": "167382505",
                    "request_headers": {
                        "range": "bytes=0-",
                        "x-goog-user-project": "example-project",
                        "authorization": "Bearer secret-token",
                        "x-goog-api-key": "secret-api-key",
                    },
                },
                show_progress=False,
                attempt_log=attempts,
            )

        self.assertFalse(ok)
        values = attempts[0]["profile_header_values"]
        self.assertEqual(values["range"], "bytes=0-")
        self.assertEqual(values["x-goog-user-project"], "example-project")
        self.assertNotIn("authorization", values)
        self.assertNotIn("x-goog-api-key", values)


class URLTimestampTests(unittest.TestCase):
    def test_timestamp_is_read_from_early_path_segment(self):
        self.assertEqual(
            discovery._url_timestamp(
                "https://storage.googleapis.com/mediastorage/1794754864223/token/video.mp4"
            ),
            1794754864223,
        )

    def test_unrecognized_path_has_no_timestamp(self):
        self.assertIsNone(discovery._url_timestamp("https://cdn.example/video/1234.mp4"))


class DiagnosticsSessionTests(unittest.TestCase):
    def test_failed_run_still_leaves_summary_logs_and_zip_without_secrets(self):
        with tempfile.TemporaryDirectory() as td:
            d = dg.Diagnostics(td, meta={"mode": "test"})
            d.event("media_discovery", "candidate_detected", "발견", url=f"https://h/x.mp4?token={SECRET}")
            cid = d.candidate({"url": f"https://h/x.mp4?token={SECRET}", "cookies": [{"value": SECRET}],
                               "classification": "AD", "ad_score": 5})
            d.player(d.new_player_id(), embed_url=f"https://p/e?key={SECRET}", jw_ready=False)
            d.download(candidate_id=cid, status="FAILED", reason=f"HTTP 403 https://h/x?token={SECRET}")
            d.browser_page_error(f"boom {SECRET}")
            try:
                raise ValueError(f"fail https://h/x?token={SECRET}")
            except ValueError as e:
                d.exception("download", e)
            d.finalize("FAILED", "classification", "본편으로 확정된 후보가 없음")

            summary = json.loads((d.dir / "summary.json").read_text(encoding="utf-8"))
            self.assertEqual(summary["status"], "FAILED")
            self.assertEqual(summary["failure_stage"], "classification")
            self.assertEqual(summary["ad_count"], 1)
            self.assertEqual(summary["exception_count"], 1)
            for f in ("run.log", "events.jsonl", "candidates.json", "players.json", "downloads.json"):
                self.assertTrue((d.dir / f).is_file(), f)
            for line in (d.dir / "events.jsonl").read_text(encoding="utf-8").splitlines():
                json.loads(line)
            self.assertTrue(d.zip_path and d.zip_path.is_file())
            with zipfile.ZipFile(d.zip_path) as zf:
                blob = b"".join(zf.read(n) for n in zf.namelist())
            self.assertNotIn(SECRET.encode(), blob)

    def test_unwritable_log_dir_never_raises(self):
        with tempfile.TemporaryDirectory() as td:
            blocker = Path(td) / "file"
            blocker.write_text("x")
            d = dg.Diagnostics(blocker / "sub")      # 디렉터리를 만들 수 없음
            self.assertFalse(d.enabled)
            d.event("a", "b")
            d.finalize("OK")

    def test_ok_run_clears_earlier_error_failure(self):
        with tempfile.TemporaryDirectory() as td:
            d = dg.Diagnostics(td)
            d.event("download", "x", "err", level="ERROR")
            d.finalize("OK", make_zip=False)
            self.assertIsNone(json.loads((d.dir / "summary.json").read_text(encoding="utf-8"))["failure_reason"])

    def test_retention_removes_oldest_runs(self):
        with tempfile.TemporaryDirectory() as td:
            runs = Path(td) / "runs"
            for i in range(5):
                (runs / f"2026010{i}_000000_aaaaaa").mkdir(parents=True)
            dg.Diagnostics(td, retain=3)
            self.assertLessEqual(len(list(runs.iterdir())), 3)


class BlacklistTests(unittest.TestCase):
    def setUp(self):
        self.td = tempfile.TemporaryDirectory()
        self.path = Path(self.td.name) / "bl.json"
        self.bl = AdBlacklistStack(self.path)
    def tearDown(self):
        self.td.cleanup()

    def test_static_matches_hostname_only(self):
        self.assertEqual(self.bl.match("https://cdn.doubleclick.net/x.mp4")["source"], "static_hard")
        self.assertEqual(self.bl.match("https://a.saawsedge.com/x.mp4")["source"], "static_soft")
        # 게시글 slug/쿼리에 키워드가 들어 있어도 광고가 아니다
        self.assertIsNone(self.bl.match("https://site.example/ethnic-dance-popads-video/index.m3u8?ref=ethnic"))
        self.assertIsNone(self.bl.match("https://www.youtube.com/doubleclick.mp4"))

    def test_weak_evidence_alone_never_blocks(self):
        for i in range(10):
            self.bl.observe_ad("https://cdn.new.example/media/a.mp4", "short_unclassified", page_url=f"https://p/{i % 2}")
        # 페이지 2곳뿐이라 합계 조건(페이지≥3) 미충족
        self.assertIsNone(self.bl.match("https://cdn.new.example/media/b.mp4"))

    def test_repeated_weak_evidence_across_pages_activates_path_scope_only(self):
        for i in range(3):
            self.bl.observe_ad("https://cdn.new.example/promo/a.mp4", "short_unclassified", page_url=f"https://p/{i}")
        self.assertEqual(self.bl.match("https://cdn.new.example/promo/z.mp4")["source"], "dynamic")
        # 같은 CDN 의 다른 경로(본편)는 영향받지 않는다
        self.assertIsNone(self.bl.match("https://cdn.new.example/videos/main.mp4"))

    def test_strong_evidence_activates_with_two_pages(self):
        self.bl.observe_ad("https://ads.new.example/x.mp4", "ad_path_token", page_url="https://p/1", strong=True)
        self.assertIsNone(self.bl.match("https://ads.new.example/x.mp4"))
        self.bl.observe_ad("https://ads.new.example/y.mp4", "ad_path_token", page_url="https://p/2", strong=True)
        self.assertIsNotNone(self.bl.match("https://ads.new.example/x.mp4"))

    def test_confirmed_main_deactivates_and_protects(self):
        for i in range(3):
            self.bl.observe_ad("https://cdn.shared.example/a.mp4", "short_unclassified", page_url=f"https://p/{i}")
        self.assertIsNotNone(self.bl.match("https://cdn.shared.example/b.mp4"))
        self.bl.record_main("https://cdn.shared.example/b.mp4", page_url="https://p/9")
        self.assertIsNone(self.bl.match("https://cdn.shared.example/b.mp4"))
        for i in range(10, 14):
            self.bl.observe_ad("https://cdn.shared.example/c.mp4", "short_unclassified", page_url=f"https://p/{i}")
        self.assertIsNone(self.bl.match("https://cdn.shared.example/c.mp4"))
        self.assertEqual(self.bl.main_confirmed_scope("https://cdn.shared.example/c.mp4"), "host")

    def test_state_persists_and_legacy_file_is_discarded(self):
        for i in range(3):
            self.bl.observe_ad("https://cdn.persist.example/promo/a.mp4", "short_unclassified", page_url=f"https://p/{i}")
        again = AdBlacklistStack(self.path)
        self.assertIsNotNone(again.match("https://cdn.persist.example/promo/q.mp4"))

        legacy = Path(self.td.name) / "legacy.json"
        legacy.write_text(json.dumps({"cdn.poisoned.example": {"reason": "short_ad_video", "count": 9}}), encoding="utf-8")
        fresh = AdBlacklistStack(legacy)
        self.assertIsNone(fresh.match("https://cdn.poisoned.example/main.mp4"))
        self.assertTrue(legacy.with_suffix(".legacy.bak").exists())


class ClassifierTests(unittest.TestCase):
    def setUp(self):
        td = tempfile.TemporaryDirectory()
        self.addCleanup(td.cleanup)
        self.bl = AdBlacklistStack(Path(td.name) / "bl.json")

    def cls(self, **c):
        c.setdefault("url", "https://cdn.example/v/video.mp4")
        return classify_candidate(c, blacklist=self.bl, player_hosts={"player.example"})

    def test_ad_path_token_is_ad_with_evidence(self):
        r = self.cls(url="https://cdn.example/ads/spot.mp4")
        self.assertEqual(r["classification"], "AD")
        self.assertIn("AD_PATH_TOKEN", r["rules"])

    def test_known_ad_network_is_ad(self):
        self.assertEqual(self.cls(url="https://x.doubleclick.net/v.mp4")["classification"], "AD")

    def test_long_playlist_video_is_main(self):
        r = self.cls(from_playlist=True, playlist_index=0, duration=1500)
        self.assertEqual(r["classification"], "MAIN")
        self.assertTrue(r["main_evidence"])

    def test_length_or_size_alone_never_decides_ad(self):
        r = self.cls(duration=20, content_length=500_000)
        self.assertEqual(r["classification"], "PENDING")

    def test_soft_pattern_can_be_overridden_by_strong_main_evidence(self):
        url = "https://a.saawsedge.com/v/video.m3u8"
        self.assertEqual(self.cls(url=url)["classification"], "AD")
        r = self.cls(url=url, from_playlist=True, duration=2400, m3u8_kind="master")
        self.assertEqual(r["classification"], "MAIN")

    def test_hard_pattern_is_not_overridden(self):
        r = self.cls(url="https://x.doubleclick.net/v.m3u8", from_playlist=True, duration=2400, m3u8_kind="master")
        self.assertEqual(r["classification"], "AD")

    def test_unknown_candidate_is_pending_not_dropped(self):
        self.assertEqual(self.cls()["classification"], "PENDING")

    def test_weak_only_dynamic_rule_cannot_make_a_long_video_ad(self):
        for i in range(3):
            self.bl.observe_ad("https://cdn.site.example/videos/clip.mp4", "short_unclassified", page_url=f"https://p/{i}")
        r = self.cls(url="https://cdn.site.example/videos/full_episode.mp4", content_length=900_000_000)
        self.assertEqual(r["classification"], "PENDING")
        self.assertIn("AD_DYNAMIC_WEAK", r["rules"])

    def test_strong_dynamic_rule_still_makes_ad(self):
        for i in range(2):
            self.bl.observe_ad("https://ads2.site.example/x/a.mp4", "ad_path_token", page_url=f"https://p/{i}", strong=True)
        self.assertEqual(self.cls(url="https://ads2.site.example/x/b.mp4")["classification"], "AD")


class GroupingTests(unittest.TestCase):
    def c(self, url, **kw):
        return {"url": url, "classification": kw.pop("classification", "MAIN"), "candidate_id": url[-12:], **kw}

    def test_hls_master_children_form_one_group_and_only_master_is_attempted(self):
        cands = [
            self.c("https://c.example/v1/master.m3u8", m3u8_kind="master", content_type="application/x-mpegurl"),
            self.c("https://c.example/v1/720p.m3u8", m3u8_kind="media", content_type="application/x-mpegurl"),
            self.c("https://c.example/v1/1080p.m3u8", m3u8_kind="media", content_type="application/x-mpegurl"),
        ]
        groups = group_candidates(cands)
        self.assertEqual(len(groups), 1)
        self.assertEqual([a["url"] for a in groups[0]["attempts"]], ["https://c.example/v1/master.m3u8"])
        self.assertEqual(len(groups[0]["members"]), 3)

    def test_quality_variants_merge_and_prefer_higher_resolution(self):
        groups = group_candidates([self.c("https://c.example/f/movie_480p.mp4"), self.c("https://c.example/f/movie_1080p.mp4")])
        self.assertEqual(len(groups), 1)
        self.assertTrue(groups[0]["attempts"][0]["url"].endswith("movie_1080p.mp4"))

    def test_different_videos_stay_separate_in_playlist_order(self):
        cands = [
            self.c("https://c.example/b/ep2.mp4", from_playlist=True, playlist_index=1, player_id="p", embed_url="e"),
            self.c("https://c.example/a/ep1.mp4", from_playlist=True, playlist_index=0, player_id="p", embed_url="e"),
        ]
        groups = group_candidates(cands)
        self.assertEqual(len(groups), 2)
        self.assertTrue(groups[0]["representative"]["url"].endswith("ep1.mp4"))

    def test_same_playlist_item_sources_merge(self):
        cands = [
            self.c("https://c.example/x/a.m3u8", from_playlist=True, playlist_index=0, player_id="p", embed_url="e"),
            self.c("https://c.example/y/b.mp4", from_playlist=True, playlist_index=0, player_id="p", embed_url="e"),
        ]
        self.assertEqual(len(group_candidates(cands)), 1)

    def test_pending_group_label(self):
        g = group_candidates([self.c("https://c.example/a.mp4", classification="PENDING")])
        self.assertEqual(g[0]["classification"], "PENDING")

    def test_same_file_via_redirect_host_is_one_video(self):
        a = self.c("https://archive.org/download/x/Content/movie_720p.mp4", content_length=61878609, source="dom")
        b = self.c("https://dn1.ca.archive.org/0/items/x/Content/movie_720p.mp4", content_length=61878609,
                   classification="PENDING", source="network")
        g = group_candidates([a, b])
        self.assertEqual(len(g), 1)
        self.assertIn("SAME_FILE", g[0]["reasons"])
        # 용량이 다르면 같은 파일명이어도 다른 영상으로 둔다
        c = self.c("https://other.example/a/movie_720p.mp4", content_length=123)
        self.assertEqual(len(group_candidates([a, c])), 2)

    def test_two_masters_in_same_directory_are_different_videos(self):
        m = dict(content_type="application/x-mpegurl", m3u8_kind="master")
        cands = [
            self.c("https://cdn.a.com/hls/ep1_master.m3u8", playlist_index=0, player_id="p", embed_url="e", **m),
            self.c("https://cdn.a.com/hls/ep2_master.m3u8", playlist_index=1, player_id="p", embed_url="e", **m),
        ]
        self.assertEqual(len(group_candidates(cands)), 2)
        # playlist 번호가 없어도 master 끼리는 합치지 않는다
        no_idx = [self.c("https://cdn.a.com/hls/a_master.m3u8", **m), self.c("https://cdn.a.com/hls/b_master.m3u8", **m)]
        self.assertEqual(len(group_candidates(no_idx)), 2)


class MaskedReasonTests(unittest.TestCase):
    def test_short_reason_masks_signed_url_in_ytdlp_error(self):
        import main
        reason = main._short_reason(f"[DIAG HTTP 403]\nERROR: Unsupported URL: https://x.ssrr.org/stream/abc?token={SECRET}")
        self.assertNotIn(SECRET, reason)


class HistoryCooldownTests(unittest.TestCase):
    def test_cooldown_after_repeated_failures_only(self):
        from history import HistoryManager
        with tempfile.TemporaryDirectory() as td:
            h = HistoryManager(Path(td) / "h")
            url = "https://s.example/post-a/"
            self.assertFalse(h.in_cooldown(url, 24))
            h.record_failed(url, "a", "no_media_found")
            self.assertFalse(h.in_cooldown(url, 24, min_attempts=2))     # 1회 실패 → 보류 안 함
            h.record_failed(url, "a", "no_media_found")
            self.assertTrue(h.in_cooldown(url, 24, min_attempts=2))
            self.assertFalse(h.in_cooldown(url, 0))                       # 옵션 꺼짐
            h.record_success(url, "a", "x.mp4")
            self.assertFalse(h.in_cooldown(url, 24))                      # 성공하면 해제

    def test_old_failure_is_not_in_cooldown(self):
        from history import HistoryManager
        with tempfile.TemporaryDirectory() as td:
            h = HistoryManager(Path(td) / "h")
            url = "https://s.example/post-b/"
            h.record_failed(url, "b", "x")
            h.record_failed(url, "b", "x")
            with mock.patch("history.datetime") as dt:
                from datetime import datetime as real, timedelta
                dt.now.return_value = real.now() + timedelta(hours=30)
                dt.fromisoformat.side_effect = real.fromisoformat
                self.assertFalse(h.in_cooldown(url, 24))


class UrlNameTests(unittest.TestCase):
    def test_name_from_url(self):
        import main
        self.assertEqual(main._name_from_url("https://s.example/post/my-video/"), "my-video")
        self.assertEqual(main._name_from_url("https://cdn.example/a/x36xhzz.m3u8?t=1"), "x36xhzz")
        self.assertEqual(main._name_from_url("https://s.example/%ec%a3%bc%ed%95%98-261009/"), "주하-261009")
        self.assertEqual(main._name_from_url("https://s.example/"), "video")
        self.assertEqual(main._name_from_url("https://s.example/ver1.5"), "ver1.5")


class AnalyzeRunTests(unittest.TestCase):
    def test_analyze_reads_zip_and_gives_hints(self):
        import analyze_run
        with tempfile.TemporaryDirectory() as td:
            d = dg.Diagnostics(td)
            d.player(d.new_player_id(), jw_present=True, jw_ready=False, play_result="no_stream_after_play", captured=0)
            d.candidate({"url": "https://a.saawsedge.com/v.m3u8", "classification": "AD", "rules": ["AD_STATIC_SOFT"]})
            d.finalize("FAILED", "classification", "본편 없음")
            text = analyze_run.render(analyze_run.RunData(d.zip_path))
            self.assertIn("FAILED", text)
            self.assertIn("SOFT/동적 규칙", text)
            self.assertNotIn("Traceback", text)


class VerifyTests(unittest.TestCase):
    def test_missing_probe_tools_does_not_condemn_a_good_file(self):
        import verify as v
        with tempfile.TemporaryDirectory() as td:
            f = Path(td) / "a.mp4"
            f.write_bytes(b"x" * 100)
            with mock.patch.object(v, "probe_tools_available", return_value=False):
                ok, meta = v.verify(f)
            self.assertTrue(ok)
            self.assertIn("unverified", meta)

    def test_probe_failure_with_tools_present_is_invalid(self):
        import verify as v
        with tempfile.TemporaryDirectory() as td:
            f = Path(td) / "a.mp4"
            f.write_bytes(b"x" * 100)
            with mock.patch.object(v, "probe_tools_available", return_value=True), \
                 mock.patch.object(v, "probe_duration_size", return_value=None):
                ok, _ = v.verify(f)
            self.assertFalse(ok)


class FailClosedAndDiagnosticsTests(unittest.TestCase):
    def test_fail_closed_skips_unconfirmed_pending_by_default(self):
        import main
        group = {
            "index": 1,
            "embed_url": "https://player.example/embed/123",
            "candidates": [
                {"url": "https://cdn.example/video.mp4", "classification": "PENDING", "candidate_id": "c1"},
            ],
            "low_confidence": True,
        }
        opts = main.Options(allow_pending=False)
        with mock.patch("main.get_diagnostics"):
            res = main._process_item(
                group=group, n=1, url="https://site.example/post/1", name="test_post",
                out_dir=Path("."), proxy=None, opts=opts, history=None, post_date=None,
                force=False, board=None, slot=1,
            )
        self.assertEqual(res["status"], "skipped_unconfirmed")
        self.assertTrue(res.get("fatal"))
        self.assertIn("fail-closed", res.get("reason", ""))

    def test_fail_closed_allows_pending_when_flag_enabled(self):
        import main
        group = {
            "index": 1,
            "embed_url": "https://player.example/embed/123",
            "candidates": [
                {"url": "https://cdn.example/video.mp4", "classification": "PENDING", "candidate_id": "c1"},
            ],
            "low_confidence": True,
        }
        opts = main.Options(allow_pending=True)
        with mock.patch("main.get_diagnostics"), \
             mock.patch("main._download_candidate", return_value={"status": "ok", "output": "test.mp4", "meta": {}}):
            res = main._process_item(
                group=group, n=1, url="https://site.example/post/1", name="test_post",
                out_dir=Path("."), proxy=None, opts=opts, history=None, post_date=None,
                force=False, board=None, slot=1,
            )
        self.assertEqual(res["status"], "ok")

    def test_no_media_found_marks_fatal_to_avoid_useless_retries(self):
        import main
        group = {
            "index": 1,
            "embed_url": "https://player.example/embed/123",
            "candidates": [],
            "debug": {},
        }
        opts = main.Options()
        with mock.patch("main.get_diagnostics"):
            res = main._process_item(
                group=group, n=1, url="https://site.example/post/1", name="test_post",
                out_dir=Path("."), proxy=None, opts=opts, history=None, post_date=None,
                force=False, board=None, slot=1,
            )
        self.assertEqual(res["status"], "no_media_found")
        self.assertTrue(res.get("fatal"))

    def test_fill_page_debug_separates_wrapper_and_embed_frame(self):
        import discover
        page = mock.MagicMock()
        page.url = "about:blank"
        page.title.return_value = ""
        page.inner_text.return_value = ""

        main_fr = mock.MagicMock()
        page.main_frame = main_fr

        embed_fr = mock.MagicMock()
        embed_fr.url = "https://player.example/embed.html"
        embed_fr.title.return_value = "Player Title"
        embed_fr.inner_text.return_value = "Player Body Content"
        embed_fr.evaluate.return_value = "complete"

        page.frames = [main_fr, embed_fr]

        debug = {}
        discover._fill_page_debug(page, debug, embed_url="https://player.example/embed.html")

        self.assertIn("wrapper", debug)
        self.assertEqual(debug["wrapper"]["url"], "about:blank")
        self.assertIn("embed_frame", debug)
        self.assertEqual(debug["embed_frame"]["url"], "https://player.example/embed.html")
        self.assertEqual(debug["embed_frame"]["title"], "Player Title")
        self.assertEqual(debug["embed_frame"]["readyState"], "complete")
        self.assertEqual(debug["title"], "Player Title")

    def test_find_embed_frame_reports_detailed_reason_on_failure(self):
        import discover
        page = mock.MagicMock()
        page.url = "https://site.example/post/1"
        page.locator.return_value.all.return_value = []

        debug = {}
        frame, point = discover._find_embed_frame(page, "https://player.example/embed", debug=debug)
        self.assertIsNone(frame)
        self.assertIsNone(point)
        self.assertEqual(debug["iframe_match"]["reason_detail"], "dom_no_iframes")


class PlaybackStreamingProgressTests(unittest.TestCase):
    def test_stream_error_recovers_in_player_frame_without_loading_finished(self):
        import base64
        import playback_capture

        with tempfile.TemporaryDirectory() as directory:
            capture = playback_capture.PlaybackCapture(Path(directory))
            session = mock.Mock()
            session.send.side_effect = RuntimeError("Request with the provided ID does not exists")
            capture._wire_session(session)
            sid = id(session)
            url = "https://cdn.test/video.mp4"
            frame = mock.Mock()
            frame.evaluate.side_effect = [
                {"status": 206, "content_range": "bytes 0-3/8", "body": base64.b64encode(b"abcd").decode()},
                {"status": 206, "content_range": "bytes 4-7/8", "body": base64.b64encode(b"efgh").decode()},
            ]
            request = mock.Mock(url=url, resource_type="media", headers={"range": "bytes=0-"}, method="GET",
                                frame=frame)
            response = mock.Mock(url=url, status=206, request=request,
                                 headers={"content-type": "video/mp4", "content-range": "bytes 0-7/8"})
            with mock.patch.object(capture, "_start_direct_media_download"), \
                    mock.patch.object(playback_capture, "BROWSER_FALLBACK_CHUNK_BYTES", 4):
                capture._on_response(response)
                capture._on_cdp_request(sid, {"requestId": "1", "type": "Media", "request": {
                    "url": url, "urlFragment": "#real=SECRET_SOURCE", "method": "GET",
                    "headers": {"Range": "bytes=0-"},
                }})
                capture._on_cdp_response(sid, {
                    "requestId": "1", "type": "Media",
                    "response": {"url": url, "status": 206, "mimeType": "video/mp4",
                                 "headers": {"content-range": "bytes 0-7/8"}},
                })
                capture.drain()
                frame.evaluate.assert_not_called()
                self.assertFalse(capture.reload_untracked_player())
                capture.drain(allow_browser_fallback=True)
                self.assertFalse(capture.media_progress()["complete"])
                capture.drain(allow_browser_fallback=True)
            result = capture.summary()
            self.assertTrue(result["complete"])
            self.assertEqual(Path(result["output"]).read_bytes(), b"abcdefgh")
            # 서비스워커가 영상을 구분하는 fragment는 요청에는 붙이고 기록에는 남기지 않는다.
            self.assertEqual(frame.evaluate.call_args_list[0].args[1], [url + "#real=SECRET_SOURCE", 0, 3])
            self.assertEqual(frame.evaluate.call_args_list[1].args[1], [url + "#real=SECRET_SOURCE", 4, 7])
            self.assertNotIn("SECRET_SOURCE", (Path(directory) / "playback_capture.json").read_text(encoding="utf-8"))

    def test_untracked_media_request_reloads_player_once_then_uses_dom_fragment(self):
        import base64
        import playback_capture

        with tempfile.TemporaryDirectory() as directory:
            capture = playback_capture.PlaybackCapture(Path(directory))
            session = mock.Mock()
            session.send.side_effect = RuntimeError("Request with the provided ID does not exists")
            capture._wire_session(session)
            sid = id(session)
            frame = mock.Mock()
            frame.evaluate.side_effect = [
                None,
                "#real=SECRET_SOURCE",
                {"status": 206, "content_range": "bytes 0-7/8", "body": base64.b64encode(b"abcdefgh").decode()},
            ]

            def untracked_response(request_id, url):
                request = mock.Mock(url=url, resource_type="media", headers={}, method="GET", frame=frame)
                capture._on_response(mock.Mock(url=url, status=206, request=request, headers={
                    "content-type": "video/mp4", "content-range": "bytes 0-7/8"}))
                capture._on_cdp_response(sid, {"requestId": request_id, "type": "Media", "response": {
                    "url": url, "status": 206, "mimeType": "video/mp4",
                    "headers": {"content-range": "bytes 0-7/8"},
                }})
                capture.drain()

            with mock.patch.object(capture, "_start_direct_media_download"):
                untracked_response("1", "https://cdn.test/first.mp4")
                self.assertTrue(capture.reload_untracked_player())
                self.assertIn("location.reload", frame.evaluate.call_args_list[0].args[0])
                capture.drain(allow_browser_fallback=True)
                self.assertEqual(frame.evaluate.call_count, 1)

                untracked_response("2", "https://cdn.test/second.mp4")
                self.assertFalse(capture.reload_untracked_player())
                capture.drain(allow_browser_fallback=True)
            result = capture.summary()
            self.assertTrue(result["complete"], result)
            self.assertEqual(frame.evaluate.call_args_list[2].args[1],
                             ["https://cdn.test/second.mp4#real=SECRET_SOURCE", 0, 7])
            self.assertNotIn("SECRET_SOURCE", json.dumps(result["capture_trace"]))

    def test_stream_recovery_rejects_mismatched_range(self):
        import base64
        import playback_capture

        with tempfile.TemporaryDirectory() as directory:
            capture = playback_capture.PlaybackCapture(Path(directory))
            session = mock.Mock()
            session.send.side_effect = RuntimeError("stream unavailable")
            capture._wire_session(session)
            sid = id(session)
            url = "https://cdn.test/video.mp4"
            frame = mock.Mock()
            frame.evaluate.return_value = {
                "status": 206, "content_range": "bytes 4-7/8", "body": base64.b64encode(b"abcd").decode(),
            }
            request = mock.Mock(url=url, resource_type="media", headers={}, method="GET", frame=frame)
            response = mock.Mock(url=url, status=206, request=request,
                                 headers={"content-type": "video/mp4", "content-range": "bytes 0-7/8"})
            with mock.patch.object(capture, "_start_direct_media_download"), \
                    mock.patch.object(playback_capture, "BROWSER_FALLBACK_CHUNK_BYTES", 4):
                capture._on_response(response)
                capture._on_cdp_request(sid, {"requestId": "1", "type": "Media", "request": {
                    "url": url, "method": "GET", "headers": {}}})
                capture._on_cdp_response(sid, {"requestId": "1", "type": "Media", "response": {
                    "url": url, "status": 206, "mimeType": "video/mp4",
                    "headers": {"content-range": "bytes 0-7/8"},
                }})
                capture.drain(allow_browser_fallback=True)
                capture.drain(allow_browser_fallback=True)
            self.assertEqual(capture.files, [])
            self.assertEqual(frame.evaluate.call_count, 1)
            self.assertTrue(any(entry["event"] == "browser_stream_error" for entry in capture.summary()["capture_trace"]))
            # 대체 수신까지 실패하면 90초를 채우지 않고 끝낸다. 받은 바이트가 있으면 계속 기다린다.
            import openx_handoff
            progress = capture.media_progress()
            state = dict(quiet=10, complete=False, covered=0, total=0, media_seen=2, inflight=0,
                         recovery_failed=progress["recovery_failed"])
            self.assertEqual(openx_handoff.streaming_stop_reason(**state), "recovery_failed")
            self.assertEqual(openx_handoff.streaming_stop_reason(**state, stream_bytes=1), "")
            self.assertIn("browser_stream:", " ".join(progress["errors"]))

    def test_partial_direct_failure_preserves_completed_browser_ranges(self):
        import playback_capture

        with tempfile.TemporaryDirectory() as directory:
            capture = playback_capture.PlaybackCapture(Path(directory))
            url = "https://cdn.test/video.mp4"
            capture._direct_download_url = url
            capture._direct_path = Path(directory) / "media_stream.mp4"
            capture._direct_total = 8
            capture._store(url, 206, "video/mp4", "bytes 0-3/8", "bytes=0-3", b"abcd", "media")
            capture._store(url, 206, "video/mp4", "bytes 4-7/8", "bytes=4-7", b"efgh", "media")
            response = mock.MagicMock(status=206, headers={"Content-Range": "bytes 0-7/8"})
            response.read.side_effect = [b"ab", OSError("connection lost")]
            with mock.patch("urllib.request.urlopen") as urlopen:
                urlopen.return_value.__enter__.return_value = response
                capture._run_direct_download(url, "bytes 0-7/8", "video/mp4", capture._direct_path, "")
            result = capture.summary()
            self.assertTrue(result["complete"], result)
            self.assertEqual(Path(result["output"]).read_bytes(), b"abcdefgh")

    def test_download_button_poll_timeouts_are_not_logged_as_errors(self):
        import openx_handoff

        page = mock.Mock()
        locator = page.frame_locator.return_value.locator.return_value.first
        clock = [0.0]

        def wait_for(**kwargs):
            clock[0] += kwargs["timeout"] / 1000
            raise TimeoutError("button not present yet")

        locator.wait_for.side_effect = wait_for
        with mock.patch("time.monotonic", side_effect=lambda: clock[0]), \
                mock.patch.object(openx_handoff, "log_event") as log_event:
            with self.assertRaises(TimeoutError):
                openx_handoff._wait_for_download_button(page, 1200)
        self.assertFalse(any(call.args[1] == "page_wait_failed" for call in log_event.call_args_list))

    def test_trace_is_bounded_and_errors_remain_in_event_log(self):
        import playback_capture

        with tempfile.TemporaryDirectory() as directory:
            capture = playback_capture.PlaybackCapture(Path(directory))
            with mock.patch.object(playback_capture, "log_event") as log_event:
                for index in range(201):
                    capture._trace("cdp_request", request_id=str(index))
                capture._trace("cdp_stream_error", request_id="last", error="failed")
                summary = capture.summary()
            self.assertEqual(len(summary["capture_trace"]), 200)
            self.assertEqual(summary["capture_trace_dropped"], 2)
            self.assertEqual(summary["capture_trace"][0]["request_id"], "2")
            self.assertTrue(any(call.args[:2] == ("playback_capture", "trace_cdp_stream_error")
                                for call in log_event.call_args_list))

    def test_trace_correlates_cdp_errors_and_masks_secrets_in_persisted_logs(self):
        import playback_capture

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            diag = dg.Diagnostics(root / "logs", run_id="capture-test", retain=0)
            capture = playback_capture.PlaybackCapture(root / "capture")
            session = mock.Mock()
            sid = id(session)
            url = "https://cdn.test/video.mp4?token=SECRET_TOKEN"
            with mock.patch.object(dg, "_current", diag):
                capture._wire_session(session)
                capture._on_cdp_request(sid, {
                    "requestId": "123", "type": "Media",
                    "request": {"url": url, "method": "GET", "headers": {"Range": "bytes=0-3"}},
                })
                capture._on_cdp_response(sid, {
                    "requestId": "123", "type": "Media",
                    "response": {"url": url, "status": 206, "mimeType": "video/mp4",
                                 "headers": {"content-range": "bytes 0-3/4"}},
                })
                session.send.side_effect = RuntimeError(f"Protocol error: {url}")
                capture.drain()
                capture._on_cdp_data(sid, {"requestId": "123", "dataLength": 4})
                capture._on_cdp_failed(sid, {
                    "requestId": "123", "errorText": "net::ERR_ABORTED", "canceled": True,
                })
                summary = capture.summary()
            trace = summary["capture_trace"]
            relevant = [entry for entry in trace if entry.get("request_id") == "123"]
            self.assertEqual([entry["event"] for entry in relevant],
                             ["cdp_request", "cdp_response", "cdp_stream_error", "cdp_loading_failed"])
            self.assertTrue(all(entry["session_id"] == sid for entry in relevant))
            self.assertEqual(relevant[1]["status"], 206)
            self.assertEqual(relevant[-1]["network_data_bytes"], 4)
            self.assertEqual(relevant[-1]["payload_bytes"], 0)
            self.assertEqual(relevant[-1]["error"], "net::ERR_ABORTED")
            events = (diag.dir / "events.jsonl").read_text(encoding="utf-8")
            manifest = (capture.out_dir / "playback_capture.json").read_text(encoding="utf-8")
            self.assertIn("trace_cdp_stream_error", events)
            self.assertNotIn("SECRET_TOKEN", events)
            self.assertNotIn("SECRET_TOKEN", manifest)
            self.assertNotIn("SECRET_TOKEN", json.dumps(trace))

    def test_body_error_is_recorded_even_when_fallback_succeeds(self):
        import playback_capture

        with tempfile.TemporaryDirectory() as directory:
            capture = playback_capture.PlaybackCapture(Path(directory))
            session = mock.Mock()
            session.send.side_effect = RuntimeError("response body unavailable")
            item = {"session": session, "sid": 42, "request_id": "id", "url": "https://cdn.test/video.mp4",
                    "status": 206, "content_type": "video/mp4", "content_range": "bytes 0-3/4",
                    "request_range": "bytes=0-3", "resource_type": "media", "claim": ""}
            with mock.patch.object(capture, "_fetch_media_direct", return_value=b"data"):
                capture._read_cdp_body(item)
            entries = capture.summary()["capture_trace"]
            self.assertEqual([entry["event"] for entry in entries], ["cdp_body_error", "cdp_body_result"])
            self.assertEqual(entries[0]["error"], "response body unavailable")
            self.assertEqual(entries[1]["source"], "fallback")
            self.assertEqual(entries[1]["bytes"], 4)

    def test_browser_fallback_uses_valid_range_after_direct_failure(self):
        import base64
        import playback_capture

        with tempfile.TemporaryDirectory() as directory:
            capture = playback_capture.PlaybackCapture(Path(directory))
            capture._page = mock.Mock()
            capture._page.is_closed.return_value = False
            capture._page.evaluate.return_value = {
                "body": base64.b64encode(b"abcd").decode(), "status": 206, "range": "bytes 0-3/8",
            }
            with mock.patch("urllib.request.urlopen", side_effect=OSError("connection failed")):
                body = capture._fetch_media_direct("https://cdn.test/video.mp4", "bytes 0-3/8")
            self.assertEqual(body, b"abcd")
            self.assertEqual(capture._page.evaluate.call_args.args[1][1], "bytes=0-3")

    def test_failed_request_without_response_does_not_start_direct_download(self):
        import playback_capture

        capture = playback_capture.PlaybackCapture(Path("unused"))
        with mock.patch.object(capture, "_start_direct_media_download") as start:
            capture._on_cdp_request(1, {
                "requestId": "1", "type": "Media",
                "request": {"url": "https://cdn.test/video.mp4", "headers": {}},
            })
            capture._on_cdp_failed(1, {"requestId": "1", "errorText": "net::ERR_ABORTED"})
        start.assert_not_called()

    def test_partial_direct_download_is_not_complete_when_length_unknown(self):
        import playback_capture

        capture = playback_capture.PlaybackCapture(Path("unused"))
        capture._direct_download_url = "https://cdn.test/video.mp4"
        capture._direct_bytes = 12
        capture._direct_total = 0
        self.assertFalse(capture.media_progress()["complete"])

    def test_direct_download_requires_a_complete_range_from_zero(self):
        import playback_capture

        for response_range, complete in (("bytes 0-3/4", True), ("bytes 2-3/4", False)):
            with self.subTest(response_range=response_range), tempfile.TemporaryDirectory() as directory:
                capture = playback_capture.PlaybackCapture(Path(directory))
                url = "https://cdn.test/video.mp4"
                capture._direct_download_url = url
                capture._direct_total = 4
                capture._direct_path = Path(directory) / "media_stream.mp4"
                response = mock.MagicMock()
                response.status = 206
                response.headers = {"Content-Range": response_range}
                response.read.side_effect = [b"abcd", b""]
                response.__enter__.return_value = response
                with mock.patch("urllib.request.urlopen", return_value=response):
                    capture._run_direct_download(url, "bytes 0-3/4", "video/mp4",
                                                 capture._direct_path, "")
                self.assertEqual(capture.media_progress()["complete"], complete)
                self.assertEqual(bool(capture.assemble()), complete)

    def test_direct_download_resumes_from_received_offset_after_read_error(self):
        import playback_capture

        with tempfile.TemporaryDirectory() as directory:
            capture = playback_capture.PlaybackCapture(Path(directory))
            url = "https://cdn.test/video.mp4"
            capture._direct_download_url = url
            capture._direct_path = Path(directory) / "media_stream.mp4"

            def response(content_range, chunks):
                resp = mock.MagicMock(status=206, headers={"Content-Range": content_range})
                resp.read.side_effect = chunks
                resp.__enter__.return_value = resp
                return resp

            first = response("bytes 0-7/8", [b"abcd", OSError("read timed out")])
            second = response("bytes 4-7/8", [b"efgh", b""])
            with mock.patch("urllib.request.urlopen", side_effect=[first, second]) as urlopen:
                capture._run_direct_download(url, "bytes 0-7/8", "video/mp4", capture._direct_path, "")
            self.assertEqual(urlopen.call_args.args[0].get_header("Range"), "bytes=4-")
            self.assertEqual(capture.direct_state(), "complete")
            self.assertEqual(capture._direct_path.read_bytes(), b"abcdefgh")
            self.assertTrue(capture.media_progress()["complete"])

    def test_cdp_stream_waits_while_direct_download_owns_the_url(self):
        import playback_capture

        with tempfile.TemporaryDirectory() as directory:
            capture = playback_capture.PlaybackCapture(Path(directory))
            session = mock.Mock()
            session.send.return_value = {}
            capture._wire_session(session)
            sid = id(session)
            url = "https://cdn.test/video.mp4"
            capture._direct_download_url = url
            capture._direct_state = "streaming"
            for request_id in ("1", "2"):
                capture._on_cdp_request(sid, {"requestId": request_id, "type": "Media",
                                              "request": {"url": url, "headers": {"Range": "bytes=0-"}}})
                capture._on_cdp_response(sid, {"requestId": request_id, "type": "Media", "response": {
                    "url": url, "status": 206, "mimeType": "video/mp4",
                    "headers": {"content-range": "bytes 0-7/8"}}})
            session.send.reset_mock()
            capture._on_cdp_finished(session, sid, {"requestId": "1"})
            with mock.patch.object(capture, "_fetch_media_direct") as fetch:
                capture.drain()
            fetch.assert_not_called()
            sent = [call.args[0] for call in session.send.call_args_list]
            self.assertNotIn("Network.streamResourceContent", sent)
            self.assertNotIn("Network.getResponseBody", sent)
            self.assertEqual(capture.skipped_count, 0)

            capture._direct_state = "failed"
            capture.drain()
            streamed = [call.args[1]["requestId"] for call in session.send.call_args_list
                        if call.args[0] == "Network.streamResourceContent"]
            self.assertEqual(streamed, ["2"])

    def test_late_stream_is_stored_at_its_real_offset_and_queues_one_reload(self):
        import base64
        import playback_capture

        with tempfile.TemporaryDirectory() as directory:
            capture = playback_capture.PlaybackCapture(Path(directory))
            session = mock.Mock()
            # Chrome 은 응답이 커지면 스트림 시작 전 버퍼를 버리고 빈 bufferedData 를 돌려준다.
            session.send.return_value = {"bufferedData": ""}
            capture._wire_session(session)
            sid = id(session)
            url = "https://cdn.test/video.mp4"
            frame = mock.Mock()
            capture._media_frames[url] = frame
            with mock.patch.object(capture, "_start_direct_media_download"):
                capture._on_cdp_request(sid, {"requestId": "1", "type": "Media",
                                              "request": {"url": url, "headers": {"Range": "bytes=0-"}}})
                capture._on_cdp_response(sid, {"requestId": "1", "type": "Media", "response": {
                    "url": url, "status": 206, "mimeType": "video/mp4",
                    "headers": {"content-range": "bytes 0-9/10"}}})
                capture._on_cdp_data(sid, {"requestId": "1", "dataLength": 6})
                capture.drain()
                capture._on_cdp_data(sid, {"requestId": "1", "dataLength": 4,
                                           "data": base64.b64encode(b"ghij").decode()})
                capture._on_cdp_finished(session, sid, {"requestId": "1"})
                capture.drain()
            self.assertNotIn("Network.getResponseBody", [call.args[0] for call in session.send.call_args_list])
            self.assertEqual([(item["content_range"], item["status"]) for item in capture.files],
                             [("bytes 6-9/10", 206)])
            self.assertEqual(Path(capture.files[0]["path"]).read_bytes(), b"ghij")
            self.assertFalse(capture.media_progress()["complete"])
            self.assertFalse(capture.reload_untracked_player())
            capture.drain()
            self.assertTrue(capture.reload_untracked_player())
            frame.evaluate.assert_called_once()
            self.assertFalse(capture.reload_untracked_player())

    def test_large_cdp_body_is_not_requested_with_get_response_body(self):
        import playback_capture

        with tempfile.TemporaryDirectory() as directory:
            capture = playback_capture.PlaybackCapture(Path(directory))
            session = mock.Mock()
            size = playback_capture.CDP_BODY_MAX_BYTES + 1
            item = {"session": session, "sid": 42, "request_id": "id", "url": "https://cdn.test/video.mp4",
                    "status": 206, "content_type": "video/mp4", "content_range": f"bytes 0-{size - 1}/{size}",
                    "request_range": "bytes=0-", "resource_type": "media", "claim": ""}
            with mock.patch.object(capture, "_fetch_media_direct", return_value=b"") as fetch:
                capture._read_cdp_body(item)
            session.send.assert_not_called()
            fetch.assert_called_once()
            events = [entry["event"] for entry in capture.summary()["capture_trace"]]
            self.assertIn("cdp_body_skipped", events)

    def test_player_pauses_during_direct_download_and_resumes_when_it_fails(self):
        import openx_handoff

        capture = mock.Mock(media_seen=1, tap_inflight=0, tap_bytes=0, stop_reason="", cache_error="")
        capture.media_progress.return_value = {"covered": 0, "total": 0, "complete": False}
        capture.direct_url.return_value = "https://cdn.test/video.mp4"
        states = iter(["streaming"] * 3 + ["failed"] * 100)
        capture.direct_state.side_effect = lambda: next(states)
        page = mock.Mock()
        clock = [0.0]
        page.wait_for_timeout.side_effect = lambda ms: clock.__setitem__(0, clock[0] + ms / 1000)
        with mock.patch.object(openx_handoff, "_pause_player_for_direct", return_value=1) as pause, \
                mock.patch.object(openx_handoff, "_prime_embed_player") as prime, \
                mock.patch.object(openx_handoff, "log"), \
                mock.patch("time.monotonic", side_effect=lambda: clock[0]):
            openx_handoff._capture_until_full(page, capture, max_seconds=8, max_duration_seconds=0)
        pause.assert_called_once()
        prime.assert_called_once()

    def test_preflight_is_not_treated_as_a_media_body(self):
        import playback_capture

        with tempfile.TemporaryDirectory() as directory:
            capture = playback_capture.PlaybackCapture(Path(directory))
            session = mock.Mock()
            capture._wire_session(session)
            session.reset_mock()
            sid = id(session)
            url = "https://storage.googleapis.com/bucket/video.mp4"
            capture._on_cdp_request(sid, {
                "requestId": "preflight", "type": "Preflight",
                "request": {"url": url, "method": "OPTIONS", "headers": {}},
            })
            capture._on_cdp_response(sid, {
                "requestId": "preflight", "type": "Preflight",
                "response": {"url": url, "status": 200, "mimeType": "text/html", "headers": {}},
            })
            capture.drain()
            capture._on_cdp_finished(session, sid, {"requestId": "preflight"})
            capture.drain()
            session.send.assert_not_called()
            self.assertEqual(capture.media_seen, 0)
            self.assertEqual(capture.skipped_count, 0)

    def test_cdp_failure_details_are_recorded_and_masked(self):
        import playback_capture

        with tempfile.TemporaryDirectory() as directory:
            capture = playback_capture.PlaybackCapture(Path(directory))
            session = mock.Mock()
            capture._wire_session(session)
            sid = id(session)
            url = "https://cdn.test/video.mp4"
            capture._on_cdp_request(sid, {
                "requestId": "1", "type": "Media", "request": {"url": url, "headers": {}},
            })
            capture._on_cdp_response(sid, {
                "requestId": "1", "type": "Media",
                "response": {"url": url, "status": 206, "mimeType": "video/mp4", "headers": {}},
            })
            session.send.side_effect = RuntimeError(
                "Protocol error: stream unavailable https://cdn.test/file?token=SECRET_TOKEN"
            )
            capture.drain()
            self.assertIn("Protocol error: stream unavailable", capture.cache_error)
            self.assertNotIn("SECRET_TOKEN", capture.cache_error)
            capture._on_cdp_failed(sid, {
                "requestId": "1", "errorText": "net::ERR_ABORTED", "canceled": True,
            })
            self.assertEqual(capture.skipped[0]["status"], 206)
            self.assertIn("net::ERR_ABORTED", capture.skipped[0]["detail"])
            self.assertIn("canceled=True", capture.skipped[0]["detail"])

    def test_streamed_chunks_are_counted_before_response_finishes(self):
        import base64
        import playback_capture

        with tempfile.TemporaryDirectory() as directory:
            capture = playback_capture.PlaybackCapture(Path(directory))
            session = mock.Mock()
            session.send.return_value = {"bufferedData": base64.b64encode(b"video").decode()}
            capture._wire_session(session)
            sid = id(session)
            capture._on_cdp_request(sid, {
                "requestId": "1", "type": "Media",
                "request": {"url": "https://cdn.test/video.mp4", "headers": {}},
            })
            capture._on_cdp_response(sid, {
                "requestId": "1", "type": "Media",
                "response": {"url": "https://cdn.test/video.mp4", "status": 200,
                             "mimeType": "video/mp4", "headers": {"content-length": "8"}},
            })
            capture.drain()
            self.assertEqual(capture.media_progress()["stream_bytes"], 5)
            capture._on_cdp_data(sid, {"requestId": "1", "data": base64.b64encode(b"123").decode()})
            capture.drain()
            progress = capture.media_progress()
            self.assertEqual(progress["stream_bytes"], 8)
            self.assertFalse(progress["complete"])
            capture._on_cdp_finished(session, sid, {"requestId": "1"})
            capture.drain()
            self.assertTrue(capture.media_progress()["complete"])
            self.assertEqual(Path(capture.files[0]["path"]).read_bytes(), b"video123")

    def test_stream_growth_and_repeated_headers_affect_timeout_correctly(self):
        import openx_handoff

        for growing in (True, False):
            with self.subTest(growing=growing):
                capture = mock.Mock(media_seen=int(growing), tap_inflight=0, tap_bytes=0,
                                    stop_reason="", cache_error="")
                page = mock.Mock()
                clock = [0.0]
                stream_bytes = [0]
                confirmations = []
                capture.media_progress.side_effect = lambda: {
                    "covered": 0, "total": 0, "complete": False, "stream_bytes": stream_bytes[0],
                }

                def wait(ms):
                    clock[0] = round(clock[0] + ms / 1000, 3)
                    if growing and clock[0] <= 90 and clock[0] % 30 == 0:
                        stream_bytes[0] += 6 * 1024 * 1024
                    if not growing:
                        capture.media_seen += 1

                page.wait_for_timeout.side_effect = wait
                with mock.patch("time.monotonic", side_effect=lambda: clock[0]), mock.patch.object(openx_handoff, "log"):
                    openx_handoff._capture_until_full(
                        page, capture, max_seconds=125,
                        on_started=lambda: confirmations.append(clock[0]),
                    )
                if growing:
                    self.assertEqual(confirmations, [30])
                    self.assertEqual(capture.stop_reason, "limit")
                else:
                    self.assertEqual(confirmations, [])
                    self.assertEqual(capture.stop_reason, "media_unreadable")
                    self.assertEqual(clock[0], 90)


class PlaybackOutputTests(unittest.TestCase):
    def setUp(self):
        import main

        self.main = main
        main._names_claimed.clear()
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.out = Path(temp.name) / "out"
        self.out.mkdir()
        self.history = mock.Mock()
        self.source = self.out / "media_stream.mp4"

    def finish(self, name="Movie", url="https://article.test/movie", *, force=False, prev=None,
              complete=True, total=5, missing=False):
        def fake_handoff(embed, **kwargs):
            capture_dir = Path(kwargs["capture_dir"])
            capture_dir.mkdir(parents=True, exist_ok=True)
            self.source = capture_dir / "media_stream.mp4"
            if not missing:
                self.source.write_bytes(b"video")
            else:
                self.source.unlink(missing_ok=True)
            return {
                "ok": True, "no_download_button": True,
                "playback": {
                    "files": [{"path": str(self.source), "size": 5}],
                    "output": str(self.source), "complete": complete,
                    "total": total, "covered": 5, "bytes": 5, "capture_dir": str(capture_dir),
                },
            }

        with mock.patch.object(self.main, "open_download_handoff", side_effect=fake_handoff):
            return self.main._finish_with_openx_handoff(
                {}, {"embed_url": "https://player.test/?v=id"}, name, "HTTP 403",
                page_url=url, item_url=url, history=self.history, out_dir=self.out,
                force=force, prev_output=prev,
            )

    def test_completed_file_uses_safe_indexed_title_and_updates_records(self):
        result = self.finish("한글: 제목 (02)", "https://article.test/movie#v02")
        dest = self.out / "한글_ 제목 (02).mp4"
        self.assertEqual(result["status"], "ok")
        self.assertEqual(dest.read_bytes(), b"video")
        self.assertFalse(self.source.exists())
        self.assertEqual(result["output"], str(dest))
        self.assertEqual(result["playback"]["files"][0]["path"], str(dest))
        self.assertEqual(self.history.record_success.call_args.args[2], str(dest))
        manifest = json.loads((self.source.parent / "playback_capture.json").read_text(encoding="utf-8"))
        self.assertEqual(manifest["output"], str(dest))
        self.assertEqual(manifest["files"][0]["path"], str(dest))

    def test_existing_file_is_not_overwritten_and_force_replaces_only_previous_file(self):
        other = self.out / "Movie.mp4"
        other.write_bytes(b"other")
        result = self.finish(force=True)
        dest = Path(result["output"])
        self.assertRegex(dest.name, r"^Movie \[[0-9a-f]{6}\]\.mp4$")
        self.assertEqual(other.read_bytes(), b"other")
        self.main._names_claimed.clear()
        result = self.finish(force=True, prev=dest)
        self.assertEqual(result["output"], str(dest))
        self.assertEqual(dest.read_bytes(), b"video")
        self.assertEqual(other.read_bytes(), b"other")

    def test_previous_file_in_capture_directory_cannot_authorize_overwrite(self):
        other = self.out / "Movie.mp4"
        other.write_bytes(b"other")
        old_dir = self.out / "old_capture"
        old_dir.mkdir()
        previous = old_dir / "Movie.mp4"
        previous.write_bytes(b"old")
        result = self.finish(force=True, prev=previous)
        self.assertEqual(result["status"], "ok")
        self.assertNotEqual(result["output"], str(other))
        self.assertEqual(other.read_bytes(), b"other")
        self.assertEqual(previous.read_bytes(), b"old")

    def test_force_with_different_previous_extension_preserves_other_file(self):
        previous = self.out / "Movie.webm"
        previous.write_bytes(b"old")
        other = self.out / "Movie.mp4"
        other.write_bytes(b"other")
        result = self.finish(force=True, prev=previous)
        self.assertEqual(result["status"], "ok")
        self.assertNotEqual(result["output"], str(other))
        self.assertEqual(previous.read_bytes(), b"old")
        self.assertEqual(other.read_bytes(), b"other")

    def test_assembled_ranges_use_the_detected_container_extension(self):
        import playback_capture

        def fake_handoff(embed, **kwargs):
            capture_dir = Path(kwargs["capture_dir"])
            capture_dir.mkdir(parents=True)
            capture = playback_capture.PlaybackCapture(capture_dir)
            url = "https://cdn.test/videoplayback"
            capture._store(url, 206, "video/webm", "bytes 0-2/5", "bytes=0-2", b"vid", "media")
            capture._store(url, 206, "video/webm", "bytes 3-4/5", "bytes=3-4", b"eo", "media")
            return {"ok": True, "no_download_button": True, "playback": capture.summary()}

        with mock.patch.object(self.main, "open_download_handoff", side_effect=fake_handoff):
            result = self.main._finish_with_openx_handoff(
                {}, {"embed_url": "https://player.test/?v=id"}, "Movie", "HTTP 403",
                item_url="https://article.test/movie", history=self.history, out_dir=self.out,
            )
        self.assertEqual(result["status"], "ok")
        self.assertEqual(result["output"], str(self.out / "Movie.webm"))
        self.assertEqual((self.out / "Movie.webm").read_bytes(), b"video")
        self.assertTrue(all(Path(item["path"]).is_file() for item in result["playback"]["files"]))
        self.assertEqual(self.history.record_success.call_args.args[2], result["output"])

    def test_failed_force_move_preserves_previous_file(self):
        previous = self.out / "Movie.mp4"
        previous.write_bytes(b"old")
        with mock.patch.object(Path, "replace", side_effect=PermissionError("blocked")):
            result = self.finish(force=True, prev=previous)
        self.assertEqual(result["status"], "failed")
        self.assertEqual(previous.read_bytes(), b"old")
        self.assertTrue(self.source.exists())
        self.history.record_success.assert_not_called()

    def test_partial_missing_and_move_failure_do_not_record_success(self):
        for kwargs in ({"complete": False}, {"total": 10}, {"missing": True}, {"move_error": True}):
            with self.subTest(kwargs=kwargs):
                self.main._names_claimed.clear()
                self.history.reset_mock()
                params: dict[str, Any] = dict(kwargs)
                move_error = params.pop("move_error", False)
                with mock.patch.object(Path, "replace", side_effect=PermissionError("blocked")) if move_error else nullcontext():
                    result = self.finish(**params)
                self.assertEqual(result["status"], "failed")
                self.history.record_success.assert_not_called()
                self.assertFalse((self.out / "Movie.mp4").exists())
                if not params.get("missing"):
                    self.assertTrue(self.source.exists())


class StreamingDurationTests(unittest.TestCase):
    def test_config_validates_and_accepts_custom_limit(self):
        import main

        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "config.yaml"
            for value, expected in (("3600", 3600), ("0", 0), ("2.5", 2.5)):
                path.write_text(f"streaming_max_duration_seconds: {value}\n", encoding="utf-8")
                self.assertEqual(main._load_config(path), expected)
            for value in ("-1", ".nan", "true", "'7200'", "9" * 400):
                path.write_text(f"streaming_max_duration_seconds: {value}\n", encoding="utf-8")
                with self.assertRaises(ValueError):
                    main._load_config(path)

    def test_stream_continues_until_length_is_known_then_stops_at_boundary(self):
        import openx_handoff

        capture = mock.Mock(media_seen=1, tap_inflight=0, tap_bytes=0, stop_reason="", cache_error="")
        capture.media_progress.return_value = {"covered": 0, "total": 0, "complete": False}
        page = mock.Mock()
        clock = [0.0]
        page.wait_for_timeout.side_effect = lambda ms: clock.__setitem__(0, clock[0] + ms / 1000)
        with mock.patch.object(openx_handoff, "_player_duration", side_effect=[None, 7199, 7200]) as duration, \
                mock.patch.object(openx_handoff, "log"), \
                mock.patch("time.monotonic", side_effect=lambda: clock[0]):
            openx_handoff._capture_until_full(page, capture, max_seconds=30, max_duration_seconds=7200)
        self.assertEqual(capture.stop_reason, "duration_limit")
        self.assertEqual(capture.video_duration, 7200)
        # 길이 확인은 매 바퀴가 아니라 DURATION_CHECK_SECONDS 마다 한다.
        self.assertEqual(duration.call_count, 3)

    def test_streaming_skip_is_recorded_and_not_reported_as_failure(self):
        import main
        from history import HistoryManager

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            history = HistoryManager(root / "history")
            url = "https://article.test/movie"
            outcome = {
                "ok": True, "no_download_button": True, "skipped_long": True,
                "duration": 7200, "playback": {"files": []},
            }
            with mock.patch.object(main, "open_download_handoff", return_value=outcome) as handoff:
                result = main._finish_with_openx_handoff(
                    {}, {"embed_url": "https://player.test/?v=id"}, "Movie", "403",
                    item_url=url, history=history, out_dir=root, max_duration_seconds=7200,
                )
            self.assertEqual(result["status"], "skipped_long")
            self.assertTrue(history.is_downloaded(url))
            self.assertEqual(main._summarize_report([result])[0], "OK")
            self.assertEqual(handoff.call_args.kwargs["max_duration_seconds"], 7200)
            self.assertEqual(list(root.glob("*.mp4")), [])

    def test_completed_file_is_checked_and_unknown_or_disabled_can_continue(self):
        import main

        for limit, metadata, expected in (
            (7200, (7200.0, 5), "skipped_long"),
            (7200, (7199.0, 5), "ok"),
            (7200, None, "ok"),
            (0, (7200.0, 5), "ok"),
        ):
            with self.subTest(limit=limit, metadata=metadata), tempfile.TemporaryDirectory() as directory:
                root = Path(directory)
                capture = root / "capture"
                capture.mkdir()
                source = capture / "video.mp4"
                source.write_bytes(b"video")
                playback = {
                    "files": [{"path": str(source), "size": 5}], "output": str(source),
                    "complete": True, "total": 5, "covered": 5, "bytes": 5, "capture_dir": str(capture),
                }
                outcome = {"ok": True, "no_download_button": True, "playback": playback}
                history = mock.Mock()
                main._names_claimed.clear()
                with mock.patch.object(main, "open_download_handoff", return_value=outcome), \
                        mock.patch.object(main, "probe_duration_size", return_value=metadata) as probe:
                    result = main._finish_with_openx_handoff(
                        {}, {"embed_url": "https://player.test/?v=id"}, "Movie", "403",
                        item_url="https://article.test/movie", history=history, out_dir=root,
                        max_duration_seconds=limit,
                    )
                self.assertEqual(result["status"], expected)
                self.assertEqual((root / "Movie.mp4").exists(), expected == "ok")
                if expected == "skipped_long":
                    history.record_success.assert_not_called()
                    history.record_skipped.assert_called_once()
                    self.assertTrue(source.exists())
                if limit == 0:
                    probe.assert_not_called()

    def test_long_limit_does_not_affect_regular_download(self):
        import main

        group = {"index": 1, "candidates": [{"url": "https://cdn.test/video.mp4", "classification": "MAIN"}]}
        opts = main.Options(streaming_max_duration_seconds=7200)
        with mock.patch.object(main, "probe_duration", return_value=8000), \
                mock.patch.object(main, "_download_candidate", return_value={"status": "ok", "output": "x.mp4"}), \
                mock.patch.object(main.blacklist, "record_main"):
            result = main._process_item(
                group=group, n=1, url="https://article.test/movie", name="Movie",
                out_dir=Path("."), proxy=None, opts=opts, history=None, post_date=None,
                force=False, board=None, slot=1,
            )
        self.assertEqual(result["status"], "ok")

    def test_limit_reaches_streaming_handoff_after_regular_download_403(self):
        import main
        from history import HistoryManager

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            history = HistoryManager(root / "history")
            group = {
                "index": 1, "embed_url": "https://player.test/?v=id",
                "candidates": [{"url": "https://cdn.test/video.mp4", "classification": "MAIN"}],
            }
            outcome = {
                "ok": True, "no_download_button": True, "skipped_long": True,
                "duration": 3600, "playback": {"files": []},
            }
            with mock.patch.object(main, "probe_duration", return_value=3600), \
                    mock.patch.object(main, "_download_candidate", return_value={"status": "failed", "reason": "HTTP 403"}), \
                    mock.patch.object(main, "open_download_handoff", return_value=outcome) as handoff:
                result = main._process_item(
                    group=group, n=1, url="https://article.test/movie", name="Movie",
                    out_dir=root, proxy=None, opts=main.Options(streaming_max_duration_seconds=3600),
                    history=history, post_date=None, force=False, board=None, slot=1,
                )
            self.assertEqual(result["status"], "skipped_long")
            self.assertEqual(handoff.call_args.kwargs["max_duration_seconds"], 3600)
            reloaded = HistoryManager(root / "history")
            self.assertTrue(reloaded.is_downloaded("https://article.test/movie"))


if __name__ == "__main__":
    unittest.main()
