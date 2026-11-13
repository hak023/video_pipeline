"""플레이어 진단 도구 (다운로드는 하지 않음).

글 페이지를 열어 플레이어 구조, 클릭을 가로채는 투명 레이어, 클릭할 때마다 발생하는
네트워크 요청/팝업/영상 길이를 기록한다. 결과는 콘솔 요약 + probe_out/*.json + 스크린샷.

사용 예:
  python probe_page.py "https://s28.testonline.co/%ec%a3%bc%ed%95%98%eb%9e%91-261009/" --headful
  python probe_page.py URL --rounds 5 --proxy http://127.0.0.1:7890

  # 수동 모드: 브라우저가 열리면 직접 재생 버튼을 (필요한 만큼) 눌러 보세요. 그동안의 요청을 기록합니다.
  python probe_page.py URL --manual 60

※ 결과 JSON 에는 요청 URL/헤더 일부가 들어 있으니, 공유 전에 토큰·쿠키가 없는지 확인하세요.
"""
from __future__ import annotations

import argparse
import json
import re
import time
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

from playwright.sync_api import sync_playwright

from discover import _CLICK_BTN_JS, _EMBED_JS, _PLAY_VIDEO_JS, _SEGMENT_EXT, _VIDEO_STATE_JS
from media_tools import get_browser_launch_kwargs

UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36")
INTERESTING_URL = re.compile(r"m3u8|\.mpd|\.mp4|player|source|stream|playlist|video|embed|token|api", re.I)

OVERLAY_JS = """(pt) => document.elementsFromPoint(pt.x, pt.y).slice(0, 6).map(e => {
    const r = e.getBoundingClientRect(), cs = getComputedStyle(e);
    return {tag: e.tagName.toLowerCase(), id: e.id || null,
            cls: (e.className && e.className.toString().slice(0, 60)) || null,
            w: Math.round(r.width), h: Math.round(r.height), position: cs.position,
            z: cs.zIndex, opacity: cs.opacity, bg: cs.backgroundColor,
            src: (e.src || e.getAttribute('src') || '').slice(0, 120) || null};
})"""

PLAYER_JS = """() => ({
    iframes: Array.from(document.querySelectorAll('iframe')).map(i => ({
        src: (i.src || i.getAttribute('data-src') || '').slice(0, 200),
        w: i.clientWidth, h: i.clientHeight})),
    videos: Array.from(document.querySelectorAll('video')).map(v => ({
        src: (v.currentSrc || v.src || '').slice(0, 200), duration: isFinite(v.duration) ? v.duration : null,
        paused: v.paused})),
    buttons: Array.from(document.querySelectorAll(
        '#player-button, .play-button, .vjs-big-play-button, button.play, .plyr__control--overlaid, ' +
        'button[aria-label*="Play"], .jw-icon-display')).map(b => ({
        tag: b.tagName.toLowerCase(), id: b.id || null, cls: (b.className || '').toString().slice(0, 60)})),
    fp_embed: Array.from(document.querySelectorAll('script.fp-embed')).map(s => (s.textContent || '').slice(0, 300)),
})"""


RESOURCE_JS = """() => performance.getEntriesByType('resource')
    .filter(r => /m3u8|mpegurl|\\.mpd|\\.mp4|\\.webm/i.test(r.name))
    .map(r => ({url: r.name.slice(0, 300), type: r.initiatorType, start_ms: Math.round(r.startTime)}))"""


class Recorder:
    def __init__(self) -> None:
        self.t0 = time.monotonic()
        self.events: list[dict[str, Any]] = []
        self.popups: list[dict[str, Any]] = []
        self.close_popups = True

    def on_response(self, resp) -> None:
        try:
            url = resp.url
            ctype = (resp.headers.get("content-type") or "").lower()
            path = urlparse(url).path.lower()
            if path.endswith(_SEGMENT_EXT) or "mp2t" in ctype:
                return
            media = any(x in url.lower() for x in (".m3u8", ".mpd", ".mp4")) or "mpegurl" in ctype or "video" in ctype
            api = ("json" in ctype or "text/plain" in ctype) and INTERESTING_URL.search(url)
            if not (media or api):
                return
            ev: dict[str, Any] = {
                "t": round(time.monotonic() - self.t0, 1), "status": resp.status,
                "kind": "media" if media else "api", "ctype": ctype[:60], "url": url[:300],
                "sent_headers": sorted(k for k in resp.request.headers.keys()
                                       if k in ("referer", "origin", "cookie", "range", "authorization")),
                "frame": (lambda f: f.url[:100] if f else "")(getattr(resp, "frame", None)),
                "referer": (resp.request.headers.get("referer") or "")[:200],
                "origin": (resp.request.headers.get("origin") or "")[:100],
            }
            if api and not media:
                try:
                    ev["body_head"] = resp.text()[:300]
                except Exception:
                    pass
            self.events.append(ev)
        except Exception:
            pass

    def on_popup(self, page) -> None:
        if page.opener() is not None:
            self.popups.append({"t": round(time.monotonic() - self.t0, 1), "url": page.url[:200]})
            if self.close_popups:
                try:
                    page.close()
                except Exception:
                    pass


def play_rounds(ctx, rec: Recorder, url: str, label: str, rounds: int, out: Path, headless: bool,
                referer: str | None = None) -> dict[str, Any]:
    page = ctx.new_page()
    page.on("response", rec.on_response)
    report: dict[str, Any] = {"label": label, "url": url, "rounds": []}
    ev0 = len(rec.events)
    try:
        page.goto(url, wait_until="domcontentloaded", timeout=30000, referer=referer)
        page.wait_for_timeout(2500)
        report["on_load_requests"] = list(rec.events[ev0:])   # 클릭 전, 로드 직후 자동 발생한 요청
        report["structure"] = page.evaluate(PLAYER_JS)
        vp = page.viewport_size or {"width": 1280, "height": 720}
        center = {"x": vp["width"] / 2, "y": vp["height"] / 2}
        report["overlay_stack_at_center"] = page.evaluate(OVERLAY_JS, center)
        page.screenshot(path=str(out / f"{label}_round0_before.png"))

        for r in range(1, rounds + 1):
            n_ev, n_pop = len(rec.events), len(rec.popups)
            action = "JS 재생버튼 클릭"
            if r > 1:
                page.mouse.click(center["x"], center["y"])
                page.wait_for_timeout(800)
                action = "마우스 중앙 클릭 + JS 재생버튼 클릭"
            clicked = page.evaluate(_CLICK_BTN_JS) + page.evaluate(_PLAY_VIDEO_JS)
            page.wait_for_timeout(2500)
            page.screenshot(path=str(out / f"{label}_round{r}.png"))
            report["rounds"].append({
                "round": r, "action": action, "js_clicked_elements": clicked,
                "longest_video_sec": page.evaluate(_VIDEO_STATE_JS),
                "new_popups": rec.popups[n_pop:],
                "new_requests": rec.events[n_ev:],
                "overlay_stack_at_center": page.evaluate(OVERLAY_JS, center),
            })
    except Exception as e:
        report["error"] = str(e).splitlines()[0]
    finally:
        try:
            page.close()
        except Exception:
            pass
    return report


def manual_session(ctx, rec: Recorder, url: str, seconds: int, out: Path) -> dict[str, Any]:
    """사용자가 직접 재생하는 동안 요청/프레임/미디어 리소스를 기록."""
    rec.close_popups = False
    page = ctx.new_page()
    page.on("response", rec.on_response)
    report: dict[str, Any] = {"label": "manual", "url": url}
    try:
        page.goto(url, wait_until="domcontentloaded", timeout=30000)
        print(f"\n[수동 모드] 열린 브라우저에서 {seconds}초 동안 직접 재생해 보세요. "
              f"(버튼을 두 번 눌러야 하면 그대로 두 번)")
        page.wait_for_timeout(seconds * 1000)
        resources: list[dict[str, Any]] = []
        for fr in page.frames:
            try:
                for r in fr.evaluate(RESOURCE_JS):
                    resources.append({**r, "frame": fr.url[:120]})
            except Exception:
                pass
        report["frames"] = [fr.url[:200] for fr in page.frames]
        try:
            report["structure"] = page.evaluate(PLAYER_JS)
        except Exception:
            pass
        report["media_resources"] = sorted(resources, key=lambda r: r["start_ms"])
        report["requests"] = list(rec.events)
        page.screenshot(path=str(out / "manual_after.png"))
    except Exception as e:
        report["error"] = str(e).splitlines()[0]
    finally:
        try:
            page.close()
        except Exception:
            pass
    return report


def main() -> None:
    ap = argparse.ArgumentParser(description="플레이어/클릭 동작 진단")
    ap.add_argument("url")
    ap.add_argument("--rounds", type=int, default=4)
    ap.add_argument("--max-embeds", type=int, default=3)
    ap.add_argument("--headful", action="store_true")
    ap.add_argument("--manual", type=int, metavar="SEC",
                    help="수동 모드: 브라우저를 띄우고 SEC초 동안 사용자가 직접 재생하는 동안 기록 (--headful 자동)")
    ap.add_argument("--proxy")
    ap.add_argument("--out", type=Path, default=Path("probe_out"))
    a = ap.parse_args()
    a.out.mkdir(parents=True, exist_ok=True)

    if a.manual:
        a.headful = True
    rec = Recorder()
    result: dict[str, Any] = {"url": a.url, "targets": []}
    with sync_playwright() as p:
        browser = p.chromium.launch(**get_browser_launch_kwargs(headless=not a.headful, proxy=a.proxy))
        ctx = browser.new_context(user_agent=UA, locale="ko-KR")
        ctx.add_init_script("Object.defineProperty(navigator,'webdriver',{get:()=>undefined});window.chrome={runtime:{}};")
        ctx.on("page", rec.on_popup)

        if a.manual:
            result["targets"].append(manual_session(ctx, rec, a.url, a.manual, a.out))
        else:
            page = ctx.new_page()
            page.goto(a.url, wait_until="domcontentloaded", timeout=30000)
            page.wait_for_timeout(1500)
            for _ in range(4):
                page.evaluate("window.scrollTo(0, document.body.scrollHeight)")
                page.wait_for_timeout(500)
            embeds = page.evaluate(_EMBED_JS)[: a.max_embeds]
            result["embed_urls"] = embeds
            page.close()

            targets = [(f"embed{i}", u) for i, u in enumerate(embeds, 1)] or [("page", a.url)]
            for label, u in targets:
                print(f"\n=== 진단: {label} {u[:100]} ===")
                result["targets"].append(play_rounds(ctx, rec, u, label, a.rounds, a.out, not a.headful,
                                                       referer=a.url if label.startswith("embed") else None))
        browser.close()

    result["popups_total"] = rec.popups
    path = a.out / f"probe_{int(time.time())}.json"
    path.write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")

    # ---- 콘솔 요약
    for t in result["targets"]:
        if t["label"] == "manual":
            print("\n[수동 모드 결과]")
            print("  프레임:", *t.get("frames", []), sep="\n    ")
            print("  재생 중 로드된 미디어 리소스(시간순):")
            for r in t.get("media_resources", []):
                print(f"    {r['start_ms']:>6}ms {r['type']:<8} {r['url'][:100]}  (frame: {r['frame'][:50]})")
            print("  미디어/API 응답:")
            for e in t.get("requests", []):
                print(f"    {e['t']:>5}s {e['status']} {e['kind']:<5} {e['url'][:90]}  referer={e.get('referer', '')[:50]}")
            if rec.popups:
                print("  열린 팝업:", [pp["url"][:60] for pp in rec.popups])
            continue
        loaded = [e for e in t.get("on_load_requests", []) if e["kind"] == "media"]
        print(f"\n[{t['label']}] 로드 직후(클릭 전) 미디어 요청 {len(loaded)}개")
        for e in loaded:
            print(f"     {e['status']} {e['ctype'][:24]:24} {e['url'][:110]}")
        print(f"[{t['label']}] 구조: iframe {len(t.get('structure', {}).get('iframes', []))}개, "
              f"video {len(t.get('structure', {}).get('videos', []))}개, 재생버튼 {len(t.get('structure', {}).get('buttons', []))}개")
        top = (t.get("overlay_stack_at_center") or [])[:2]
        print("  화면 중앙 최상단 요소:", [f"{e['tag']}#{e['id']}.{(e['cls'] or '')[:20]} {e['w']}x{e['h']} {e['position']}" for e in top])
        for r in t["rounds"]:
            media = [e for e in r["new_requests"] if e["kind"] == "media"]
            print(f"  라운드{r['round']} [{r['action']}] 팝업 {len(r['new_popups'])}개 | "
                  f"영상길이 {r['longest_video_sec']:.0f}s | 새 미디어 요청 {len(media)}개")
            for e in media:
                print(f"     {e['status']} {e['ctype'][:24]:24} {e['url'][:110]}")
        if "error" in t:
            print("  오류:", t["error"])
    print(f"\n저장: {path}  (스크린샷: {a.out}/)")


if __name__ == "__main__":
    main()
