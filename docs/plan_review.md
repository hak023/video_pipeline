# 개발계획 검토 및 기존 프로그램 이슈

> **2026-11-13 갱신:** 아래 §2 의 H1~H4, M1~M5, M7 일부, M9 와 §1 불일치는 이후 개발에서 처리되었다.
> 처리 내역은 [계획서 §12](../video_discovery_development_plan.md) 와 [README](../README.md) 참고. 이 문서의 §2~§3 은 **개선 전 코드 기준의 분석 기록**이다.
> 남은 항목: M7(광고 iframe 이 영상 항목으로 세어짐), M8 의 `skip_under` 정책 최종 결정(현재: 사용자 옵션 유지, 본편 근거가 있는 영상만 `skipped_short` 로 완료 처리),
> JWPlayer 광고 이벤트 활용, `download()` 반환 구조화, 실사이트 검증.
> 사용자 답변: `diagnostics.py` 는 유실 → 신규 작성, 운영 PC 의 `ad_blacklist.json` 은 삭제됨 → 증거 기반 동적 학습으로 재설계.

- 작성일: 2026-11-13
- 대상: [video_discovery_development_plan.md](../video_discovery_development_plan.md) 와 현재 소스
- 프로그램 구조는 [program_analysis.md](./program_analysis.md) 참고
- 근거 수준: **코드 정독 기준**이며 실제 실행·재현은 하지 않았다. 실행 확인이 필요한 항목은 "미검증"으로 표시했다.

---

## 0. 요약

1. 계획서의 방향(후보 수집/판별 분리, 실행별 진단 로그, 복수 본편 그룹화)은 현재 코드의 구조적 한계를 정확히 짚고 있어 **타당**하다.
2. 다만 **계획서가 언급하지 않은, 본편 누락·오탐에 직접 영향을 주는 기존 버그/설계 문제**가 있다 (§2의 H1~H4). 이들은 진단 로그를 만들어도 계속 결과를 오염시키므로 Phase 1 과 병행해 먼저 고치는 것을 권한다.
3. 계획서 §12 는 `diagnostics.py` 가 "완료"라고 적었지만 **파일이 존재하지 않는다**. 상태 정정 또는 파일 복구가 필요하다.
4. 계획서에 **정책이 비어 있는 부분**(PENDING 처리, 그룹 키, 점수 규칙, 동시성, 테스트 방침 등)이 있어 구현 전에 확정해야 한다 (§3).

---

## 1. 계획서와 현재 코드의 불일치

| # | 내용 | 조치 |
|---|---|---|
| 1 | §12 에 `diagnostics.py` "신규 구성 … 완료" 로 기록됐으나 워크스페이스에 파일 없음(`diagnostics`, `init_diagnostics`, `log_event` 를 참조하는 코드도 전혀 없음). Git 저장소도 아니라 이력으로 확인 불가. | 다른 위치/브랜치에 있는지 확인. 없으면 §12 상태를 "미완료"로 정정하고 새로 구현 |
| 2 | §4.7 은 "현재 `ad_blacklist.py` 가 정적·동적 패턴 관리"라고 하나, 실제로는 **같은 목록이 `discover.AD_DOMAINS` 에 중복**되어 있고 `ad_domains.txt` 라는 세 번째 소스도 있음 | 계획에 "광고 패턴 소스 단일화" 추가 |
| 3 | §7 파일 목록에 `history.py`, `probe_page.py`, `progress.py`, `verify.py`, `media_tools.py` 가 빠짐. 이 중 `history.py`(skipped_short 의미), `probe_page.py`(discover 비공개 상수 import), `verify.py`(결과 상태 구분)는 변경 영향이 있음 | 영향 범위에 추가 |
| 4 | 계획서 출력 경로는 `logs/runs/...`(상대) 인데 기존 코드는 모두 `ROOT` 기준 경로 사용(`out/`, `history_data/`) | `ROOT / "logs"` 로 고정해 실행 위치에 따라 달라지지 않게 |
| 5 | §5 "콘솔에는 기존 진행 메시지 유지" — 현재 `progress.log()` 가 진행 보드와 섞이지 않게 하는 유일한 출력 경로 | 새 로거도 반드시 `progress.log` 를 통해 콘솔 출력 |

---

## 2. 계획서가 놓친 기존 코드 이슈 (우선순위순)

### 높음 — 본편 누락/오탐을 직접 유발

**H1. 블랙리스트 자동 오염 (호스트 전체 영구 차단)**
- 위치: [main.py:210](../main.py), [main.py:220](../main.py), [main.py:237](../main.py) → [ad_blacklist.py `push`](../ad_blacklist.py)
- `skip_under`(60초) 이하 영상, 또는 "dead URL" 이면 `add_ad_pattern(url)` 호출 → `push` 는 일반 호스트일 때 **호스트 전체**를 `ad_blacklist.json` 에 영구 등록.
- 본편이 올라오는 CDN 호스트에서 짧은 클립/예고편이 한 번 나오거나 만료 URL 이 한 번 나오면 그 호스트의 **본편까지 이후 모든 실행에서 제외**된다. 보호 목록(`_PROTECTED_HOSTS`)에 없는 CDN 은 방어 수단이 없다.
- 계획서 §4.7("짧은 길이/다운로드 실패만으로 등록 금지")과 정면으로 충돌하는 **현재 동작**이므로, 계획 항목에 "기존 자동 등록 호출 제거"를 명시해야 한다. 즉시 조치 가능(해당 3줄 제거 또는 로그만 남김).
- 이미 오염된 `ad_blacklist.json` 이 있다면 `reason` 이 `short_ad_video`/`dead_url:` 인 항목 점검 필요.

**H2. 광고만 발견된 글이 `skipped_short` 로 영구 "완료" 처리**
- 위치: [main.py `_mark_short`](../main.py), [history.py `_verify_record`](../history.py)
- 모든 후보가 60초 이하로 판정되면 항목이 `skipped_short` 가 되고 history 에 기록. `_verify_record` 가 `skipped_short` 를 **완료로 간주**하므로 이후 실행에서 탐색 자체를 하지 않는다(`--force` 제외).
- 본편 탐색이 실패하고 광고(≤60s)만 잡힌 경우가 "정상적으로 짧은 영상이라 스킵"과 구분되지 않는다. 가장 눈에 안 띄는 형태의 본편 누락.
- 제안: 광고 분류 근거가 있는 경우와 "본편 후보 없음"을 구분해 후자는 `no_main_found` 로 기록하고 history 완료 처리하지 않음.

**H3. `_has_real_stream` 이 광고 HLS 를 본편으로 오인해 재생 루프 조기 종료**
- 위치: [discover.py `_has_real_stream`](../discover.py), `_play_until_stream`, `discover_videos`의 B 단계 판단
- 후보에 `.m3u8`/`.mpd` 가 하나라도 있으면 "본편 확보"로 간주(길이 확인 없음). 광고가 HLS 로 먼저 나오면 ①재생 라운드 중단 ②B 단계(글 페이지 직접 재생) 생략 → 본편 요청이 아직 발생하지 않은 채 종료.
- 계획서의 "후보 충분 수집 후 판별" 원칙과 반대. 분류 엔진 도입 시 이 함수를 "본편 근거가 있는 후보 존재"로 바꿔야 한다.

**H4. 광고 패턴이 URL 전체에 대한 부분문자열 매칭**
- 위치: [ad_blacklist.py `is_ad`](../ad_blacklist.py), [discover.py `_is_ad_url`](../discover.py)
- `"ethnic"`, `"syndication"`, `"popads"` 같은 키워드가 호스트뿐 아니라 **경로·쿼리·게시글 slug** 에도 일치한다. 임베드 URL/미디어 URL 에 우연히 포함되면 본편이 제외된다(예: slug 에 해당 단어가 들어간 경우).
- 동적 패턴도 `pat in url` 이라 `ads.example.com` 이 `leads.example.com.cdn…` 류에 걸릴 수 있다.
- 제안: 호스트는 정확/서픽스(점 경계) 비교, 경로 패턴은 별도 필드로 분리. 계획서 §4.7 "경로·파일 패턴 단위 차단"과 함께 정리.

### 중간

| # | 내용 | 위치 |
|---|---|---|
| M1 | `_media_candidate` 가 URL 에 `sora` 또는 `sssrr.org` 문자열이 **어디든** 있으면 광고 필터·프레임 필터 이전에 즉시 통과. 광범위 부분문자열이라 우연 일치 가능 | [discover.py:331](../discover.py) |
| M2 | `nav_fatal = getattr(e, "status", None) in (404, 410, 451)` 는 **항상 False**: `DiscoverNavigationError` 에 `status` 를 설정하는 코드가 없고, `discover_videos` 는 `page.goto` 의 HTTP 상태를 읽지 않음(crawler 는 읽음). 404/410/451 에도 재시도·백오프 소모 | [main.py:400](../main.py), [discover.py:22](../discover.py) |
| M3 | JWPlayer playlist 중 **첫 항목(`pl[0]`)만** 수집, `jwplayer()` 는 첫 인스턴스만 조회. 계획서 §4.5 "playlist 모든 항목"은 현 구조로는 불가능하므로 `_DOM_MEDIA_JS` 재작성이 필요 | `_DOM_MEDIA_JS` |
| M4 | `_prune_segments` 가 **호스트 단위**로 mp4 제거: m3u8 이 있는 호스트의 별도 본편 mp4 도 사라짐. "동일 CDN 에 광고·본편 공존" 원칙과 충돌 | [discover.py:365](../discover.py) |
| M5 | FFmpeg fallback 트리거 조건에 `"generic"` 이 포함됨. yt-dlp 로그에는 `[generic] Extracting URL` 이 거의 항상 나오므로 사실상 **모든 실패(정체/타임아웃 포함)에서 fallback 시도**. 또한 FFmpeg 실패 로그 `f_log` 는 버려짐 → 시도별 실패 원인 소실(계획서 §4.9 요구와 직접 관련) | [download.py:439](../download.py), [download.py:452](../download.py) |
| M6 | 후보 dict 에 **쿠키 전체(name/value)·`authorization` 헤더**가 들어 있고, `report.json`/`debug` 에 미마스킹 URL·최근 요청 50개가 저장됨. 계획서 §6 은 신규 진단 로그만 다루므로 **기존 report·콘솔 DEBUG 출력도 함께 정리**해야 함 | [discover.py `_REPLAY_HEADERS`/`_collect_cookies`](../discover.py), [main.py report 저장](../main.py) |
| M7 | 임베드를 인덱스로 식별(`#vNN`)하는데 임베드 목록에 **광고 iframe(≥400×200)이 포함될 수 있고**, 시도마다 개수가 달라지면 `n_total`/`item_url` 형식(`url` vs `url#v01`)이 바뀜 → history 키 불일치, 광고 임베드가 `no_media_found` 로 남아 글이 `partial` 상태로 매 실행 재탐색 | [main.py:151](../main.py), `_EMBED_JS` |
| M8 | 길이·용량이 사실상 단독 탈락 기준으로 작동(`skip_under`, 1MB 미만 mp4). 계획서 §4.6 "보조 신호"와 상충. `skip_under` 를 사용자 옵션으로 유지할지, 분류 점수로 흡수할지 결정 필요 | [main.py](../main.py), `rank_candidates` |
| M9 | `probe_duration_size`/`verify` 가 `ffprobe` 의 `N/A`, JSON 키 누락, 타임아웃 없는 `subprocess.run` 에서 예외 가능. 예외 시 `_download_candidate` 에서 파일이 삭제되지 않고 워커가 `error` 로 종료 | [media_tools.py](../media_tools.py), [verify.py](../verify.py) |

### 낮음 / 위험 요소 (미검증 포함)

- `_JW_SETUP_JS` 가 초기화가 늦은 정상 플레이어에 `jw.setup({sources:[{file:''}]})` 를 호출해 **플레이어를 빈 소스로 재설정**할 수 있음(미검증).
- A 단계 wrapper 는 `about:blank` 부모에서 iframe 을 로드하므로 **실제 글 페이지의 Referer/Origin 이 없음**. 도메인 잠금 플레이어는 B 단계로 가야 하며, 이때 `frame_url` 의미(wrapper 기준)가 계획서의 `page_url/frame_url` 과 달라질 수 있음. 또한 `embed_url` 이 HTML 에 이스케이프 없이 삽입됨.
- 임베드 HTML 을 `route.fetch()` 로 받아 `fulfill(headers=원본 헤더, body=디코딩된 본문)` 하는 코드는 `content-encoding`/`content-length` 불일치 위험(미검증).
- 호스트 보호 검사: `ad_blacklist.push` 는 `host.endswith("youtube.com")` (점 경계 없음), `discover._is_ad_url` 은 점 경계 사용 → 두 곳의 기준이 다름.
- `ad_blacklist` 의 `_load/_save` 가 예외를 무시(파일 손상 시 조용히 빈 상태), 프로세스 간 락/병합 없음, 항목별 `last_seen`/발견 페이지 없음.
- `crawler._site_label` 이 도메인 뒤 두 라벨만 사용 → `*.co.kr` 같은 사이트에서 서로 다른 사이트가 같은 라벨(`co`)이 되어 외부 링크 필터가 무력화됨.
- `Ctrl+C` 는 `_run_batch` 안에서만 처리 → `--crawl` 의 페이지 순회 단계에서 중단하면 트레이스백으로 종료(계획서 §4.3 "사용자 중단" 요건과 관련).
- `_run_batch` 마지막 `except Exception: pass`, `work()` 의 `except Exception as e` 는 traceback 없이 `str(e)` 만 보존.
- `MAX_CAPTURED=20`, `trace` 12개, `debug_requests` 50개 상한 때문에 **정작 필요한 순간의 증거가 잘릴 수 있음**.
- 실행 이력 없음: `report.json` 은 매번 덮어씀.
- `requirements.txt`/README/테스트 부재. 분석 환경에서는 `yt_dlp` 모듈이 없었음(실제 실행 PC 는 다를 수 있음).
- `probe_page.py` 의 결과 JSON 에 URL/헤더가 그대로 저장됨(스크립트 docstring 도 경고). 계획서 §6 의 마스킹 정책을 공유 모듈로 적용 권장.

참고: `download.py` 의 쿠키 임시 파일은 `finally` 에서 삭제되며 명령행에 쿠키 값이 노출되지 않도록 이미 설계되어 있다(양호).

---

## 3. 계획서 자체에 대한 검토 의견

### 3.1 잘 된 점
- 후보 수집과 분류 분리, 도메인 단독 판정 금지, 오탐 방지 우선(`PENDING` 도입)은 H1~H4 문제의 정확한 해법 방향.
- 실패 시에도 로그를 남기는 원칙, 로그 오류가 본 작업을 중단시키지 않는 원칙, 민감정보 마스킹, 기존 다운로드 엔진 재사용 원칙이 현실적.

### 3.2 구현 전에 확정해야 할 빈틈

1. **`PENDING` 의 최종 처리 정책 미정.** `MAIN` 이 0개이고 `PENDING` 만 있을 때 (a) 다운로드 시도 (b) 실패 처리 중 무엇인지, 시도한다면 "근거 약함" 표기를 어떻게 남길지. 제안: 기본은 PENDING 도 순위대로 다운로드 시도하되 결과에 `low_confidence` 표시 → 광고를 받는 오탐보다 본편 누락 비용이 더 크다는 계획서 원칙과 일치.
2. **점수/규칙 정의 부재.** "보조 점수", "규칙 ID"만 있고 가중치·임계값이 없음. 최소한 초기 규칙 목록(예: 광고 이벤트 관측, 광고 경로 패턴, 플레이어 playlist 포함, 길이 구간, 마스터 playlist 여부)과 AD/MAIN 확정 조건을 표로 합의해야 함. JWPlayer 의 광고 관련 이벤트/상태(플레이어가 직접 알려주는 신호)를 활용할 수 있는지 먼저 조사하는 것이 URL 휴리스틱보다 신뢰도가 높을 가능성이 큼(미검증).
3. **그룹 키 정의 부재(§4.8).** "같은 영상의 화질 스트림"을 무엇으로 판단하는가: master playlist 하위 여부, URL 디렉터리 동일성, 길이 ±오차, playlist 항목 인덱스 등. 현재 `rank_candidates` 가 master 하위 playlist 를 제거하는 로직이 이미 있으므로 이를 확장하는 방식을 제안.
4. **계획의 "그룹"과 기존 "group(임베드 1개)" 용어 충돌.** 현재 코드의 group = 임베드 1개 = history 키 `#vNN`. 한 임베드(플레이어 playlist)에 본편이 여러 개면 history 키/`item_url` 체계를 어떻게 바꿀지 계획이 없음. 기존 history 호환(영상 1개면 글 URL 그대로)을 깨지 않는 규칙 필요.
5. **동시성 설계 부재.** 워커 5개가 동시에 진단 세션에 기록한다. 전역 세션(`init_diagnostics/get_diagnostics`) 방식이면 글/영상별 컨텍스트(스레드 로컬 또는 컨텍스트 객체 전달), 파일 쓰기 락, `candidate_id` 고유성 규칙이 필요. 글 단위 하위 디렉터리 또는 이벤트에 `target`/`item` 필드 추가를 권장.
6. **직렬화 방식은 blacklist(마스킹)가 아니라 allowlist 로.** 후보 dict 에 `cookies`, `request_headers(authorization)` 가 들어 있어, "민감 키를 찾아 마스킹"하면 누락 위험. 로그에 기록할 필드를 명시적으로 허용 목록으로 지정하고 URL 쿼리는 키 이름 기준이 아니라 **기본 마스킹 + 허용 키만 노출**을 고려.
7. **로그 용량·보존 정책 구체값 없음**(§6 "보관 기간 제공"). 실행당 `responses.jsonl` 크기 상한, 보관 개수/일수 기본값, 삭제 명령(또는 옵션) 정의 필요.
8. **성공 기준이 정성적**(§8 "누락 없이 수집하는 방향"). 최소한 실행마다 산출되는 수치(후보 수, MAIN/AD/PENDING 수, 다운로드 성공/실패)와 사용자 피드백(오탐/누락 신고)을 어떻게 누적 비교할지 정의 권장.
9. **테스트 방침 재고.** "별도 테스트 프로그램을 개발 범위로 두지 않는다"는 결정은 존중하되, 분류기·그룹화·마스킹·블랙리스트 매칭은 **네트워크/브라우저 없이 입력→출력으로 검증 가능한 순수 함수**다. 사용자 실행만으로 검증하면 광고 오탐 같은 회귀를 실사이트 실패로만 발견하게 된다. 최소한 이 순수 로직용 소규모 단위 테스트(저장된 후보 JSON 샘플 기반)는 범위에 넣을 것을 제안(최종 결정은 사용자).
10. **`media_classifier` 의 입력 계약 미정.** 분류 시점(수집 완료 후 일괄 vs. 재생 중 점진)과, B 단계 재탐색 결과로 PENDING 을 재평가하는 흐름을 `discover_videos` 반환 구조에 어떻게 싣는지(예: `candidates[].classification`, `groups[].main_groups`) 명시 필요. 현재 반환 구조는 `main._process_item`, `--discover-only`, `probe_page` 가 사용하므로 호환 전략 필요.
11. **`download()` 반환 계약.** 현재 `(bool, str)` + 로그 접두어 파싱. 시도별 결과·검증 정보를 계획서대로 남기려면 구조화 반환이 필요하며, 기존 호출부(`main._download_candidate`, `_short_reason`)와 접두어 규칙을 함께 바꿔야 함. 임시 호환 방안(기존 튜플 유지 + 보조 콜백/컨텍스트로 이벤트 전달)을 계획에 명시.
12. **검증 실패 상태 구분.** `verify` 가 "ffprobe 없음"과 "손상"을 구분하지 않고 둘 다 실패→파일 삭제. 계획서의 `DOWNLOAD_INVALID` 도입 시 "검증 불가(도구 없음)"는 파일을 삭제하지 않는 별도 상태로 분리 권장.

### 3.3 일정(Phase) 조정 제안

| Phase | 내용 |
|---|---|
| **0 (신규, 즉시)** | H1(자동 블랙리스트 등록 중단), H2(광고만 발견 시 완료 처리 방지), M2(HTTP 상태 반영), 콘솔 DEBUG/`report.json` 의 미마스킹 URL 정리, 광고 패턴 소스 단일화. 코드 변경이 작고 효과가 큼 |
| 1 | `diagnostics.py`(복구 또는 신규) + 동시성/allowlist 설계 반영 + `main.py` 연동 |
| 2 | 계획서 Phase 2 (+ M3 JW playlist 전 항목 수집, H3 `_has_real_stream` 개편) |
| 3 | 분류·그룹화·블랙리스트 개편 (H4, M1, M4 포함) |
| 4~5 | 계획서 그대로 (+ M5 FFmpeg 로그 보존, M9/#12 검증 상태 분리) |

---

## 4. 확인이 필요한 질문

1. `diagnostics.py` 는 다른 경로/백업에 있는가, 아니면 새로 작성해야 하는가?
2. `skip_under`(60초 이하 스킵)는 앞으로도 사용자 옵션으로 유지할 것인가, 분류 점수의 일부로 흡수할 것인가? (H2·M8)
3. 본편이 확정되지 않았을 때(`PENDING` 만 존재) 다운로드를 시도할 것인가? (§3.2-1)
4. 기존 `ad_blacklist.json`(런타임 생성물)이 이미 운영 PC 에 쌓여 있는가? 있다면 H1 로 오염된 항목 점검이 필요하다.
5. 대상 사이트는 현재 `s28.testonline.co` 계열 하나인가, 여러 사이트인가? (`normalize_url` 이 호스트를 제외해 사이트 간 경로 충돌 가능, `_site_label` 문제와 직결)
