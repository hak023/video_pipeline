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
python main.py --url "https://사이트/글/"         # 글 1개
python main.py --crawl https://사이트 --end-page 3
python main.py --discover-only                  # 다운로드 없이 후보만 확인
```

옵션은 `python main.py --help` 를 참고하세요. 평소와 같이 실행하면 되고, 별도의 디버그 옵션은 필요 없습니다.

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
