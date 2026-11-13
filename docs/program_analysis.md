# 비디오 탐색·다운로드 파이프라인 분석 문서

> **주의(2026-11-13):** 이 문서는 **개선 전** 코드 기준의 분석이다. 이후 `diagnostics.py`, `media_classifier.py`, `media_grouping.py` 가
> 추가되고 `ad_blacklist.py`/`discover.py`/`main.py`/`download.py` 가 변경되었다. 현재 동작은 [README](../README.md) 와
> [계획서 §12](../video_discovery_development_plan.md) 를 우선한다. 전체 흐름(§4)과 데이터 구조(§5)의 큰 틀은 유효하며,
> 변경점: 후보 수집 후 판별·그룹화 단계 추가, 광고 필터가 수집 단계에서 분류 단계로 이동, 블랙리스트 자동 영구 차단 제거.

- 작성일: 2026-11-13
- 기준: 워크스페이스 `c:\work\workspace_video_pipeline` 의 소스 정독 결과 (코드를 실제 실행한 검증은 하지 않음. `py_compile` 문법 검사만 통과)
- 짝 문서: [plan_review.md](./plan_review.md) (개발계획 검토 및 기존 이슈)
- 관련 계획서: [../video_discovery_development_plan.md](../video_discovery_development_plan.md)

---

## 1. 프로그램 개요

게시글(웹 페이지) 안에 임베드된 동영상 플레이어를 **Playwright(Chromium)로 열어 미디어 URL(m3u8/mpd/mp4)을 수집**하고,
**yt-dlp(+FFmpeg)로 다운로드**한 뒤 **ffprobe로 검증**하고, **history(JSON)** 에 기록해 재실행 시 중복을 건너뛰는 일괄 처리 도구.

- 입력: ① `targets.yaml`, ② `--url` 글 1개, ③ `--crawl` 카탈로그 URL(페이지 순회로 게시글 목록 자동 수집)
- 출력: `out/*.mp4` 등 미디어 파일, `out/report.json`(또는 `report_crawl.json`), `history_data/*.json`, `ad_blacklist.json`(런타임 생성)
- 핵심 난점: 한 페이지에 **광고 영상과 본편이 섞여 있음**, 재생 버튼을 두 번 눌러야 하는 플레이어, 헤더(Referer/Origin)·쿠키 의존 CDN, 봇 탐지
- 실행 환경: Windows 전제 (`disable_quickedit`, `taskkill`, Chrome 경로 탐색, `CREATE_NEW_PROCESS_GROUP`). 일부 POSIX 분기는 있음.

## 2. 파일 구성

| 파일 | 크기(대략) | 역할 |
|---|---|---|
| [main.py](../main.py) | 36KB | CLI, 배치 오케스트레이션, 항목 단위 처리(후보 순회·재시도·길이 스킵·검증·history 기록), 보고서 |
| [discover.py](../discover.py) | 45KB | 핵심 탐색 엔진: 임베드 검출, 플레이어 재생 유도, 네트워크/DOM 미디어 수집, 광고 필터, 후보 정렬, 길이 추정 |
| [download.py](../download.py) | 23KB | yt-dlp 실행·진행률·타임아웃·중단, 헤더 조합 재시도, FFmpeg 직접 다운로드 fallback, 쿠키 파일, SSRF 차단 |
| [ad_blacklist.py](../ad_blacklist.py) | 6KB | 정적+동적 광고 패턴 스택(JSON 영속), 보호 호스트 |
| [history.py](../history.py) | 12KB | 날짜 파티션 JSON 기반 다운로드 이력, URL 인덱스 중복 판정, in-flight claim |
| [crawler.py](../crawler.py) | 10KB | 카탈로그 페이지에서 게시글 목록/날짜 추출, 페이지 URL 생성, 날짜 범위 필터 |
| [media_tools.py](../media_tools.py) | 3KB | Chrome 채널·launch 옵션, ffmpeg/ffprobe 탐색, 길이/크기 측정 |
| [verify.py](../verify.py) | 0.7KB | 다운로드 파일 검증(존재, 0바이트, 길이 ≥ 5초) |
| [progress.py](../progress.py) | 3KB | 스레드 안전 `log()` 와 한 줄 진행 보드 |
| [probe_page.py](../probe_page.py) | 13KB | 독립 실행 진단 도구(플레이어 구조·오버레이·클릭 후 요청 기록, 다운로드 안 함) |
| [targets.yaml](../targets.yaml) | 0.4KB | 대상 목록 (현재 2건: 공개 HLS 테스트 스트림, 통합 테스트 페이지) |
| [video_discovery_development_plan.md](../video_discovery_development_plan.md) | 21KB | 개선 개발계획 |

없는 것: `requirements.txt`, README, 테스트 코드, `diagnostics.py`/`media_classifier.py`/`media_grouping.py` (계획서에만 존재), `ad_domains.txt`(코드가 선택적으로 읽는 파일, 현재 없음).

모듈 의존 관계:

```text
main ──> crawler ──> media_tools
  │──> discover ──> media_tools, progress, ad_blacklist
  │──> download ──> media_tools
  │──> verify ──> media_tools
  │──> history, progress, ad_blacklist
probe_page ──> discover (비공개 상수 _CLICK_BTN_JS 등 직접 import), media_tools
```

## 3. 실행 방법(CLI, `main.py`)

| 옵션 | 동작 |
|---|---|
| (없음) | `targets.yaml` 의 모든 대상 처리 → `out/report.json` |
| `--only NAME` | 특정 target 만 |
| `--url POST_URL` | 글 1개 즉시 처리, 결과(debug 포함)를 JSON 으로 출력 |
| `--discover-only` | 다운로드 없이 후보 URL·길이만 출력 |
| `--crawl URL` `--start-page` `--end-page` `--since` `--until` `--max-posts` | 카탈로그 순회 → 게시글 수집 → 일괄 처리 → `out/report_crawl.json` |
| `--proxy` `--headful` `--force` | 프록시 / 창 표시 / 중복 무시 |
| `--workers`(5) `--attempts`(3) `--timeout`(3600s) `--stall-timeout`(90s) `--skip-under`(60s) | 병렬·재시도·타임아웃·짧은 영상 스킵 |
| `--history-dir` `--verify-files` `--match-name` `--library DIR` | history 위치 / 파일 실존 확인 / 파일명 기반 중복 방지 |

CLI 로 노출되지 않는 값: `wait_ms`, `click_selector`(crawl 시 기본값 고정), `include_undated`(항상 True).

## 4. 전체 처리 흐름

```text
main()
 ├─ --url      → 대상 1건 구성
 ├─ --crawl    → crawl_page() 반복 → 날짜/중복 필터 → targets 구성 (이름=게시글 제목)
 └─ (기본)     → targets.yaml 로드
        │
        ▼
 _run_batch()  : ThreadPoolExecutor(workers) + 브라우저 세마포어(최대 3) + ProgressBoard
        │   (각 워커) run_target(t)
        ▼
 run_target()  : 게시글 1건
   1. history.is_downloaded / claim (중복·진행중 차단), 옵션 library 이름 매칭
   2. attempt 루프 (최대 --attempts, 재시도마다 wait_ms 1.5배씩 증가, 백오프 5/15/30s + jitter)
        └─ discover_videos() → groups[]  (영상 1개 = group 1개)
        └─ 그룹마다 순차: _process_item()
   3. 결과 집계 → status 결정 → history 기록 → entry 반환
        ▼
 _process_item() : 영상 1개
   1. item_url = 영상 1개면 글 URL, 여러 개면 "글URL#v01" (history 키 호환)
   2. history/library 중복 검사
   3. rank_candidates() 로 후보 정렬 (최대 MAX_CANDIDATES_TRIED=3 개 시도)
   4. 후보별: probe_duration → skip_under 이하면 "광고"로 간주해 건너뜀(+블랙리스트 등록)
              → _download_candidate() → download() → verify() → 길이 재확인
   5. 성공 시 history.record_success, 모든 후보 소진 시 실패/short 처리
```

### 4.1 `discover_videos()` 탐색 단계 (discover.py)

1. 브라우저 실행(`_launch_browser`: headless 면 새 headless 모드 `channel=chromium` 우선, 시스템 Chrome 이 있으면 `channel=chrome`), 스텔스 init script 주입, **의도하지 않은 새 탭(팝업)은 즉시 닫기**.
2. `goto(domcontentloaded, 30s)` → 스크롤(지연 로딩 유도) → `_EMBED_JS` 로 임베드 URL 수집 (`script.fp-embed` JSON, `iframe`: 이름이 `upns|tellmewhy|embed` 이거나 400×200 이상) → `_is_ad_url` 필터, 최대 50개.
3. 임베드가 있으면:
   - **A) 임베드 직접 열기** `_extract_embed_stream`: 빈 페이지에 `<iframe src=embed>` wrapper 를 `set_content` 로 만들고 열기 → anti-adblock 스크립트 주입 → JWPlayer 초기화 폴링(5s + 최대 15s) → 필요 시 `jwplayer().setup()` 시도 → DOM/JW 소스 수집 → `_play_until_stream`(재생 최대 4라운드) → 재생 후 DOM 재수집 → 네트워크 캡처와 병합 → `_prune_segments` → 쿠키 첨부.
   - **B) 글 페이지에서 직접 재생**: A 에서 "본편 같은 스트림"(`_has_real_stream`)이 없는 그룹이 있을 때만. 플레이어 호스트의 프레임에서 발생한 요청만 수집(`player_found`)해 해당 그룹에 병합.
   - 임베드가 있으면 여기서 반환(C 로 내려가지 않음).
4. **C) 임베드 없음**: `click_selector` 클릭 → 재생 유도 → 모든 프레임의 네트워크/`<video>` 소스에서 후보 수집 (광고 도메인 필터만 적용) → **그룹 1개**로 반환.

반환 구조: `[{"index": n, "embed_url": str|None, "candidates": [...], "debug": {...}}]`

### 4.2 광고 필터가 적용되는 지점(분산되어 있음)

| 위치 | 규칙 |
|---|---|
| `_EMBED_JS` + `discover_videos` | 임베드 URL 이 `_is_ad_url` 이면 제외, 작은 iframe 제외 |
| `_media_candidate` | `_is_ad_url` 이면 거절(`ad_domain`), 플레이어 호스트 프레임이 아니면 거절(`other_frame`). 단 URL 에 `sora`/`sssrr.org` 가 있으면 **필터 이전에 무조건 통과** |
| `on_response`(discover_videos), DOM 수집 | 광고 도메인이면 수집 단계에서 폐기 |
| `_prune_segments` | m3u8/mpd 가 있는 호스트의 `.mp4` 는 조각으로 보고 제거 |
| `rank_candidates` | `Content-Length` < 1MB 인 mp4 제외 |
| `main._process_item` | 길이 ≤ `skip_under` 이면 광고 취급 + 블랙리스트 등록, 폐기 URL(dead)도 등록 |

`_is_ad_url` = `_PROTECTED_HOSTS` 보호 검사 → `ad_blacklist`(정적+동적 부분문자열) 또는 `discover.AD_DOMAINS`(+`ad_domains.txt`) 부분문자열 일치.

### 4.3 후보 정렬 `rank_candidates`

`m3u8(master → media → 기타) > mpd > mp4(Content-Length 큰 순)`. master 가 있으면 같은 디렉터리의 하위 playlist 는 제외. 화질 선택은 하지 않고 yt-dlp 기본 선택(최고 화질)에 맡김.

### 4.4 다운로드 `download.download()` (download.py)

1. `validate_url`: http(s)만, 사설/루프백/예약 IP 차단(SSRF).
2. `_build_profiles`: 헤더 조합 목록 — ① 브라우저가 실제 보낸 헤더 ② Referer ③ Referer+Origin ④ 임베드 origin 을 Referer 로 ⑤ 글 URL 을 Referer 로 ⑥ UA 만.
3. 쿠키는 Netscape 임시 파일로 yt-dlp 에 전달(`finally` 에서 삭제).
4. 프로필별 `_download_once`: yt-dlp 실행(`--ignore-config`, `--no-playlist`, `--limit-rate 5M`, `--max-filesize`, mp4 병합) + 출력 reader 스레드 + 총시간/정체/용량/중단 감시. 실패 로그에 `generic|404|Unable to download webpage|Unsupported URL` 가 있으면 **FFmpeg 직접 다운로드(`-c copy`) fallback**.
5. 401/403 일 때만 다음 헤더 프로필로 재시도. 첫 실패 시 `_diagnose_http`(Range 0-0 요청) 로 원인 진단, GCS 류 `NoSuchKey/BillingNotEnabled/404/410` 이면 `[DEAD ...]` 로 즉시 종료.
6. 실패 시 이번 시도에서 생긴 부분 파일만 정리.

실패 로그의 접두어(`[DEAD`, `[DIAG`, `[STALLED`, `[TIMEOUT`, `[SIZE`, `[SECURITY]`, `[CANCELLED`)를 `main._short_reason` 과 상태 판정이 파싱한다 → **로그 문자열이 사실상 API 계약**이다.

### 4.5 검증 `verify.verify`

존재·0바이트 확인 → `probe_duration_size`(ffprobe, 없으면 ffmpeg stderr 파싱) → `duration ≥ 5s` 이면 통과. 실패 시 `main` 이 파일을 삭제하고 실패로 처리.

## 5. 핵심 데이터 구조

### 5.1 후보(candidate) dict
```text
url, content_type, referer, frame_url?, request_headers?(재생용 헤더 allowlist, authorization 포함),
user_agent, page_url, cookies?(name/value/domain/... 전체), dom_duration?
```
※ 식별자(candidate_id), 분류 상태, 근거 필드는 현재 없음.

### 5.2 그룹(group) dict
`{index, embed_url, candidates[], debug{headless, final_url, title, body, frames, rejected[≤12], captured_urls, debug_requests[≤50], debug_responses[≤50], log_popups, inpage, popups_closed}}`

### 5.3 항목 결과(item result)
`{index, name, url, status, reason?, candidates(개수), source, selected_content_type, output, meta, duration, download_log_tail, debug?}`

status 값: `ok`, `skipped`, `skipped_short`, `no_media_found`, `failed`, `cancelled`, `partial`(글 단위), `error`(워커 예외).
`DONE_STATES = {ok, skipped, skipped_short}` — 이 상태는 재시도 대상에서 빠지고 history 에서 "완료"로 취급됨.

### 5.3.1 글(post) 결과 status 결정
항목이 1개면 항목 필드를 평탄화, 여러 개면 `items[]`. 일부만 성공하면 `partial`. 여러 영상 글은 전부 완료되어야 글 단위 history 를 기록해 다음 실행에서 탐색 자체를 생략.

### 5.4 history 레코드 (`history_data/<post_date|undated>.json`)
키 = `normalize_url`(호스트 제외 path 소문자 + `#vNN`), 값 = `{name, url, post_date, status(ok|skipped_short|failed), output_file, duration, size, downloaded_at|skipped_at|last_attempt, reason, attempts}`.
중복 판정은 날짜 무관 URL 인덱스. `skipped_short` 도 "완료"로 간주. `--verify-files` 시 `ok` 의 파일 실존 확인.

### 5.5 `ad_blacklist.json`
`{패턴(호스트 또는 호스트/버킷): {reason, count, added_from}}`. 정적 패턴은 코드(`_STATIC_AD_PATTERNS`)에 있음.

### 5.6 `targets.yaml`
`targets[]: name, page_url, license(정보용), wait_ms, click_selector, proxy?, date?`

## 6. 동시성 모델

- 글 단위 병렬: `ThreadPoolExecutor(workers=5)`; 브라우저 동시 실행은 `Semaphore(min(workers, 3))` 로 제한(탐색 구간만). 첫 배치는 1.5초 간격으로 시차 시작, 작업 사이 1~3초 랜덤 대기.
- 글 내부 영상은 **순차** 처리. 한 글 안에서는 `sync_playwright()` 를 호출마다 새로 시작.
- 스레드 로컬: `discover._own`(자기 페이지 여부, 팝업 카운트).
- 전역 상태: `CANCEL`(Event), `_names_in_use`(Lock), `HistoryManager`(RLock + 인메모리 캐시), `blacklist` 싱글톤(Lock), `progress._board`.
- Ctrl+C 는 `_run_batch` 의 `as_completed` 루프에서만 잡힘 → `CANCEL` 세트 후 워커가 정리.
- 프로세스 간: history 는 저장 시 디스크 내용을 병합(다중 프로세스 부분 대응), 블랙리스트는 병합/락 없음.

## 7. 주요 하드코딩·튜닝 상수

| 값 | 위치 | 의미 |
|---|---|---|
| `MAX_CANDIDATES_TRIED=3` | main | 영상당 시도할 후보 수 |
| `BACKOFF=(5,15,30)`, `MAX_CONCURRENT_BROWSERS=3` | main | 재시도 대기 / 동시 브라우저 |
| `skip_under=60` | main | 이 길이 이하 영상 스킵(광고 가정) |
| `MAX_PLAY_ROUNDS=4`, `REAL_VIDEO_SECONDS=90` | discover | 재생 시도 횟수 / 본편 판단 길이 |
| `MAX_CAPTURED=20`, `MAX_VIDEOS_PER_PAGE=50`, trace 12, debug 50 | discover | 수집 상한 |
| `MIN_REAL_MP4_BYTES=1MB` | discover | 광고 배너 mp4 판단 |
| `DEFAULT_TOTAL_TIMEOUT=3600`, `STALL=90`, `MAX_BYTES=8GB`, `--limit-rate 5M` | download | 다운로드 제한 |
| 사이트 특화 문자열 `upns|tellmewhy|embed`, `fp-embed`, `iamcdn.net`, `sora`, `sssrr.org`, `1465544.xyz`, `saawsedge`, `embedorigin.cc`(주석) | discover | 특정 사이트/플레이어 대응 |
| 정적 광고 패턴 ~40개 | ad_blacklist, discover(중복) | 광고 호스트/키워드 |
| `_PROTECTED_HOSTS` | ad_blacklist | 블랙리스트 등록 금지 호스트 |

## 8. 외부 의존성

- Python 3.11 확인(분석 환경). 필요한 패키지: `playwright`, `pyyaml`, `yt-dlp`. 문서화된 목록(`requirements.txt`) 없음.
- 분석 환경 확인 결과: `playwright`, `yaml`, `ffmpeg`, `ffprobe` 는 존재, **`yt_dlp` 모듈은 미설치**(PATH 의 `yt-dlp` 실행 파일 존재 여부는 미확인 — `download.py` 는 둘 중 하나를 사용). 실제 실행 PC 와 다를 수 있음.
- 시스템 Google Chrome 이 있으면 우선 사용(VPN/SecretDNS 호환 목적, `media_tools` 주석).
- `resolve_ffmpeg`: PATH 의 ffmpeg, 없으면 Playwright 번들 `ffmpeg-win64.exe`. (번들 빌드가 HLS/HTTPS 다운로드를 지원하는지는 **미검증**)

## 9. 현재 진단/로깅 현황

- 콘솔: `progress.log()` 로 한글 메시지 출력(`[SKIP]`, `[FAIL]`, `[RETRY]`, `[Ad Filter]` 등). 파일 로그 없음.
- `out/report.json`: 매 실행 덮어씀(실행 이력 없음). 항목별 `debug`(최종 URL, 프레임, 거절된 요청, 최근 요청 50개) 포함 — **URL 이 마스킹되지 않음**.
- `discover.py` 에 특정 호스트(`sora`, `ssrr.org`, `1465544.xyz`, `saawsedge`, `iamcdn`) 요청을 항상 콘솔에 출력하는 DEBUG 로그가 고정되어 있음.
- 예외 삼킴(`except ...: pass` 형태) grep 기준 대략 46곳 (discover 19, probe_page 7, download 6, main 5, crawler 4, history 3, ad_blacklist 2).
- `probe_page.py` 가 별도 수동 진단 도구 역할 (`probe_out/*.json`, 스크린샷).

## 10. 확장 시 참고 (개발계획과의 접점)

- 계획서의 `diagnostics.py` 는 **현재 워크스페이스에 없음**(자세한 내용은 [plan_review.md](./plan_review.md) §1).
- 후보 수집/분류 분리를 구현하려면 광고 필터가 걸린 위 §4.2 의 모든 지점이 영향을 받음.
- `download()` 의 반환이 `(bool, str)` 이고 상태 판정이 로그 접두어에 의존하므로, 시도별 진단·검증 정보를 늘리려면 구조화된 결과 객체로의 전환이 필요.
- `probe_page.py` 가 `discover` 의 비공개 상수를 import 하므로 해당 상수 이름/위치 변경 시 함께 수정 필요.
