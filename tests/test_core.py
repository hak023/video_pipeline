"""네트워크/브라우저 없이 검증 가능한 핵심 로직 테스트 (표준 unittest).

실행: python -m unittest discover -s tests -v
"""
from __future__ import annotations

import json
import sys
import tempfile
import unittest
import zipfile
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import diagnostics as dg                                    # noqa: E402
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
        out = dg.sanitize({"cookies": [{"value": SECRET}], "request_headers": {"authorization": SECRET},
                           "url": f"https://h/x?token={SECRET}", "n": 1})
        self.assertNotIn(SECRET, json.dumps(out))
        self.assertEqual(out["n"], 1)

    def test_candidate_view_is_allowlist(self):
        v = dg.candidate_view({"url": "https://h/x.mp4", "cookies": [{"value": SECRET}],
                               "request_headers": {"authorization": SECRET}, "user_agent": "ua", "classification": "MAIN"})
        self.assertNotIn("cookies", v)
        self.assertNotIn("request_headers", v)
        self.assertNotIn("user_agent", v)
        self.assertEqual(v["classification"], "MAIN")


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


if __name__ == "__main__":
    unittest.main()
