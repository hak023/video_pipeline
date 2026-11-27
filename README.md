# 비디오 탐색·다운로드 파이프라인

게시글 페이지에서 영상(본편)을 찾아 광고와 구분하고, 본편별로 다운로드·검증해 `out/` 에 저장합니다.
구조와 내부 동작은 [docs/program_analysis.md](docs/program_analysis.md), 개발계획은
[video_discovery_development_plan.md](video_discovery_development_plan.md) 를 참고하세요.

## 설치

```powershell
pip install -r requirements.txt
```

- ffmpeg / ffprobe 가 PATH 에 있어야 합니다 (검증·HLS 병합). 없으면 검증은 "미검증"으로 표시되고 파일은 지워지지 않습니다.
- 시스템에 Google Chrome 이 있으면 우선 사용합니다. 없으면 `playwright install chromium` 이 필요합니다.

## 실행

```powershell
python main.py                                  # targets.yaml 전체
python main.py --url "https://사이트/글/"         # 글 1개 (다른 옵션 없이 이것만으로 충분)
python main.py --crawl https://사이트 --end-page 3
python main.py --discover-only                  # 다운로드 없이 후보만 확인
```

`--url` 만 써도 되는 이유: 나머지는 모두 기본값이 있습니다 (workers 5, attempts 3, 총 1시간/무진행 90초 타임아웃,
`out/` 저장, `history_data/` 이력, `logs/` 로그·ZIP 자동 생성). 파일명은 URL 마지막 경로 조각(`/글-제목/` → `글-제목.mp4`,
영상 여러 개면 `글-제목 (01).mp4`...)이고, 영상 URL(`.m3u8`/`.mp4`)을 직접 줘도 동작합니다. 결과 JSON 은 콘솔에 출력됩니다.

**알아둘 기본값 하나:** `--skip-under 60` — **60초 이하 영상은 받지 않습니다**(광고/예고편 제외 목적). 짧은 본편을 받으려면
`--skip-under 0` 을 함께 주세요. 짧은 영상만 발견되면 `no_main_found` 로 끝나며 사유에 이 안내가 표시됩니다.
같은 URL 을 다시 실행하면 이미 받은 것은 건너뛰고(`--force` 로 재다운로드), 실패한 글은 다시 시도합니다.

옵션은 `python main.py --help` 를 참고하세요. 평소와 같이 실행하면 되고, 별도의 디버그 옵션은 필요 없습니다.

403 이후 다운로드 버튼은 재생을 준비한 뒤 최대 30초 확인합니다. `openx_handoff.py`의
`ENABLE_STREAMING_FALLBACK` 기본값은 `True`입니다. 버튼이 없으면 재생 응답을 캡처하고
발견한 미디어 URL의 직접 다운로드도 시도합니다. 직접 요청은 브라우저의 인증 정보를 사용하지 않으므로
서버가 거부할 수 있습니다. 작업이 연 재생 탭은 Chrome 연결을 끊기 전에 닫습니다.
재생 응답 본문은 요청 시작부터 관찰한 CDP 세션만 받을 수 있어, 플레이어 iframe이 열리는 즉시
네트워크 관찰을 붙입니다. 그래도 미디어 요청이 먼저 시작됐다면 버튼이 없다고 판정한 뒤 플레이어 프레임을 한 번 다시 불러와
요청을 처음부터 관찰합니다(`player_reloaded`).
CDP 스트림 연결이 실패하고 전체 길이를 알 수 있으면, 버튼이 없는 것으로 판정한 뒤
해당 미디어 응답을 받은 플레이어 프레임에서 1 MiB씩 추가 Range 요청을 시도합니다.
서비스워커 또는 서버가 인증된 요청에 `206`과 정확한 `Content-Range`를 반환하고, 교차 출처에서는
CORS가 해당 헤더를 노출해야 합니다. 이를 만족하지 않으면 복구가 실패할 수 있습니다.
서비스워커가 주소의 `#fragment`로 영상을 구분하는 플레이어를 위해 추가 요청에는 원래 주소의 fragment를
그대로 붙입니다. fragment 값은 기록하지 않고 유무(`has_fragment`)만 남깁니다.
이 추가 요청까지 실패하고 받은 바이트가 없으면 10초 뒤 `recovery_failed`로 중단합니다.
스트리밍 영상은 [config.yaml](config.yaml)의 `streaming_max_duration_seconds` 이상이면 건너뜁니다
(기본값 `7200`초, `0`이면 제한 해제). download 버튼이 있는 경우와 일반 다운로드에는 적용하지 않습니다.
플레이어에서 길이가 확인되면 수신을 중단하고, 확인되지 않으면 수신을 계속한 뒤 완성 파일에서도 확인합니다.
끝까지 길이를 알 수 없으면 경고를 남기고 저장합니다. 건너뛴 항목을 설정 변경 후 다시 시도하려면 `--force`를 사용하세요.
길이 판정 전 받은 캡처 조각과 진단 파일은 캡처 하위 폴더에 남을 수 있습니다.
수신한 스트림 바이트는 누적량으로 별도 표시하며, 응답 완료 및 전체 범위 확인 전에는 완료로 간주하지 않습니다.
전체 수신이 끝난 영상은 일반 다운로드와 같은 제목·순번·중복 방지 규칙을 적용해 `out/`에 저장합니다.
확장자는 미디어 유형을 따르며, 유형을 알 수 없으면 `.mp4`를 사용합니다.
미완료 조각과 캡처 진단 목록은 별도의 캡처 하위 폴더에 남습니다.
바이트가 증가하지 않으면 미디어 응답이 없는 경우 45초, 응답만 있거나 수신이 멈춘 경우 90초 후 중단합니다.
CDP 본문 읽기 오류와 네트워크 요청 실패의 상세 내용은 민감값을 마스킹해 진단 로그와 캡처 목록에 남깁니다.
캡처 목록의 `capture_trace`에는 CDP 세션·요청 ID별 응답, 스트림 연결 및 실패 사유와 바이트 수,
직접 다운로드·브라우저 대체 요청 결과가 최근 200건까지 남습니다. `events.jsonl`에는 처음 200건과
이후 오류 이벤트가 기록됩니다. 본문과 쿠키·인증 헤더는 기록하지 않습니다.
범위 재수신 진단은 `browser_stream_queued`, `browser_stream_waiting_for_frame`, `browser_stream_source`,
`browser_stream_chunk`, `browser_stream_error` 이벤트에서 확인할 수 있습니다.
`cdp_response`와 `cdp_stream_error`의 `request_tracked=false`는 CDP 세션이 그 요청보다 늦게 붙었다는 뜻입니다.

직접 다운로드가 본편을 받는 동안에는 같은 바이트를 Chrome에서 한 번 더 받지 않습니다. 그 URL의 CDP 스트림 연결을
미루고 플레이어의 해당 영상을 일시정지합니다(`openx_handoff.py`의 `PAUSE_PLAYER_DURING_DIRECT`로 끌 수 있음).
직접 다운로드는 읽기 오류로 끊기면 받은 위치부터 최대 3번 이어 받고, 그래도 끝내지 못하면 재생을 다시 시작해
CDP 스트림으로 받습니다. 스트림을 늦게 연결해 Chrome이 앞부분을 이미 버렸다면(`cdp_stream_late`) 받은 조각을
실제 위치에 저장하고, 빈 구간이 남으면 플레이어 프레임을 한 번 다시 불러옵니다(`uncaptured_range_reload`).
직접 다운로드가 처음부터 거부되는 서버에서는 이 절감이 적용되지 않고 이전과 같이 CDP 스트림으로만 받습니다.
`Network.getResponseBody`는 16 MiB를 넘는 응답에는 호출하지 않으며(`cdp_body_skipped`), 2초 이상 걸리면
`cdp_body_slow`를 남깁니다. 재생 캡처 루프 한 바퀴가 10초 이상 걸리면 `loop_stall`에 drain 소요 시간과 함께 기록합니다.

재생 시도 뒤 `PLAYER_INIT_WAIT_SECONDS`(12초) 안에 플레이어가 만들어지지 않고 로더만 보이면(`player_init_stuck`),
사용자 Chrome 프로필에 남은 그 임베드의 서비스워커·IndexedDB 상태가 초기화를 막은 것으로 보고
플레이어 프레임의 저장소만 지운 뒤 프레임을 한 번 다시 불러옵니다(`player_storage_reset`).
지우는 대상은 임베드 origin과 글 페이지 site 조합의 서비스워커·IndexedDB·Cache Storage·localStorage이며,
쿠키와 다른 사이트 데이터는 건드리지 않습니다. CDP로 지우지 못하면 프레임 안에서 직접 지웁니다.
복구되면 `player_init_recovered`, 그래도 멈춰 있으면 `player_init_still_stuck`(ERROR)을 남기고 기존 판정대로 진행합니다.
각 이벤트의 `player`에는 로더·JW 상태·video 상태·안내 창 문구·localStorage 접근 결과가,
`storage`에는 서비스워커 등록(`sw_registrations`)과 IndexedDB 버전·열기 결과(`idb_open`, `timeout`이면 다른 연결 뒤에서 대기 중)가 들어갑니다.
캡처가 멈추거나 버튼 없음으로 끝날 때도 같은 내용을 `player_state_at_stop`으로 남깁니다.
재생 탭의 console 오류와 실패한 요청은 `browser/` 로그에 `handoff:<영상 id>` 라벨로 기록합니다
(플레이어의 DevTools 탐지가 반복하는 console.clear/table 출력은 제외). 임베드를 글 페이지에 올리지 못해
wrapper로 열었다면 `embed_article_fallback`이 남습니다.

403 이후 Chrome 재생 캡처는 한 프로세스에서 **최대 5개**입니다 (`--workers 10`이어도
재생 캡처는 5개를 넘지 않음). 재생 탭은 이 상한 안에서 병렬로 시작합니다.
기존 사용자 탭, 탐색용 브라우저, 별도 상한의 download 탭은
이 재생 캡처 5개에 포함되지 않습니다. 여러 프로세스를 동시에 실행하면 제한은 공유되지 않습니다.

재생 페이지에서 열린 추가 팝업은 확장 프로그램의 차단 여부와 관계없이 닫습니다
(`https://` 광고 주소, `chrome-extension://`, `about:blank` 포함). 재생 준비·버튼 대기·
스트리밍 중에 계속 처리하며 `[광고 탭]` 로그를 남깁니다. URL 문자열만으로 Chrome 전체를
정리하지 않고, 해당 재생 페이지와 그 팝업에서 열린 탭만 대상으로 하므로 기존 사용자 탭,
다른 병렬 재생 탭, 파이프라인이 따로 여는 정상 download 탭은 보존합니다.

## 실행 로그 (항상 자동 생성)

실행할 때마다 고유한 Run ID 가 만들어지고 `logs/runs/<run_id>/` 에 진단 자료가 쌓입니다.
성공/실패/예외/Ctrl+C 어느 경우에도 `summary.json` 이 남고, 실행이 끝나면 경로가 출력됩니다.

```text
logs/
  runs/<run_id>/
    summary.json          실행 결과, 단계별 소요시간, 실패 단계/사유, 후보·다운로드 집계
    run.log               읽기 쉬운 시간순 로그 (콘솔 메시지 포함)
    events.jsonl          구조화 이벤트 (단계, 이벤트명, 레벨, 페이지/프레임/후보 ID, 세부정보)
    candidates.json       발견한 모든 미디어 후보 + MAIN/AD/PENDING 판정, 점수, 근거(rule), 그룹, 제외 사유
    players.json          플레이어/iframe 분석 (JW 초기화 여부, 대기시간, 수집/거절 수 등)
    downloads.json        다운로드 시도 (헤더 프로필·엔진별 결과, 검증 정보, SUCCESS/FAILED/DOWNLOAD_INVALID ...)
    report.json           대상별 결과 (민감값 마스킹됨)
    blacklist.json        종료 시점의 블랙리스트 상태 스냅샷
    browser/              console.log, page_errors.log, request_failures.jsonl, responses.jsonl
    exceptions/           예외 traceback (발생 단계, 컨텍스트 포함)
  zips/<run_id>.zip       위 폴더를 묶은 전달용 파일
```

**문제가 생기면** `logs/zips/<run_id>.zip` 을 전달해 주세요. 받은 쪽은 `python analyze_run.py <zip 또는 실행 폴더>` 로
상태, 후보별 판별 근거, 플레이어/다운로드 결과, 원인 힌트를 한 번에 볼 수 있습니다 (인자 없이 실행하면 가장 최근 실행).
쿠키, Authorization, 서명 토큰, 긴 쿼리 값은
기록 단계에서 마스킹되며(같은 값은 같은 `<masked:xxxxxx>` 태그), 쿠키 파일·브라우저 프로필은 포함되지 않습니다.
ZIP 생성에 실패해도 원본 폴더는 보존됩니다.

- `--log-retain N` : 최근 N 회 실행만 보관 (기본 30, 0 이면 정리하지 않음). 직접 삭제하려면 `logs/` 를 지우면 됩니다.
- 로그 기록에 실패(디스크 등)해도 다운로드 작업은 중단되지 않고 stderr 에 1회 경고만 출력합니다.

### summary.json 의 status

| 값 | 의미 |
|---|---|
| `OK` | 모든 대상이 완료/건너뜀 |
| `PARTIAL` | 일부만 성공 |
| `FAILED` | 성공한 대상 없음 (`failure_stage`/`failure_reason` 참고) |
| `ERROR` | 처리되지 않은 예외로 종료 |
| `CANCELLED` | 사용자 중단 |
| `RUNNING` | 비정상 종료(강제 종료 등)로 최종 갱신이 없었음 |

`failure_stage`: `navigation`, `crawl`, `media_discovery`, `classification`(본편 후보 없음/광고만 발견), `download`, `exception`.

## 본편/광고 판별

수집 단계에서는 광고 의심 후보도 버리지 않고 모두 모은 뒤, 근거를 점수화해 `MAIN` / `AD` / `PENDING` 으로 나눕니다.

- 도메인·길이·용량은 단독으로 AD 를 확정하지 못합니다 (광고 네트워크 HARD 규칙, 광고 전용 경로 토큰 등 주된 근거가 필요).
- `PENDING`(근거 부족)은 버리지 않습니다. 본편이 없으면 PENDING 도 시도하며 결과에 `low_confidence` 로 표시됩니다.
- 같은 영상의 화질별 스트림(master/하위 playlist, `_1080p` 등)은 하나의 그룹으로 묶고, 서로 다른 본편은 별도 항목(`#v01`, `#v02` ...)으로 각각 다운로드합니다.
- 결과 상태 `no_main_found` 는 "광고만 발견/본편 근거 없는 짧은 영상만 발견" 으로, history 에 **완료로 기록하지 않아** 다음 실행에서 다시 탐색합니다.
  (`--skip-under` 로 건너뛴 영상이 본편 근거(MAIN)가 있을 때만 `skipped_short` 로 완료 처리됩니다.)

## 블랙리스트 (`ad_blacklist.json`, 자동 생성·학습)

파일이 없으면 코드의 정적 규칙만으로 시작하고, 실행하면서 증거를 쌓아 스스로 판단합니다. 삭제해도 안전합니다.

- 정적 규칙은 **호스트명에만** 적용됩니다 (게시글 제목/쿼리에 키워드가 있어도 광고로 보지 않음). HARD = 광고 네트워크, SOFT = 사이트 실측 패턴(본편 근거가 충분하면 분류기가 뒤집을 수 있음). `ad_domains.txt`(선택, 한 줄에 하나)의 항목은 SOFT 로 추가됩니다.
- 동적 규칙은 범위가 `호스트/첫 경로 디렉터리` 이며, **본편 관측 0회**이고 다음 중 하나일 때만 활성화됩니다.
  강한 증거(광고 전용 경로 등) 2건 이상 + 서로 다른 페이지 2곳 이상, 또는 증거 합계 3건 이상 + 페이지 3곳 이상.
- 짧은 길이/폐기된 URL 같은 **약한 증거만으로는 절대 차단하지 않습니다.** 약한 증거만으로 활성화된 규칙은
  분류에서 보조 근거(`AD_DYNAMIC_WEAK`)로만 쓰여 AD 를 확정하지 못합니다 (본편이 같이 발견될 때 근거 없는 후보를 걸러내는 용도).
- 다운로드·검증까지 성공한 본편이 확인된 범위는 즉시 차단이 해제되고 이후 활성화되지 않습니다.
- 90일간 관측되지 않은 항목은 사라집니다. 구버전 형식의 파일은 `.legacy.bak` 으로 백업하고 사용하지 않습니다.

## 운영 PC 투입 전 체크리스트 (광고·숨은 임베드가 많은 사이트)

**처음 실행 방법 (권장)**
1. `pip install -U yt-dlp playwright` 후 `python -m unittest discover -s tests` (단위 테스트)로 환경 확인. yt-dlp 는 사이트/CDN 변경으로 자주 깨지므로 주기적으로 갱신하세요 (버전은 `summary.json` 의 `meta.env` 에 기록됨).
2. **소량으로 시작:** `python main.py --discover-only --targets 소량.yaml` 로 몇 개 글만 탐색해 보고 `python analyze_run.py` 로 후보 판별(MAIN/AD/PENDING)과 근거를 눈으로 확인한 뒤 본 실행. (판별이 틀리면 `ad_domains.txt` 에 광고 호스트를 한 줄씩 추가)
3. `--workers 1`~`2` 로 시작 (기본 5 는 사이트 차단/IP 밴 위험). 사이트가 막으면 `--headful`, `--proxy`.
4. 반복 실패 글이 많으면 `--cooldown-hours 24`.

**자동으로 적용되는 방어**
- Chrome 샌드박스 ON (`--no-sandbox` 제거; 광고 악성 스크립트로부터 PC 보호). 컨테이너/루트에서만 필요하면 `VP_NO_SANDBOX=1`.
- 광고가 유발하는 자동 파일 다운로드(`.exe` 등)는 받지 않음, 광고 팝업 탭은 즉시 닫음.
- 글 1개 탐색 시간 예산 360초(`VP_DISCOVER_BUDGET` 로 조정). 초과 시 남은 임베드는 생략(로그에 `budget_exceeded`) — 광고 iframe 이 많아도 끝없이 늘어지지 않음.
- 광고가 글 페이지를 다른 사이트로 이동시키면(`page_hijacked`) 그 페이지에서의 클릭/재생은 생략.
- 다운로드 전 디스크 여유 3GB 미만이면 중단(`low_disk_space`), 파일당 8GB·1시간·무진행 90초 상한, 내부망 주소 차단(SSRF).

**알아둘 한계 (실사이트 로그로 확인 필요)**
- 광고 iframe(400×200 이상)이 영상 항목으로 세어져 `no_media_found` 항목이 생기면 글이 `partial` 로 남고 매 실행 재탐색됩니다. 항목 번호(`#vNN`)도 광고 iframe 수에 따라 달라질 수 있습니다.
- 60초를 넘는 광고(긴 프리롤)는 길이만으로는 본편과 구분되지 않습니다. 광고 호스트/경로 규칙에 의존하므로 첫 실행의 `low_confidence`/`candidates.json` 을 꼭 검토하세요.
- 서명 URL 은 몇 분 뒤 만료됩니다. 영상이 많은 글에서 뒤쪽 항목이 403 이면 다음 시도에서 재탐색해 새 URL 로 이어받습니다(`--attempts`).
- 안티봇(Cloudflare 등) 챌린지는 우회하지 않습니다. HTTP 403/429/503 이 반복되면 속도를 낮추거나 `--headful`/`--proxy`.
- 실행 중 PC 절전/백신의 파일 잠금(`WinError 32`)은 일시적 실패로 보이고 재시도로 복구됩니다.

## 테스트

네트워크/브라우저 없이 도는 핵심 로직 테스트 (마스킹, 진단 세션, 블랙리스트 학습, 분류, 그룹화, 검증, 재탐색 보류, 로그 분석):

```powershell
python -m unittest discover -s tests -v
```

실제 Chrome + yt-dlp + ffmpeg 로 로컬 서버를 처리하는 E2E (기본은 건너뜀). `test_e2e_local`(약 3분)은 복수 본편·광고 제외·JW playlist·광고만 있는 글·404,
`test_e2e_crawl`(약 3분, 외부 네트워크 불필요)은 `--crawl` 전체 경로(2페이지 카탈로그, 날짜 필터, HLS master, playlist, 광고만 있는 글)를 확인합니다.

```powershell
$env:RUN_E2E=1; python -m unittest tests.test_e2e_local tests.test_e2e_crawl -v
```

## 파일 이름 규칙 (영상이 여러 개인 글)

| 상황 | 파일명 | history 키 |
|---|---|---|
| 영상 1개 | `제목.mp4` | 글 URL |
| 영상 여러 개 | `제목 (01).mp4`, `제목 (02).mp4` ... (플레이어 playlist/DOM 순서) | `글URL#v01`, `#v02` ... |
| 이미 같은 이름의 파일이 있음(다른 글/영상) | `제목 [a1b2c3].mp4` (URL 해시 6자리, 여러 개 글이면 `제목 (02) [a1b2c3].mp4`) | - |

- 제목이 길어도 끝의 ` (NN)` 순번은 잘리지 않습니다 (파일명 상한 120자, `out/` 경로가 깊으면 Windows MAX_PATH 를 넘지 않도록 자동으로 더 줄임).
- 이름 비교는 대소문자를 구분하지 않습니다 (`Movie` 와 `movie` 는 같은 파일로 취급 → 해시 부여).
- **덮어쓰기 방지:** 다른 글/영상의 파일은 `--force` 에서도 덮어쓰지 않습니다. `--force` 는 history 에 기록된 **같은 항목의 이전 파일**만 같은 이름으로 다시 받습니다.
- 다운로드 실패 시 정리(`.part` 등)는 해당 항목의 임시 파일만 지웁니다 (`Vol` 실패가 완료된 `Vol.1.mp4` 를 지우지 않음).
- 주의: 순번은 이번 실행에서 발견된 순서입니다. 다음 실행에서 영상 수/순서가 달라지면 같은 `#vNN` 이 다른 영상을 가리킬 수 있습니다(계획서 §13.4-4).

## 반복 실패 글의 재탐색 보류 (선택)

본편을 못 찾은 글은 history 에 `failed` 로 남아 매 실행 다시 탐색됩니다. 부담이 되면 보류를 켤 수 있습니다 (기본 꺼짐).

```powershell
python main.py --crawl https://사이트 --cooldown-hours 24 --cooldown-after 2
```

같은 글이 누적 2회 이상 실패했고 마지막 시도가 24시간 이내면 건너뜁니다(`reason=cooldown`). `--force` 로 무시할 수 있고, 성공하면 해제됩니다.
글 URL 자체의 실패 기록만 대상입니다 (여러 영상 글의 개별 `#vNN` 항목 실패는 포함하지 않음).
