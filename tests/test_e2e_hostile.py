"""가혹한 환경(광고 팝업/하이재킹/다수 iframe) 방어 테스트. 브라우저 필요 → RUN_E2E=1 일 때만 실행.

실행: $env:RUN_E2E=1; python -m unittest tests.test_e2e_hostile -v
 - 샌드박스를 켠 채(--no-sandbox 없음) Chrome 이 정상 동작
 - 광고가 글 페이지를 다른 사이트로 이동시켜도 본편 임베드를 놓치지 않음 (A 단계는 별도 페이지에서 열기 때문)
 - 광고 팝업/자동 다운로드 시도에도 본편 다운로드 성공, 광고 파일이 저장되지 않음
 - 임베드가 많아 시간 예산을 넘으면 남은 임베드는 빈 항목으로 남고 나머지는 처리
"""
from __future__ import annotations

import functools
import http.server
import os
import shutil
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

V = '<video src="{}" muted preload="auto" width="400" height="200"></video>'
# 광고 스크립트 흉내: 팝업 열기 + 자동 다운로드 시도 + 3초 뒤 최상위 페이지를 다른 사이트(127.0.0.2)로 이동
HOSTILE = ("<script>window.open('/popup.html','_blank');"
           "var a=document.createElement('a');a.href='/ads/malware.bin';a.download='malware.bin';document.body.appendChild(a);a.click();"
           "setTimeout(function(){location.href='http://127.0.0.2:%d/landing.html';},3000);</script>")


@unittest.skipUnless(os.environ.get("RUN_E2E") == "1" and _have_tools(), "RUN_E2E=1 및 Chrome/ffmpeg/yt-dlp 필요")
class HostileE2ETests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = Path(tempfile.mkdtemp(prefix="hostile_"))
        site = cls.tmp / "site"
        for d in ("ep", "embed", "ads"):
            (site / d).mkdir(parents=True)
        _make_video(site / "ep" / "main.mp4", 120)
        (site / "ads" / "malware.bin").write_bytes(b"MZ" + b"\0" * 2048)
        (site / "popup.html").write_text(_page("popup ad"), encoding="utf-8")
        (site / "embed" / "player.html").write_text(_page(V.format("/ep/main.mp4")), encoding="utf-8")
        (site / "embed" / "empty.html").write_text(_page("<p>just an ad banner</p>"), encoding="utf-8")
        handler = functools.partial(http.server.SimpleHTTPRequestHandler, directory=str(site))
        handler.log_message = lambda *a, **k: None
        cls.httpd = http.server.ThreadingHTTPServer(("127.0.0.1", 0), handler)
        threading.Thread(target=cls.httpd.serve_forever, daemon=True).start()
        port = cls.httpd.server_address[1]
        (site / "hostile.html").write_text(
            _page('<iframe src="/embed/player.html" width="800" height="450"></iframe>' + HOSTILE % port), encoding="utf-8")
        many = "".join(f'<iframe src="/embed/empty.html?n={i}" width="800" height="450"></iframe>' for i in range(8))
        (site / "many.html").write_text(_page('<iframe src="/embed/player.html" width="800" height="450"></iframe>' + many), encoding="utf-8")
        cls.base = f"http://127.0.0.1:{port}"
        cls.out_dir = cls.tmp / "out"
        cls.out_dir.mkdir()
        bl = ad_blacklist.AdBlacklistStack(cls.tmp / "bl.json", extra_file=cls.tmp / "none.txt")
        orig = dl.download
        for p in (mock.patch.object(discover, "blacklist", bl), mock.patch.object(main, "blacklist", bl),
                  mock.patch.object(main, "download", lambda *a, **k: orig(*a, allow_private=True, **k)),
                  mock.patch.object(main, "OUT_DIR", cls.out_dir)):
            p.start()
            cls.addClassCleanup(p.stop)
        cls.diag = diagnostics.init_diagnostics(cls.tmp / "logs", meta={"mode": "hostile-e2e"})
        cls.addClassCleanup(cls.httpd.shutdown)
        cls.addClassCleanup(shutil.rmtree, cls.tmp, ignore_errors=True)

    def _run(self, name, path, budget=None):
        from history import HistoryManager
        opts = main.Options(workers=1, attempts=1, history_dir=self.tmp / f"hist_{name}")
        env = {"VP_DISCOVER_BUDGET": str(budget)} if budget else {}
        with mock.patch.dict(os.environ, env):
            rep = main._run_batch([{"name": name, "page_url": f"{self.base}/{path}", "wait_ms": 3000}],
                                  HistoryManager(opts.history_dir), None, True, False, opts)
        return rep[0]

    def test_hijack_popup_and_auto_download_do_not_break_download(self):
        r = self._run("hostile", "hostile.html")
        self.assertEqual(r["status"], "ok")
        self.assertEqual(round(r["duration"]), 120)
        names = {p.name for p in self.out_dir.iterdir()}
        self.assertIn("hostile.mp4", names)
        self.assertNotIn("malware.bin", names)       # 광고가 시도한 자동 다운로드는 저장되지 않는다

    def test_discovery_budget_skips_remaining_embeds_but_keeps_main(self):
        r = self._run("manyembeds", "many.html", budget=10)      # 첫 임베드 처리 중 예산 초과 → 나머지는 생략
        statuses = {it["status"] for it in r.get("items", [])} if "items" in r else {r["status"]}
        self.assertIn("ok", statuses)
        self.assertTrue(any(it.get("status") == "no_media_found" for it in r.get("items", [])))


if __name__ == "__main__":
    unittest.main()
