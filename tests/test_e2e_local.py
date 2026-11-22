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


_SW_JS = r"""
self.addEventListener('install', e => e.waitUntil(self.skipWaiting()));
self.addEventListener('activate', e => e.waitUntil(self.clients.claim()));
const sleep = ms => new Promise(r => setTimeout(r, ms));
let real = null;
self.addEventListener('fetch', e => {
    const url = new URL(e.request.url);
    // 실제 사이트처럼 fragment가 있어야만 가상 주소의 영상을 만들어 준다.
    if (!url.pathname.startsWith('/mediastorage/') || !url.hash) return;
    const opts = new URLSearchParams(url.hash.slice(1));
    e.respondWith((async () => {
        await sleep(Number(opts.get('delay') || 0));
        if (!real) real = new Uint8Array(await (await fetch(opts.get('real'))).arrayBuffer());
        const m = /^bytes=(\d+)-(\d*)$/.exec(e.request.headers.get('range') || 'bytes=0-');
        const start = Number(m[1]);
        const end = m[2] ? Math.min(Number(m[2]), real.length - 1) : real.length - 1;
        const data = real.slice(start, end + 1);
        const half = Math.floor(data.length / 2);
        const body = new ReadableStream({async start(c) {
            c.enqueue(data.slice(0, half));
            await sleep(Number(opts.get('hold') || 0));
            c.enqueue(data.slice(half));
            c.close();
        }});
        return new Response(body, {status: 206, headers: {
            'Content-Type': 'video/mp4', 'Accept-Ranges': 'bytes',
            'Content-Length': String(data.length),
            'Content-Range': `bytes ${start}-${end}/${real.length}`,
        }});
    })());
});
"""
_SW_PLAYER = """<!doctype html><title>player</title><body><script>
(async () => {
    const q = new URLSearchParams(location.search);
    await navigator.serviceWorker.register('/sw.js');
    await navigator.serviceWorker.ready;
    if (!navigator.serviceWorker.controller) {
        await new Promise(r => navigator.serviceWorker.addEventListener('controllerchange', r, {once: true}));
    }
    const v = document.createElement('video');
    v.muted = true;
    if (q.has('test_duration')) Object.defineProperty(v, 'duration', {value: Number(q.get('test_duration'))});
    v.src = `http://127.0.0.1:%d/mediastorage/${Date.now()}/token/%d.mp4`
        + `#real=/real.mp4&delay=${q.get('delay') || 0}&hold=${q.get('hold') || 0}`;
    document.body.appendChild(v);
    v.play().catch(() => {});
})();
</script></body>"""


@unittest.skipUnless(os.environ.get("RUN_PLAYBACK_CAPTURE") == "1" and resolve_chrome_channel()
                     and shutil.which("ffmpeg"), "RUN_PLAYBACK_CAPTURE=1 및 Chrome/ffmpeg 필요")
class ServiceWorkerPlaybackTests(unittest.TestCase):
    """교차 출처 iframe의 서비스워커가 가상 주소(직접 요청은 403)로 영상을 만들어 주는 플레이어."""

    def setUp(self):
        import socket

        from media_tools import get_browser_launch_kwargs
        from playwright.sync_api import sync_playwright

        self.tmp = Path(tempfile.mkdtemp(prefix="vp_sw_"))
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)
        _make_video(self.tmp / "real.mp4", 3)
        self.real = (self.tmp / "real.mp4").read_bytes()
        self.origin_hits = []
        test = self

        class Origin(http.server.BaseHTTPRequestHandler):
            def do_GET(self):  # noqa: N802
                test.origin_hits.append(self.headers.get("User-Agent", ""))
                self.send_response(403)
                self.send_header("Content-Length", "0")
                self.end_headers()

            def log_message(self, *_args):
                pass

        class Site(http.server.BaseHTTPRequestHandler):
            def do_GET(self):  # noqa: N802
                path = self.path.split("?")[0]
                pages = {
                    "/article": ("text/html", b"<!doctype html><title>article</title><body>article</body>"),
                    "/": ("text/html", (_SW_PLAYER % (test.origin_port, len(test.real))).encode()),
                    "/sw.js": ("application/javascript", _SW_JS.encode()),
                    "/real.mp4": ("video/mp4", test.real),
                }
                if path not in pages:
                    self.send_error(404)
                    return
                ctype, body = pages[path]
                self.send_response(200)
                self.send_header("Content-Type", ctype)
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def log_message(self, *_args):
                pass

        for handler in (Origin, Site):
            server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), handler)
            threading.Thread(target=server.serve_forever, daemon=True).start()
            self.addCleanup(server.server_close)
            self.addCleanup(server.shutdown)
            if handler is Origin:
                self.origin_port = server.server_address[1]
            else:
                site_port = server.server_address[1]
        # 글 페이지와 임베드의 사이트가 달라야 임베드가 별도 프로세스(별도 CDP 세션)가 된다.
        self.article = f"http://localhost:{site_port}/article"
        self.embed = f"http://127.0.0.1:{site_port}/?v=swvideo01"
        with socket.socket() as probe:
            probe.bind(("127.0.0.1", 0))
            self.cdp_port = probe.getsockname()[1]
        self.playwright = sync_playwright().start()
        self.addCleanup(self.playwright.stop)
        launch = get_browser_launch_kwargs(headless=True)
        launch["args"] = [*launch["args"], f"--remote-debugging-port={self.cdp_port}"]
        self.browser = self.playwright.chromium.launch(**launch)
        self.addCleanup(self.browser.close)

    def _player_frame(self, page):
        frame = page.query_selector("#player_frame").content_frame()
        assert frame is not None
        return frame

    def _wait_complete(self, page, capture, **drain):
        for _ in range(200):
            capture.drain(**drain)
            if capture.media_progress()["complete"]:
                break
            page.wait_for_timeout(100)
        return capture.summary()

    def test_handoff_saves_service_worker_media_from_cross_origin_player(self):
        import contextlib

        import openx_handoff

        with mock.patch.object(openx_handoff, "log"):
            outcome = openx_handoff.open_download_handoff(
                self.embed + "&delay=300&hold=4000", page_url=self.article,
                cdp_url=f"http://127.0.0.1:{self.cdp_port}", timeout_ms=3000,
                playwright=contextlib.nullcontext(self.playwright), capture_dir=self.tmp / "capture",
            )
        playback = outcome.get("playback") or {}
        self.assertTrue(playback.get("complete"), outcome)
        self.assertEqual(Path(playback["output"]).read_bytes(), self.real)

    def test_handoff_skips_video_at_configured_duration(self):
        import contextlib
        import openx_handoff

        with mock.patch.object(openx_handoff, "log"):
            outcome = openx_handoff.open_download_handoff(
                self.embed + "&delay=300&hold=4000&test_duration=7200", page_url=self.article,
                cdp_url=f"http://127.0.0.1:{self.cdp_port}", timeout_ms=3000,
                playwright=contextlib.nullcontext(self.playwright), capture_dir=self.tmp / "capture",
                max_duration_seconds=7200,
            )
        self.assertTrue(outcome.get("skipped_long"), outcome)
        self.assertEqual(outcome.get("duration"), 7200)
        self.assertEqual(outcome["playback"]["stop_reason"], "duration_limit")

    def test_media_requested_before_cdp_attach_is_recovered_by_reloading_player(self):
        import openx_handoff
        import playback_capture

        page = self.browser.new_page()
        capture = playback_capture.PlaybackCapture(self.tmp / "capture")
        capture.install(page)
        self.addCleanup(capture.uninstall, page)
        openx_handoff._open_embed_frame(page, self.embed + "&delay=1500&hold=4000", self.article)
        # 영상 요청이 나간 뒤에야 iframe의 CDP 세션이 붙는 순서를 만든다.
        self._player_frame(page).wait_for_selector("video", state="attached", timeout=15000)
        with mock.patch.object(openx_handoff, "log"):
            openx_handoff._prime_embed_player(page, None, capture)
            self.assertFalse(capture.media_progress()["complete"])
            openx_handoff._capture_until_full(page, capture, max_seconds=60)
        result = capture.summary()
        events = [entry["event"] for entry in result["capture_trace"]]
        self.assertIn("player_reloaded", events)
        self.assertIn("cdp_stream_started", events)
        self.assertTrue(result["complete"], result)
        self.assertEqual(Path(result["output"]).read_bytes(), self.real)

    def test_browser_range_fallback_keeps_fragment_for_service_worker(self):
        import playback_capture

        page = self.browser.new_page()
        page.goto(self.article)
        page.set_content(f"<iframe id='player_frame' src='{self.embed}'></iframe>")
        frame = self._player_frame(page)
        frame.wait_for_selector("video", state="attached", timeout=15000)
        url = frame.evaluate("() => document.querySelector('video').currentSrc").split("#")[0]
        total = len(self.real)
        headers = {"content-type": "video/mp4", "content-range": f"bytes 0-{total - 1}/{total}"}
        capture = playback_capture.PlaybackCapture(self.tmp / "capture")
        capture.out_dir.mkdir()
        request = mock.Mock(url=url, resource_type="media", headers={"range": "bytes=0-"}, method="GET", frame=frame)
        session = mock.Mock()
        session.send.side_effect = RuntimeError("Request with the provided ID does not exists")
        capture._wire_session(session)
        with mock.patch.object(capture, "_start_direct_media_download"), \
                mock.patch.object(playback_capture, "BROWSER_FALLBACK_CHUNK_BYTES", max(1024, total // 3)):
            capture._on_response(mock.Mock(url=url, status=206, request=request, headers=headers))
            capture._on_cdp_response(id(session), {"requestId": "1", "type": "Media", "response": {
                "url": url, "status": 206, "mimeType": "video/mp4", "headers": headers}})
            result = self._wait_complete(page, capture, allow_browser_fallback=True)
        self.assertTrue(result["complete"], result)
        self.assertEqual(Path(result["output"]).read_bytes(), self.real)
        self.assertEqual(self.origin_hits, [])


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
