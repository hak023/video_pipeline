from pathlib import Path

path = Path(__file__).resolve().parent / "main.py"
source = path.read_text(encoding="utf-8")
original = source

# Add imports without replacing the existing diagnostics import block.
if "from playwright.sync_api import sync_playwright" not in source:
    anchor = "from diagnostics import ("
    start = source.find(anchor)
    if start < 0:
        raise SystemExit("diagnostics import 위치를 찾지 못했습니다.")
    end = source.find("\n)", start)
    if end < 0:
        raise SystemExit("diagnostics import 블록을 찾지 못했습니다.")
    end += 2
    # attach_page_diagnostics는 기존 diagnostics 모듈의 페이지 이벤트 수집기를 재사용한다.
    if "attach_page_diagnostics" not in source[start:end]:
        block = source[start:end]
        marker = "    sanitize,"
        if marker not in block:
            raise SystemExit("diagnostics import 블록에서 sanitize 항목을 찾지 못했습니다.")
        block = block.replace(marker, marker + "\n    attach_page_diagnostics,", 1)
        source = source[:start] + block + source[end:]
        end = start + len(block)
    source = source[:end] + (
        "\nfrom media_tools import get_browser_launch_kwargs, get_context_kwargs"
        "\nfrom playwright.sync_api import sync_playwright"
    ) + source[end:]

# Add CLI option immediately after --discover-only argument block.
if "--manual-playback" not in source:
    anchor = '    parser.add_argument("--discover-only", action="store_true",'
    pos = source.find(anchor)
    if pos < 0:
        raise SystemExit("--discover-only 옵션 위치를 찾지 못했습니다.")
    close = source.find(")", pos)
    line_end = source.find("\n", close)
    option = (
        '\n    parser.add_argument("--manual-playback", action="store_true",\n'
        '                        help="--url 페이지를 실제 브라우저로 열어 사용자가 직접 재생하고 네트워크/플레이어 로그 수집 (다운로드 안 함)")'
    )
    source = source[:line_end] + option + source[line_end:]

if "def _manual_playback(" not in source:
    dispatch = "def _dispatch(args: argparse.Namespace, opts: Options, headless: bool) -> list[dict] | None:\n"
    if dispatch not in source:
        raise SystemExit("_dispatch 함수 위치를 찾지 못했습니다.")

    function = r'''def _manual_playback(url: str, proxy: str | None = None) -> None:
    """사용자가 브라우저에서 직접 재생하는 동안 네트워크/플레이어 진단을 수집한다."""
    from urllib.parse import urlparse

    diag = get_diagnostics()
    ua = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
          "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36")
    observed: dict[str, dict[str, Any]] = {}
    response_count = 0
    lock = threading.Lock()
    finished = threading.Event()

    def on_response(resp) -> None:
        nonlocal response_count
        try:
            media_url = resp.url
            path_part = urlparse(media_url).path.lower()
            headers = resp.headers
            content_type = (headers.get("content-type") or "").lower()
            if not (path_part.endswith((".mp4", ".m3u8", ".mpd", ".webm", ".m4v", ".ts"))
                    or any(x in content_type for x in ("video/", "mpegurl", "dash+xml"))):
                return

            try:
                raw_headers = resp.request.all_headers()
            except Exception:
                raw_headers = {}
            allowed = {
                "user-agent", "referer", "origin", "accept", "accept-language",
                "sec-ch-ua", "sec-ch-ua-mobile", "sec-ch-ua-platform",
                "sec-fetch-dest", "sec-fetch-mode", "sec-fetch-site",
            }
            safe_headers = {k.lower(): v for k, v in raw_headers.items() if k.lower() in allowed}
            try:
                frame_url = resp.frame.url
            except Exception:
                frame_url = ""

            item = {
                "url": media_url, "status": resp.status, "content_type": content_type,
                "content_length": headers.get("content-length"),
                "content_range": headers.get("content-range"),
                "accept_ranges": headers.get("accept-ranges"),
                "frame_url": frame_url, "request_method": resp.request.method,
                "request_headers": safe_headers,
                "observed_at": datetime.now().astimezone().isoformat(timespec="seconds"),
            }
            with lock:
                if media_url in observed:
                    old = observed[media_url]
                    old["response_count"] = old.get("response_count", 1) + 1
                    old["last_status"] = resp.status
                    old["last_content_range"] = headers.get("content-range")
                else:
                    item["response_count"] = 1
                    observed[media_url] = item
                response_count += 1
                snapshot = list(observed.values())
                total = response_count

            diag.event("manual_playback", "media_response",
                       f"브라우저 미디어 응답 HTTP {resp.status}",
                       level="INFO" if resp.status < 400 else "WARNING",
                       frame_url=frame_url, media_url=media_url,
                       content_type=content_type, content_length=headers.get("content-length"),
                       content_range=headers.get("content-range"),
                       request_method=resp.request.method)
            diag.write_json("manual_playback.json", {
                "page_url": url, "responses": snapshot, "response_event_count": total,
            })
        except Exception as exc:
            log_exception("manual_playback_response", exc, page_url=url)

    snapshot_js = r"""() => {
      const out = {url: location.href, title: document.title,
        readyState: document.readyState, jwplayers: [], videos: [], sources: []};
      try {
        if (typeof jwplayer === 'function') {
          document.querySelectorAll('.jwplayer,[id^="jwplayer"],#player').forEach((el, i) => {
            try {
              const p = jwplayer(el.id || undefined);
              const pl = p && p.getPlaylist ? (p.getPlaylist() || []) : [];
              out.jwplayers.push({id: el.id || ('player-' + i),
                state: p && p.getState ? p.getState() : null,
                position: p && p.getPosition ? p.getPosition() : null,
                duration: p && p.getDuration ? p.getDuration() : null,
                playlist: pl.map((x, n) => ({index:n, title:x.title || null,
                  duration:x.duration || null,
                  sources:(x.allSources || x.sources || []).map(y => ({
                    file:y.file || null, type:y.type || null, label:y.label || null}))}))});
            } catch(e) { out.jwplayers.push({error:String(e)}); }
          });
        }
      } catch(e) { out.jw_error = String(e); }
      document.querySelectorAll('video').forEach((v, i) => out.videos.push({
        index:i, src:v.currentSrc || v.src || null, readyState:v.readyState,
        networkState:v.networkState, paused:v.paused, currentTime:v.currentTime,
        duration:Number.isFinite(v.duration) ? v.duration : null,
        videoWidth:v.videoWidth, videoHeight:v.videoHeight,
        error:v.error ? {code:v.error.code, message:v.error.message} : null}));
      document.querySelectorAll('source').forEach((x, i) =>
        out.sources.push({index:i, src:x.src || x.getAttribute('src'), type:x.type || null}));
      return out;
    }"""

    def wait_for_enter() -> None:
        try:
            input("\n브라우저에서 직접 재생을 시도한 뒤 이 콘솔에서 Enter를 누르세요: ")
        except (EOFError, KeyboardInterrupt):
            pass
        finally:
            finished.set()

    log_event("manual_playback", "started",
              "사용자 직접 재생 진단 시작 (자동 클릭/다운로드 없음)", page_url=url)
    with sync_playwright() as p:
        browser = p.chromium.launch(**get_browser_launch_kwargs(headless=False, proxy=proxy))
        context = browser.new_context(**get_context_kwargs(ua))
        page = context.new_page()
        attach_page_diagnostics(page, "manual")
        page.on("response", on_response)
        try:
            response = page.goto(url, wait_until="domcontentloaded", timeout=45000)
            diag.note_page(url)
            diag.note_frames([frame.url for frame in page.frames])
            log_event("manual_playback", "page_loaded", "수동 재생 페이지 로드",
                      page_url=url, status=response.status if response else None, final_url=page.url)
            print("\n[수동 재생 진단]")
            print("1. 열린 브라우저에서 플레이어를 직접 찾아 재생 버튼을 누르세요.")
            print("2. 재생 성공 또는 오류가 나타날 때까지 기다리세요.")
            print("3. 브라우저는 그대로 두고 이 콘솔에서 Enter를 누르세요.")
            print("4. 쿠키와 Authorization 헤더는 저장하지 않습니다.")
            threading.Thread(target=wait_for_enter, daemon=True).start()
            while not finished.is_set():
                page.wait_for_timeout(250)
            page.wait_for_timeout(1000)

            frame_snapshots = []
            for index, frame in enumerate(page.frames):
                try:
                    data = frame.evaluate(snapshot_js)
                    data["frame_index"] = index
                    data["frame_url"] = frame.url
                    frame_snapshots.append(data)
                except Exception as exc:
                    frame_snapshots.append({"frame_index": index, "frame_url": frame.url,
                        "snapshot_error": f"{type(exc).__name__}: {str(exc)[:250]}"})

            with lock:
                responses = list(observed.values())
                total_events = response_count
            result = {
                "page_url": url, "final_url": page.url, "page_title": page.title(),
                "captured_at": datetime.now().astimezone().isoformat(),
                "response_event_count": total_events, "responses": responses,
                "frames": frame_snapshots,
                "note": "수동 관찰 결과. 자동 다운로드 및 인증정보 저장 없음.",
            }
            diag.write_json("manual_playback.json", result)
            ok_count = sum(1 for x in responses if x.get("status") in (200, 206))
            error_count = sum(1 for x in responses
                              if (x.get("last_status") or x.get("status", 0)) >= 400)
            log_event("manual_playback", "completed", "수동 재생 진단 완료",
                      page_url=url, unique_media_urls=len(responses),
                      successful_media_responses=ok_count, error_media_responses=error_count)
            print(f"\n[수집 완료] 미디어 URL {len(responses)}개 / 정상(200·206) {ok_count}개 / 오류 {error_count}개")
            print(f"[로그] {diag.dir / 'manual_playback.json'}")
        except Exception as exc:
            log_exception("manual_playback", exc, page_url=url)
            raise
        finally:
            try:
                browser.close()
            except Exception:
                pass


'''
    source = source.replace(dispatch, function + dispatch, 1)

old_dispatch = (
    "def _dispatch(args: argparse.Namespace, opts: Options, headless: bool) -> list[dict] | None:\n"
    "    if args.url:\n"
)
new_dispatch = (
    "def _dispatch(args: argparse.Namespace, opts: Options, headless: bool) -> list[dict] | None:\n"
    "    if args.manual_playback:\n"
    "        if not args.url:\n"
    "            raise ValueError('--manual-playback 옵션은 --url 과 함께 사용해야 합니다.')\n"
    "        _manual_playback(args.url, proxy=args.proxy)\n"
    "        return None\n\n"
    "    if args.url:\n"
)
if "if args.manual_playback:" not in source:
    if old_dispatch not in source:
        raise SystemExit("_dispatch 분기 위치를 찾지 못했습니다.")
    source = source.replace(old_dispatch, new_dispatch, 1)

source = source.replace(
    '"mode": "url" if args.url else "crawl" if args.crawl else "discover_only" if args.discover_only else "targets",',
    '"mode": "manual_playback" if args.manual_playback else "url" if args.url else "crawl" if args.crawl else "discover_only" if args.discover_only else "targets",',
    1,
)

if source == original:
    print("변경 사항이 없습니다. 이미 적용된 main.py인지 확인하세요.")
else:
    backup = path.with_suffix(".py.before_manual_playback.bak")
    if not backup.exists():
        backup.write_text(original, encoding="utf-8")
    path.write_text(source, encoding="utf-8")
    print("수동 재생 진단 옵션 적용 완료")
    print(f"백업 파일: {backup}")
    print('실행: python main.py --url "게시글_URL" --manual-playback')
    print("로그: logs/runs/<run_id>/manual_playback.json")
