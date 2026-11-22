"""브라우저 포함 로컬 E2E (선택 실행). 실제 Chrome + yt-dlp + ffmpeg 로 로컬 서버의 페이지를 처리한다.

실행: $env:RUN_E2E=1; python -m unittest tests.test_e2e_local -v      (약 3~4분)
Chrome/ffmpeg/yt-dlp 가 없거나 RUN_E2E 가 없으면 건너뛴다. 로컬 주소는 SSRF 차단 대상이라
이 테스트에서만 download(allow_private=True)로 감싼다.
"""
from __future__ import annotations

import functools
import http.server
import json
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
from history import HistoryManager  # noqa: E402
from media_tools import resolve_chrome_channel  # noqa: E402


def _have_tools() -> bool:
    try:
        import yt_dlp  # noqa: F401
    except ImportError:
        return False
    return bool(shutil.which("ffmpeg") and resolve_chrome_channel())


def _make_video(path: Path, secs: int) -> None:
    subprocess.run(["ffmpeg", "-y", "-loglevel", "error", "-f", "lavfi", "-i", "testsrc=size=160x90:rate=10",
                    "-f", "lavfi", "-i", "sine=frequency=440", "-t", str(secs), "-c:v", "libx264",
                    "-preset", "ultrafast", "-pix_fmt", "yuv420p", "-c:a", "aac", "-shortest", str(path)], check=True)


PLAYER_TWO = ('<!doctype html><html><body style="margin:0">'
              '<video src="/ads/promo.mp4" muted preload="auto" width="400" height="200"></video>'
              '<video src="/media/episode1_1080p.mp4" muted preload="auto" width="400" height="200"></video>'
              '</body></html>')
JW_FAKE = """<!doctype html><html><body style="margin:0"><div id="player"></div><script>
window.jwplayer = function(id){ return {
  getPlaylist: function(){ return [
    {duration:130, sources:[{file:'/media/episode1_1080p.mp4'}]},
    {duration:100, sources:[{file:'/ep2/episode2.mp4'}]} ]; },
  getPlaylistIndex: function(){ return 0; }, getDuration: function(){ return 130; },
  play: function(){}, getState: function(){ return 'idle'; } }; };
</script></body></html>"""


def _page(body: str) -> str:
    return f"<!doctype html><html><body>{body}</body></html>"


@unittest.skipUnless(os.environ.get("RUN_PLAYBACK_CAPTURE") == "1" and resolve_chrome_channel(),
                     "RUN_PLAYBACK_CAPTURE=1 및 Chrome 필요")
class ServiceWorkerPlaybackTests(unittest.TestCase):
    def test_browser_range_fallback_uses_player_service_worker(self):
        import urllib.error
        import urllib.request

        import playback_capture
        from media_tools import get_browser_launch_kwargs
        from playwright.sync_api import sync_playwright

        hits = []
        worker = b"""
            self.addEventListener('install', e => e.waitUntil(self.skipWaiting()));
            self.addEventListener('activate', e => e.waitUntil(self.clients.claim()));
            self.addEventListener('fetch', e => {
                if (new URL(e.request.url).pathname !== '/media/video.mp4') return;
                const match = /^bytes=(\\d+)-(\\d+)$/.exec(e.request.headers.get('range') || '');
                if (!match) return;
                const start = Number(match[1]), end = Number(match[2]);
                const data = new TextEncoder().encode('abcdefgh').slice(start, end + 1);
                e.respondWith(new Response(data, {status: 206, headers: {
                    'Content-Type': 'video/mp4',
                    'Content-Range': `bytes ${start}-${end}/8`
                }}));
            });
        """

        class Handler(http.server.BaseHTTPRequestHandler):
            def do_GET(self):  # noqa: N802
                if self.path == "/sw.js":
                    status, ctype, body = 200, "application/javascript", worker
                elif self.path == "/":
                    status, ctype, body = 200, "text/html", b"<iframe name='player' src='/player'></iframe>"
                elif self.path == "/player":
                    status, ctype, body = 200, "text/html", b"<!doctype html><title>player</title>"
                else:
                    hits.append(self.path)
                    status, ctype, body = 403, "text/plain", b"forbidden"
                self.send_response(status)
                self.send_header("Content-Type", ctype)
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def log_message(self, *_args):
                pass

        server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        threading.Thread(target=server.serve_forever, daemon=True).start()
        self.addCleanup(server.server_close)
        self.addCleanup(server.shutdown)
        base = f"http://127.0.0.1:{server.server_address[1]}"
        url = f"{base}/media/video.mp4"
        with self.assertRaises(urllib.error.HTTPError) as error:
            urllib.request.urlopen(url, timeout=5)
        self.assertEqual(error.exception.code, 403)

        with tempfile.TemporaryDirectory() as directory, sync_playwright() as p:
            browser = p.chromium.launch(**get_browser_launch_kwargs(headless=True))
            try:
                page = browser.new_page()
                page.goto(base)
                frame = page.frame(name="player")
                assert frame is not None
                frame.evaluate("async () => { await navigator.serviceWorker.register('/sw.js'); await navigator.serviceWorker.ready; }")
                frame.wait_for_function("() => !!navigator.serviceWorker.controller", timeout=10000)
                capture = playback_capture.PlaybackCapture(Path(directory))
                capture.install(page)
                request = mock.Mock(url=url, resource_type="media", headers={"range": "bytes=0-"},
                                    method="GET", frame=frame)
                response = mock.Mock(url=url, status=206, request=request,
                                     headers={"content-type": "video/mp4", "content-range": "bytes 0-7/8"})
                session = mock.Mock()
                session.send.side_effect = RuntimeError("Request with the provided ID does not exists")
                capture._wire_session(session)
                with mock.patch.object(capture, "_start_direct_media_download"), \
                        mock.patch.object(playback_capture, "BROWSER_FALLBACK_CHUNK_BYTES", 4):
                    capture._on_response(response)
                    capture._on_cdp_response(id(session), {"requestId": "1", "type": "Media", "response": {
                        "url": url, "status": 206, "mimeType": "video/mp4",
                        "headers": {"content-range": "bytes 0-7/8"},
                    }})
                    capture.drain(allow_browser_fallback=True)
                    capture.drain(allow_browser_fallback=True)
                    result = capture.summary()
                capture.uninstall(page)
                self.assertTrue(result["complete"], result)
                self.assertEqual(Path(result["output"]).read_bytes(), b"abcdefgh")
                self.assertFalse(any(entry["event"] == "fallback_start" for entry in result["capture_trace"]))
                self.assertEqual(hits.count("/media/video.mp4"), 1)
            finally:
                browser.close()


@unittest.skipUnless(os.environ.get("RUN_E2E") == "1" and _have_tools(), "RUN_E2E=1 및 Chrome/ffmpeg/yt-dlp 필요")
class LocalE2ETests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = Path(tempfile.mkdtemp(prefix="e2e_"))
        site = cls.tmp / "site"
        for d in ("media", "ep2", "ads", "embed"):
            (site / d).mkdir(parents=True)
        _make_video(site / "media" / "episode1_1080p.mp4", 130)
        _make_video(site / "ep2" / "episode2.mp4", 100)
        _make_video(site / "ads" / "promo.mp4", 20)
        v = '<video src="{}" muted preload="auto" width="320" height="180"></video>'
        (site / "index.html").write_text(_page(v.format("/ads/promo.mp4") + v.format("/media/episode1_1080p.mp4")
                                                + v.format("/ep2/episode2.mp4")), encoding="utf-8")
        (site / "embed" / "player.html").write_text(PLAYER_TWO, encoding="utf-8")
        (site / "embed" / "adonly.html").write_text(_page(v.format("/ads/promo.mp4")), encoding="utf-8")
        (site / "embed" / "jw.html").write_text(JW_FAKE, encoding="utf-8")
        for name, emb in (("page_embed", "player"), ("page_adonly", "adonly"), ("page_jw", "jw")):
            (site / f"{name}.html").write_text(
                _page(f'<iframe src="/embed/{emb}.html" width="800" height="450"></iframe>'), encoding="utf-8")

        handler = functools.partial(http.server.SimpleHTTPRequestHandler, directory=str(site))
        handler.log_message = lambda *a, **k: None
        cls.httpd = http.server.ThreadingHTTPServer(("127.0.0.1", 0), handler)
        threading.Thread(target=cls.httpd.serve_forever, daemon=True).start()
        base = f"http://127.0.0.1:{cls.httpd.server_address[1]}"

        bl = ad_blacklist.AdBlacklistStack(cls.tmp / "bl.json")
        orig = dl.download
        out_dir = cls.tmp / "out"
        out_dir.mkdir()
        patches = [
            mock.patch.object(discover, "blacklist", bl), mock.patch.object(main, "blacklist", bl),
            mock.patch.object(main, "download", lambda *a, **k: orig(*a, allow_private=True, **k)),
            mock.patch.object(main, "OUT_DIR", out_dir),
        ]
        for p in patches:
            p.start()
            cls.addClassCleanup(p.stop)
        cls.diag = diagnostics.init_diagnostics(cls.tmp / "logs", meta={"mode": "e2e"})
        opts = main.Options(workers=1, attempts=2, history_dir=cls.tmp / "hist")
        cls.history = HistoryManager(opts.history_dir)
        targets = [
            {"name": "e2e-local", "page_url": f"{base}/index.html", "wait_ms": 4000},
            {"name": "e2e-embed", "page_url": f"{base}/page_embed.html", "wait_ms": 4000},
            {"name": "e2e-adonly", "page_url": f"{base}/page_adonly.html", "wait_ms": 3000},
            {"name": "e2e-404", "page_url": f"{base}/missing.html", "wait_ms": 3000},
            {"name": "e2e-jw", "page_url": f"{base}/page_jw.html", "wait_ms": 3000},
        ]
        report = main._run_batch(targets, cls.history, None, True, False, opts)
        cls.report = {r["name"]: r for r in report}
        cls.out_dir = out_dir
        status, stage, why = main._summarize_report(report)
        cls.diag.finalize(status, stage, why)
        cls.status = status
        cls.addClassCleanup(cls.httpd.shutdown)
        cls.addClassCleanup(shutil.rmtree, cls.tmp, ignore_errors=True)

    def test_multiple_mains_downloaded_and_ad_excluded(self):
        self.assertEqual(self.report["e2e-local"]["status"], "ok")
        names = {p.name for p in self.out_dir.glob("e2e-local*.mp4")}
        self.assertEqual(names, {"e2e-local (01).mp4", "e2e-local (02).mp4"})

    def test_embed_with_ad_and_main_downloads_only_main(self):
        self.assertEqual(self.report["e2e-embed"]["status"], "ok")
        self.assertEqual(self.report["e2e-embed"].get("duration") and round(self.report["e2e-embed"]["duration"]), 130)

    def test_jw_playlist_items_become_separate_videos(self):
        self.assertEqual(self.report["e2e-jw"]["status"], "ok")
        self.assertEqual(len(list(self.out_dir.glob("e2e-jw*.mp4"))), 2)

    def test_ad_only_page_is_not_marked_done(self):
        self.assertEqual(self.report["e2e-adonly"]["status"], "no_main_found")
        self.assertFalse(self.history.is_downloaded(self.report["e2e-adonly"]["url"]))

    def test_404_fails_fast_without_retry(self):
        self.assertEqual(self.report["e2e-404"]["status"], "failed")
        self.assertEqual(self.report["e2e-404"].get("attempts"), 1)

    def test_summary_and_zip_written_without_secrets(self):
        s = json.loads((self.diag.dir / "summary.json").read_text(encoding="utf-8"))
        self.assertEqual(s["status"], "PARTIAL")
        self.assertGreaterEqual(s["main_count"], 4)
        self.assertGreaterEqual(s["ad_count"], 1)
        self.assertTrue(self.diag.zip_path and self.diag.zip_path.is_file())


if __name__ == "__main__":
    unittest.main()
