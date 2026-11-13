"""--crawl 경로 E2E (선택 실행, 외부 네트워크 불필요): 로컬 카탈로그 2페이지 → 게시글 추출 → 날짜 필터 → 탐색·판별 → 다운로드.

실행: $env:RUN_E2E=1; python -m unittest tests.test_e2e_crawl -v
"""
from __future__ import annotations

import functools
import http.server
import os
import shutil
import subprocess
import sys
import tempfile
import threading
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import ad_blacklist  # noqa: E402
import diagnostics  # noqa: E402
import discover  # noqa: E402
import download as dl  # noqa: E402
import main  # noqa: E402
from tests.test_e2e_local import _have_tools, _make_video, _page  # noqa: E402

JW_PLAYLIST = """<!doctype html><html><body style="margin:0"><div id="player"></div><script>
window.jwplayer=function(id){return{getPlaylist:function(){return[
{duration:120,sources:[{file:'/ep/ep1.mp4'}]},{duration:100,sources:[{file:'/ep/ep2.mp4'}]}];},
getPlaylistIndex:function(){return 0;},getDuration:function(){return 120;},play:function(){},getState:function(){return 'idle';}};};
</script></body></html>"""
# 브라우저가 HLS 를 직접 재생하지 못하므로 fetch 로 master playlist 를 요청해 네트워크 후보를 만든다
HLS_FETCH = ('<!doctype html><html><body style="margin:0"><video muted width="640" height="360"></video>'
             "<script>fetch('/hls/master.m3u8').then(r=>r.text());</script></body></html>")

POSTS = {  # slug: (제목, 날짜, 임베드)
    "ad-and-main": ("Post with ad and main", "2026-11-10", "adplusmain"),
    "local-hls": ("Local HLS multi quality", "2026-11-08", "hls"),
    "two-episodes": ("Two episodes playlist", "2026-11-07", "playlist"),
    "ad-only": ("Ad only post", "2026-11-06", "adonly"),
    "old-post": ("Old post filtered by date", "2020-01-01", "hls"),
}


def _catalog(slugs):
    items = "".join(
        f'<article class="post"><a href="/p/{s}/" title="{POSTS[s][0]}"></a><h2 class="entry-title">{POSTS[s][0]}</h2>'
        f'<time datetime="{POSTS[s][1]}">{POSTS[s][1]}</time></article>' for s in slugs)
    return _page(f"<main>{items}</main>")


@unittest.skipUnless(os.environ.get("RUN_E2E") == "1" and _have_tools(), "RUN_E2E=1 및 Chrome/ffmpeg/yt-dlp 필요")
class CrawlE2ETests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = Path(tempfile.mkdtemp(prefix="crawl_e2e_"))
        site = cls.tmp / "site"
        for d in ("ads", "ep", "hls", "embed", "page/2", "p"):
            (site / d).mkdir(parents=True)
        _make_video(site / "ads" / "promo.mp4", 20)
        _make_video(site / "ep" / "ep1.mp4", 120)
        _make_video(site / "ep" / "ep2.mp4", 100)
        subprocess.run(
            ["ffmpeg", "-y", "-loglevel", "error", "-f", "lavfi", "-i", "testsrc=size=320x180:rate=10", "-f", "lavfi",
             "-i", "sine=frequency=330", "-t", "90", "-filter_complex", "[0:v]split=2[a][b];[b]scale=160:90[c]",
             "-map", "[a]", "-map", "1:a", "-map", "[c]", "-map", "1:a", "-c:v", "libx264", "-preset", "ultrafast",
             "-pix_fmt", "yuv420p", "-c:a", "aac", "-f", "hls", "-hls_time", "6", "-hls_playlist_type", "vod",
             "-master_pl_name", "master.m3u8", "-var_stream_map", "v:0,a:0 v:1,a:1",
             "-hls_segment_filename", str(site / "hls" / "v%v_%03d.ts"), str(site / "hls" / "v%v.m3u8")], check=True)
        v = '<video src="{}" muted preload="auto" width="400" height="200"></video>'
        (site / "embed" / "adplusmain.html").write_text(_page(v.format("/ads/promo.mp4") + v.format("/ep/ep1.mp4")), encoding="utf-8")
        (site / "embed" / "adonly.html").write_text(_page(v.format("/ads/promo.mp4")), encoding="utf-8")
        (site / "embed" / "hls.html").write_text(HLS_FETCH, encoding="utf-8")
        (site / "embed" / "playlist.html").write_text(JW_PLAYLIST, encoding="utf-8")
        for slug, (title, _d, emb) in POSTS.items():
            (site / "p" / slug).mkdir()
            (site / "p" / slug / "index.html").write_text(
                _page(f'<h1>{title}</h1><iframe src="/embed/{emb}.html" width="800" height="450"></iframe>'), encoding="utf-8")
        (site / "index.html").write_text(_catalog(["ad-and-main", "local-hls"]), encoding="utf-8")
        (site / "page" / "2" / "index.html").write_text(_catalog(["two-episodes", "ad-only", "old-post"]), encoding="utf-8")

        handler = functools.partial(http.server.SimpleHTTPRequestHandler, directory=str(site))
        handler.log_message = lambda *a, **k: None
        cls.httpd = http.server.ThreadingHTTPServer(("127.0.0.1", 0), handler)
        threading.Thread(target=cls.httpd.serve_forever, daemon=True).start()
        base = f"http://127.0.0.1:{cls.httpd.server_address[1]}"

        bl = ad_blacklist.AdBlacklistStack(cls.tmp / "bl.json", extra_file=cls.tmp / "none.txt")
        orig = dl.download
        cls.out_dir = cls.tmp / "out"
        cls.out_dir.mkdir()
        for p in (mock.patch.object(discover, "blacklist", bl), mock.patch.object(main, "blacklist", bl),
                  mock.patch.object(main, "download", lambda *a, **k: orig(*a, allow_private=True, **k)),
                  mock.patch.object(main, "OUT_DIR", cls.out_dir)):
            p.start()
            cls.addClassCleanup(p.stop)
        cls.diag = diagnostics.init_diagnostics(cls.tmp / "logs", meta={"mode": "crawl-e2e"})
        opts = main.Options(workers=2, attempts=2, history_dir=cls.tmp / "hist")
        report = main.run_crawl_and_download(base, start_page=1, end_page=2, since_date="2024-01-01", opts=opts,
                                             wait_ms=3000)
        cls.report = {r["name"]: r for r in report}
        cls.diag.finalize(*main._summarize_report(report))
        cls.addClassCleanup(cls.httpd.shutdown)
        cls.addClassCleanup(shutil.rmtree, cls.tmp, ignore_errors=True)

    def test_pagination_and_date_filter(self):
        self.assertNotIn("Old post filtered by date", self.report)
        self.assertEqual(len(self.report), 4)

    def test_ad_excluded_main_downloaded(self):
        r = self.report["Post with ad and main"]
        self.assertEqual(r["status"], "ok")
        self.assertEqual(round(r["duration"]), 120)

    def test_hls_master_downloaded_once(self):
        self.assertEqual(self.report["Local HLS multi quality"]["status"], "ok")
        self.assertEqual(len(list(self.out_dir.glob("Local HLS*.mp4"))), 1)

    def test_playlist_gives_two_files(self):
        self.assertEqual(self.report["Two episodes playlist"]["status"], "ok")
        self.assertEqual(len(list(self.out_dir.glob("Two episodes playlist (0*).mp4"))), 2)

    def test_ad_only_post_not_done(self):
        self.assertEqual(self.report["Ad only post"]["status"], "no_main_found")


if __name__ == "__main__":
    unittest.main()
