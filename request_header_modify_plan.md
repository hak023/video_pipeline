이제 충분한 증거를 모았습니다. 분석 결과를 바탕으로 가설을 검증하고 개발계획을 재수립하겠습니다.

---

## 📊 분석 결과 요약

### 1. 핵심 증거: URL 경로의 첫 세그먼트가 **요청 시점의 epoch-ms**다
| run | 접두사 | 변환 결과 (KST) | 수집 시각 | 일치 |
|-----|--------|----------------|----------|-----|
| 1차 | 1794751553322 | 2026-11-15 23:05:53.322 | 23:05:53 | ✅ |
| 2차 | 1794753770106 | 2026-11-15 23:42:50.106 | 23:42:50 | ✅ |
| 3차 | 1794754864223 | 2026-11-16 00:01:04.223 | 00:01:04.500 | ✅ (270ms 전) |

→ **동영상 URL은 재생 순간마다 타임스탬프 기반으로 새로 발급(mint)됨**. 과거에 수집한 URL은 몇 분 뒤에도 유효할 수 있으나, 세션이 바뀌면 400/403/청구 오류 발생 가능.

### 2. 수동 재생 성공 요청의 실제 헤더는 로그에서 **누락**됨
- `diagnostics.py:49` `_DROP_KEYS = {"request_headers", ...}` → `sanitize()`가 `request_headers`를 통째로 제거
- 수집기(`main.py:1091-1095`)는 `range`, `x-goog-user-project`를 허용 리스트에 두고 수집하지만, JSON 저장 시 소멸
- 노트에 *"userProject와 Range 관련 정보는 보존"*이라 적혀 있으나 실제 데이터는 사라짐

### 3. 자동 다운로드 경로(discover → download)의 헤더 재현 부족
| 모듈 | 허용 리스트 | 누락된 핵심 헤더 |
|------|------------|-----------------|
| `discover.py:_REPLAY_HEADERS` (L117-121) | referer, origin, accept, user-agent, sec-ch-ua*, sec-fetch-* | **range, if-range, x-goog-user-project** |
| `download.py:_REPLAY_OK` (L508-512) | 동일 | **동일** |

→ 브라우저가 보낸 **Range: bytes=0-** (전체 파일 206 응답 유도), **Requester-Pays용 x-goog-user-project**가 yt-dlp에 전달되지 않음.

### 4. 미디어 요청 직전의 400 응답 2회 (00:01:04.273~)
- `network_events`에 `console_error: "status of 400"` 2건이 media_request 바로 직전에 기록
- 임베드 플레이어가 **세션 초기화/URL 발급 API**를 호출하고, 그 응답(쿠키/토큰/URL)로 직후 미디어 요청이 성공함
- 자동 탐색(discover)도 같은 임베드를 여니 발급은 되지만, **발급→다운로드 사이 시간차**로 URL이 만료되거나 세션이 끊어질 위험

### 5. `UserProjectAccountProblem` 원인 가능성 3가지
| 가능성 | 설명 | 검증 필요 |
|--------|------|-----------|
| ① GCS Requester-Pays 버킷, 자동 요청에 `userProject` 없음 | 브라우저는 `x-goog-user-project` 헤더/쿼리를 보내나 자동 요청은 누락 | 헤더 재현 후 재시도 |
| ② 세션 바인딩 URL (발급 직후만 유효) | 타임스탬프 접두사가 요청 시점과 결합; 일정 시간 지나면 400/청구 오류 반환 | URL 발급 후 즉시 다운로드 or 재발급 |
| ③ 2026-11-14 제공자 측 일시적 billing 중단 | 11/14 실패 → 11/15-16 수동 성공으로 복구된 경우 | 현재 자동 실행으로 재확인 |

---

## 🎯 수정된 가설 검증 결론

> **가설: "수동 재생은 브라우저가 Range 헤더와 userProject 인증을 실시간으로 보내서 성공하고, 자동 다운로드는 둘 다 없어서 billing 오류가 난다"**
> 
> **→ 부분적으로 맞음. 핵심은 Range/x-goog-user-project 헤더 재현 누락 + URL 세션 만료 가능성임.**
> 
> - 수동 로그에서 헤더가 보이지 않는 건 **저장 단계(sanitize)에서 삭제됐기 때문**이지, 브라우저가 안 보냈기 때문이 아님.
> - 자동 경로는 해당 헤더를 허용 리스트에조차 두고 있지 않아 **재현 자체가 불가능**.
> - 타임스탬프 접두사 구조로 보아 **URL 발급 후 즉시(수 초~수십 초 내) 다운로드**하거나, 실패 시 **재발급(재탐색) 후 재시도** 로직이 필요.

---

## 📋 개발 계획 (Revised)

### Phase 0 — 진단 데이터 보존 (즉시, 30분)
| 작업 | 파일 | 내용 |
|------|------|------|
| 0-1 | `diagnostics.py` | `_DROP_KEYS`에서 `"request_headers"` 제거. 대신 `mask_headers()` 허용 리스트(`_REPLAY_HEADERS`와 동일)로 민감값만 마스킹하고 구조 보존 |
| 0-2 | `main.py:_manual_playback` | `safe_url()`이 `userProject` 쿼리 키를 보존하도록 `safe_query_keys`에 `"userproject"` 이미 있음 — 확인만 |
| 0-3 | `main.py` | `manual_playback.json`에 `request_headers`가 기록되도록 `persist()` 수정(이미 `item["request_headers"]` 추가 중, sanitize만 피하면 됨) |

**목표**: 다음 수동 실행부터 `manual_playback.json`에 `request_headers: {range: "bytes=0-", "x-goog-user-project": "..."}` 등이 실제로 남게 함 → 가설 ① 확정.

---

### Phase 1 — 헤더 재현 파이프라인 보강 (1시간)
| 작업 | 파일 | 내용 |
|------|------|------|
| 1-1 | `discover.py:_REPLAY_HEADERS` | `range`, `if-range`, `if-none-match`, `if-modified-since`, `x-goog-user-project` 추가 |
| 1-2 | `download.py:_REPLAY_OK` | 동일하게 추가 |
| 1-3 | `download.py:_build_profiles` | 프로필 0(브라우저 원본 헤더)에 `range`, `x-goog-user-project`가 있으면 자동 포함되도록 이미 동작 — 허용 리스트만 늘리면 됨 |
| 1-4 | `download.py` | yt-dlp에 `--add-header "Range: bytes=0-"` 전달 (프로필 0에 range 없을 때 기본 프로필로 주입) |

**목표**: 자동 다운로드가 브라우저와 같은 Range 요청 + userProject 헤더를 보내도록 함.

---

### Phase 2 — URL 신선도 보장 & 재발급 재시도 (2시간)
| 작업 | 파일 | 내용 |
|------|------|------|
| 2-1 | `discover.py` | 수집된 후보에 `url_timestamp`(접두사 epoch-ms) 필드 추가. `discover_videos` 반환 후보 dict에 포함 |
| 2-2 | `main.py` | `--max-url-age SEC` 옵션 추가(기본 300초). 후보 선택 시 `now - url_timestamp > max_age`면 **재탐색 트리거** |
| 2-3 | `main.py:_process_item` | `UserProjectAccountProblem` 감지 시 `fatal=True` 대신 **"재탐색 후 재시도 1회" 브랜치**로 분기 (기존 353-368 라인 수정) |
| 2-4 | `main.py` | 재탐색 함수 `_rediscover_and_download(group, ...)` 추가: 같은 embed_url로 `discover_videos` 재실행 → 새 후보로 `_download_candidate` 호출 |

**목표**: URL 만료/세션 끊김을 자동 복구. "청구 오류 = 치명적 아님"으로 처리.

---

### Phase 3 — 동일 세션 다운로드 (선택, 2시간)
*Phase 1-2로 해결 안 될 때만*
| 작업 | 파일 | 내용 |
|------|------|------|
| 3-1 | `discover.py` | `_extract_embed_stream` 마지막에 **즉시 다운로드 훅** 추가: 후보 URL + 쿠키 + 헤더를 받아 그 자리에서 `download()` 호출 (브라우저 컨텍스트 유지) |
| 3-2 | `main.py` | `--same-session-download` 플래그로 활성화. 발견→다운로드 사이 갭 0에 수렴 |

**목표**: 세션 바인딩이 극도로 짧은 사이트(이 케이스) 대응.

---

### Phase 4 — 히스토리/블랙리스트 정리 & 회귀 테스트 (1시간)
| 작업 | 내용 |
|------|------|
| 4-1 | `history_data/undated.json`의 해당 항목 `status="failed"` → `status="pending_retry"` 로 변경(또는 `--force` 시 무시되도록 이미 동작) |
| 4-2 | `ad_blacklist.json`에 `storage.googleapis.com/mediastorage`가 오염되지 않았는지 확인(`_PROTECTED_HOSTS` 효과 검증) |
| 4-3 | 로컬 E2E 테스트 추가: `tests/test_e2e_gcs_timestamped.py` — 가짜 GCS 서버가 타임스탬프 접두사 URL 발급, 만료 시 400/청구 에러 반환 → 파이프라인이 재탐색 후 성공하는지 검증 |

---

## ✅ 즉시 실행 가능한 검증 단계 (당장 해볼 것)

```bash
# 1) Phase 0 적용 후 수동 재실행 → request_headers 기록 확인
python main.py --url "https://s29.watchfreejavonline.co/%ec%9c%a0%eb%b0%b1%ec%84%a4-362540/" --manual-playback
cat logs/runs/<latest>/manual_playback.json | jq '.responses[0].request_headers'

# 2) Phase 1-2 패치 후 강제 재시도 (히스토리 무시)
python main.py --url "https://s29.watchfreejavonline.co/%ec%9c%a0%eb%b0%b1%ec%84%a4-362540/" --force

# 3) 그래도 실패하면 Phase 3 검토
```

---

## 🔧 변경 파일 요약 (우선순위)

| 우선순위 | 파일 | 변경 핵심 |
|----------|------|-----------|
| P0 | `diagnostics.py` | `_DROP_KEYS`에서 `request_headers` 제거, `mask_headers`로 민감값만 가림 |
| P1 | `discover.py` | `_REPLAY_HEADERS`에 `range`/`x-goog-user-project` 추가 |
| P1 | `download.py` | `_REPLAY_OK` 동일 추가; 기본 프로필에 `Range: bytes=0-` 주입 |
| P2 | `main.py` | `--max-url-age`, `UserProjectAccountProblem` 재탐색 재시도 분기, `_rediscover_and_download` |
| P2 | `discover.py` | 후보 dict에 `url_timestamp` 필드 추가 |
| P3 | (선택) `discover.py` | 동일 세션 즉시 다운로드 훅 |

이 계획대로 진행하면 **수동 재생 성공 조건(Range + userProject + 세션 신선 URL)**을 자동 파이프라인에 모두 이식할 수 있습니다. Phase 0-1만 적용해도 바로 헤더 재현이 되므로, 먼저 그것부터 적용하고 수동 로그로 `x-goog-user-project` 실제 값 유무 확인 → Phase 2로 확장 권장합니다.



## ✅ P0 작업 완료 요약

### 변경 내역

**1. `diagnostics.py` — 핵심 수정**
```python
# 변경 전
_DROP_KEYS = {"cookies", "cookie", "request_headers", "authorization", "set-cookie", "headers"}

# 변경 후
_DROP_KEYS = {"cookies", "cookie", "authorization", "set-cookie", "headers"}
```
- `request_headers`를 top-level drop 목록에서 제거 → 구조 자체는 보존
- 하위 내부 키는 그대로 `_DROP_KEYS` 재귀 필터링(`sanitize`가 dict를 재귀 순회)로 처리되므로, **`cookie` / `authorization` / `set-cookie`는 계속 제거**, `range`, `x-goog-user-project`, `referer`, `user-agent` 등은 보존됨
- `sanitize()` docstring / `_CANDIDATE_FIELDS` 주석도 새 동작에 맞게 갱신

**2. `tests/test_core.py` — 기존 테스트 동기화 (임시)**
- `test_sanitize_drops_cookies_and_request_headers`를 새 정책에 맞게 갱신: `request_headers` 구조 보존 + 내부 `authorization` 제거 + `range`/`x-goog-user-project` 유지 검증

**3. `main.py` — 확인만 수행 (변경 불필요)**
- `safe_query_keys`에 `"userproject"` 포함 확인 (URL 쿼리 userProject 보존)
- `_manual_playback`의 `persist()` / `on_response`가 `request_headers`를 기록하도록 구현되어 있었음 → P0에서는 **diagnostics의 sanitize만** 고쳐주면 JSON에 실제로 남음

### 검증 결과
```
Ran 43 tests in 0.813s — OK
```
P0 변경 후 전체 코어 테스트 통과.

### 남은 상태 (P1 이후 진행 예정)
- `main.py`는 기존부터 수정 상태(`M main.py`, 321줄)였으며 이번 P0에서는 직접 변경하지 않았음
- P1(헤더 재현): `discover.py:_REPLAY_HEADERS` / `download.py:_REPLAY_OK`에 `range`, `if-range`, `x-goog-user-project` 추가 — 계획대로 미진행

**참고**: 기존 테스트 43개는 모두 통과했습니다. 다만 `test_sanitize_drops_cookies_and_request_headers`가 과거 정책(요청 헤더 전체 drop)을 단언하던 테스트였기에, 새 동작에 맞게 수정했습니다. (사용자 요청대로 테스트 보강은 이후 별도 진행)

## 후속 정보
현재 P1과 P2가 완료된것으로 보임. 확인 필요.

## 수동 성공시 manual playback 결과 로그
'''
{
  "page_url": "https://s29.watchfreejavonline.co/%ec%9c%a0%eb%b0%b1%ec%84%a4-362540/",
  "captured_at": "2026-11-16T00:01:12.220383+09:00",
  "response_event_count": 1,
  "responses": [
    {
      "url": "https://storage.googleapis.com/mediastorage/1794754864223/fmv3js6rok/167382505.mp4",
      "status": 206,
      "status_text": "",
      "content_type": "video/mp4",
      "content_length": "167382505",
      "content_range": "bytes 0-167382504/167382505",
      "accept_ranges": "bytes",
      "content_disposition": null,
      "response_headers": {
        "accept-ranges": "bytes",
        "cache-control": "no-cache, no-store, must-revalidate, max-age=0",
        "content-length": "167382505",
        "content-type": "video/mp4",
        "content-range": "bytes 0-167382504/167382505"
      },
      "request_method": "GET",
      "resource_type": "media",
      "frame_url": "https://1465544.xyz/?v=[OMITTED]",
      "observed_at": "2026-11-16T00:01:04.500+09:00"
    }
  ],
  "downloads": [],
  "network_events": [
    {
      "event": "page_attached",
      "observed_at": "2026-11-16T00:00:52.217+09:00",
      "page_url": "about:blank"
    },
    {
      "event": "page_attached",
      "observed_at": "2026-11-16T00:00:52.219+09:00",
      "page_url": "about:blank"
    },
    {
      "event": "console_error",
      "observed_at": "2026-11-16T00:00:54.248+09:00",
      "page_url": "https://s29.watchfreejavonline.co/%ec%9c%a0%eb%b0%b1%ec%84%a4-362540/",
      "message": "Failed to load resource: net::ERR_BLOCKED_BY_CLIENT"
    },
    {
      "event": "console_error",
      "observed_at": "2026-11-16T00:00:54.249+09:00",
      "page_url": "https://s29.watchfreejavonline.co/%ec%9c%a0%eb%b0%b1%ec%84%a4-362540/",
      "message": "Failed to load resource: net::ERR_BLOCKED_BY_CLIENT"
    },
    {
      "event": "page_error",
      "observed_at": "2026-11-16T00:00:54.256+09:00",
      "page_url": "https://s29.watchfreejavonline.co/%ec%9c%a0%eb%b0%b1%ec%84%a4-362540/",
      "error": "afa3igj"
    },
    {
      "event": "page_error",
      "observed_at": "2026-11-16T00:00:54.257+09:00",
      "page_url": "https://s29.watchfreejavonline.co/%ec%9c%a0%eb%b0%b1%ec%84%a4-362540/",
      "error": "afa3igj"
    },
    {
      "event": "console_error",
      "observed_at": "2026-11-16T00:00:54.308+09:00",
      "page_url": "https://s29.watchfreejavonline.co/%ec%9c%a0%eb%b0%b1%ec%84%a4-362540/",
      "message": "Failed to load resource: net::ERR_BLOCKED_BY_CLIENT"
    },
    {
      "event": "console_error",
      "observed_at": "2026-11-16T00:00:54.309+09:00",
      "page_url": "https://s29.watchfreejavonline.co/%ec%9c%a0%eb%b0%b1%ec%84%a4-362540/",
      "message": "Failed to load resource: net::ERR_BLOCKED_BY_CLIENT"
    },
    {
      "event": "console_error",
      "observed_at": "2026-11-16T00:00:54.330+09:00",
      "page_url": "https://s29.watchfreejavonline.co/%ec%9c%a0%eb%b0%b1%ec%84%a4-362540/",
      "message": "Failed to load resource: net::ERR_BLOCKED_BY_CLIENT"
    },
    {
      "event": "console_error",
      "observed_at": "2026-11-16T00:00:54.331+09:00",
      "page_url": "https://s29.watchfreejavonline.co/%ec%9c%a0%eb%b0%b1%ec%84%a4-362540/",
      "message": "Failed to load resource: net::ERR_BLOCKED_BY_CLIENT"
    },
    {
      "event": "console_error",
      "observed_at": "2026-11-16T00:00:54.334+09:00",
      "page_url": "https://s29.watchfreejavonline.co/%ec%9c%a0%eb%b0%b1%ec%84%a4-362540/",
      "message": "Failed to load resource: net::ERR_BLOCKED_BY_CLIENT"
    },
    {
      "event": "console_error",
      "observed_at": "2026-11-16T00:00:54.335+09:00",
      "page_url": "https://s29.watchfreejavonline.co/%ec%9c%a0%eb%b0%b1%ec%84%a4-362540/",
      "message": "Failed to load resource: net::ERR_BLOCKED_BY_CLIENT"
    },
    {
      "event": "console_error",
      "observed_at": "2026-11-16T00:00:54.416+09:00",
      "page_url": "https://s29.watchfreejavonline.co/%ec%9c%a0%eb%b0%b1%ec%84%a4-362540/",
      "message": "Failed to load resource: net::ERR_BLOCKED_BY_CLIENT"
    },
    {
      "event": "console_error",
      "observed_at": "2026-11-16T00:00:54.417+09:00",
      "page_url": "https://s29.watchfreejavonline.co/%ec%9c%a0%eb%b0%b1%ec%84%a4-362540/",
      "message": "Failed to load resource: net::ERR_BLOCKED_BY_CLIENT"
    },
    {
      "event": "console_error",
      "observed_at": "2026-11-16T00:00:54.420+09:00",
      "page_url": "https://s29.watchfreejavonline.co/%ec%9c%a0%eb%b0%b1%ec%84%a4-362540/",
      "message": "Failed to load resource: net::ERR_BLOCKED_BY_CLIENT"
    },
    {
      "event": "console_error",
      "observed_at": "2026-11-16T00:00:54.421+09:00",
      "page_url": "https://s29.watchfreejavonline.co/%ec%9c%a0%eb%b0%b1%ec%84%a4-362540/",
      "message": "Failed to load resource: net::ERR_BLOCKED_BY_CLIENT"
    },
    {
      "event": "console_error",
      "observed_at": "2026-11-16T00:00:54.440+09:00",
      "page_url": "https://s29.watchfreejavonline.co/%ec%9c%a0%eb%b0%b1%ec%84%a4-362540/",
      "message": "Failed to load resource: net::ERR_BLOCKED_BY_CLIENT"
    },
    {
      "event": "console_error",
      "observed_at": "2026-11-16T00:00:54.442+09:00",
      "page_url": "https://s29.watchfreejavonline.co/%ec%9c%a0%eb%b0%b1%ec%84%a4-362540/",
      "message": "Failed to load resource: net::ERR_BLOCKED_BY_CLIENT"
    },
    {
      "event": "console_error",
      "observed_at": "2026-11-16T00:00:54.446+09:00",
      "page_url": "https://s29.watchfreejavonline.co/%ec%9c%a0%eb%b0%b1%ec%84%a4-362540/",
      "message": "Failed to load resource: net::ERR_BLOCKED_BY_CLIENT"
    },
    {
      "event": "console_error",
      "observed_at": "2026-11-16T00:00:54.447+09:00",
      "page_url": "https://s29.watchfreejavonline.co/%ec%9c%a0%eb%b0%b1%ec%84%a4-362540/",
      "message": "Failed to load resource: net::ERR_BLOCKED_BY_CLIENT"
    },
    {
      "event": "console_error",
      "observed_at": "2026-11-16T00:00:55.038+09:00",
      "page_url": "https://s29.watchfreejavonline.co/%ec%9c%a0%eb%b0%b1%ec%84%a4-362540/",
      "message": "Failed to load resource: net::ERR_BLOCKED_BY_CLIENT"
    },
    {
      "event": "console_error",
      "observed_at": "2026-11-16T00:00:55.040+09:00",
      "page_url": "https://s29.watchfreejavonline.co/%ec%9c%a0%eb%b0%b1%ec%84%a4-362540/",
      "message": "Failed to load resource: net::ERR_BLOCKED_BY_CLIENT"
    },
    {
      "event": "console_error",
      "observed_at": "2026-11-16T00:01:04.273+09:00",
      "page_url": "https://s29.watchfreejavonline.co/%ec%9c%a0%eb%b0%b1%ec%84%a4-362540/",
      "message": "Failed to load resource: the server responded with a status of 400 ()"
    },
    {
      "event": "console_error",
      "observed_at": "2026-11-16T00:01:04.275+09:00",
      "page_url": "https://s29.watchfreejavonline.co/%ec%9c%a0%eb%b0%b1%ec%84%a4-362540/",
      "message": "Failed to load resource: the server responded with a status of 400 ()"
    },
    {
      "event": "media_request",
      "observed_at": "2026-11-16T00:01:04.498+09:00",
      "url": "https://storage.googleapis.com/mediastorage/1794754864223/fmv3js6rok/167382505.mp4",
      "method": "GET",
      "resource_type": "media",
      "frame_url": "https://1465544.xyz/?v=[OMITTED]",
      "is_navigation_request": false
    },
    {
      "event": "media_response",
      "observed_at": "2026-11-16T00:01:04.500+09:00",
      "url": "https://storage.googleapis.com/mediastorage/1794754864223/fmv3js6rok/167382505.mp4",
      "status": 206,
      "status_text": "",
      "content_type": "video/mp4",
      "content_length": "167382505",
      "content_range": "bytes 0-167382504/167382505",
      "accept_ranges": "bytes",
      "content_disposition": null,
      "response_headers": {
        "accept-ranges": "bytes",
        "cache-control": "no-cache, no-store, must-revalidate, max-age=0",
        "content-length": "167382505",
        "content-type": "video/mp4",
        "content-range": "bytes 0-167382504/167382505"
      },
      "request_method": "GET",
      "resource_type": "media",
      "frame_url": "https://1465544.xyz/?v=[OMITTED]"
    },
    {
      "event": "console_error",
      "observed_at": "2026-11-16T00:01:06.048+09:00",
      "page_url": "https://s29.watchfreejavonline.co/%ec%9c%a0%eb%b0%b1%ec%84%a4-362540/",
      "message": "Failed to load resource: net::ERR_BLOCKED_BY_CLIENT"
    },
    {
      "event": "console_error",
      "observed_at": "2026-11-16T00:01:06.050+09:00",
      "page_url": "https://s29.watchfreejavonline.co/%ec%9c%a0%eb%b0%b1%ec%84%a4-362540/",
      "message": "Failed to load resource: net::ERR_BLOCKED_BY_CLIENT"
    },
    {
      "event": "final_snapshot",
      "observed_at": "2026-11-16T00:01:12.220+09:00",
      "frames": [
        {
          "url": "https://s29.watchfreejavonline.co/%ec%9c%a0%eb%b0%b1%ec%84%a4-362540/",
          "title": "유백설-362540 - Watch Free JAV Online - Free JAV Porn Online",
          "readyState": "complete",
          "jwplayers": [],
          "videos": [
            {
              "index": 0,
              "src": null,
              "readyState": 0,
              "networkState": 0,
              "paused": true,
              "currentTime": 0,
              "duration": null,
              "videoWidth": 0,
              "videoHeight": 0,
              "error": null
            }
          ],
          "sources": [],
          "frame_index": 0,
          "frame_url": "https://s29.watchfreejavonline.co/%ec%9c%a0%eb%b0%b1%ec%84%a4-362540/"
        },
        {
          "url": "chrome-error://chromewebdata/",
          "title": "creative.whitetrafsa.com",
          "readyState": "complete",
          "jwplayers": [],
          "videos": [],
          "sources": [],
          "frame_index": 1,
          "frame_url": "chrome-error://chromewebdata/"
        },
        {
          "url": "chrome-error://chromewebdata/",
          "title": "thekav.co",
          "readyState": "complete",
          "jwplayers": [],
          "videos": [],
          "sources": [],
          "frame_index": 2,
          "frame_url": "chrome-error://chromewebdata/"
        },
        {
          "url": "chrome-error://chromewebdata/",
          "title": "creative.whitetrafsa.com",
          "readyState": "complete",
          "jwplayers": [],
          "videos": [],
          "sources": [],
          "frame_index": 3,
          "frame_url": "chrome-error://chromewebdata/"
        },
        {
          "url": "chrome-error://chromewebdata/",
          "title": "creative.whitetrafsa.com",
          "readyState": "complete",
          "jwplayers": [],
          "videos": [],
          "sources": [],
          "frame_index": 4,
          "frame_url": "chrome-error://chromewebdata/"
        },
        {
          "url": "chrome-error://chromewebdata/",
          "title": "thekav.co",
          "readyState": "complete",
          "jwplayers": [],
          "videos": [],
          "sources": [],
          "frame_index": 5,
          "frame_url": "chrome-error://chromewebdata/"
        },
        {
          "url": "about:blank",
          "title": "",
          "readyState": "complete",
          "jwplayers": [],
          "videos": [],
          "sources": [],
          "frame_index": 6,
          "frame_url": "about:blank"
        },
        {
          "url": "https://1465544.xyz/?v=gI-cXG9gx",
          "title": "유백설-362540.mp4",
          "readyState": "complete",
          "jwplayers": [
            {
              "id": "player",
              "state": "playing",
              "position": 6.612742,
              "duration": 1813.079206,
              "playlist": [
                {
                  "index": 0,
                  "title": null,
                  "duration": null,
                  "sources": [
                    {
                      "file": "https://storage.googleapis.com/mediastorage/1794754864223/fmv3js6rok/167382505.mp4",
                      "type": "mp4",
                      "label": "480p"
                    },
                    {
                      "file": "https://storage.googleapis.com/mediastorage/1794754864223/gmla7urkpl5/335703721.mp4",
                      "type": "mp4",
                      "label": "720p"
                    },
                    {
                      "file": "https://storage.googleapis.com/mediastorage/1794754864223/yzvwn59ikp/984822727.mp4",
                      "type": "mp4",
                      "label": "1080p"
                    }
                  ]
                }
              ]
            }
          ],
          "videos": [
            {
              "index": 0,
              "src": "https://storage.googleapis.com/mediastorage/1794754864223/fmv3js6rok/167382505.mp4",
              "readyState": 4,
              "networkState": 1,
              "paused": false,
              "currentTime": 6.717832,
              "duration": 1813.079206,
              "videoWidth": 854,
              "videoHeight": 480,
              "error": null
            }
          ],
          "sources": [],
          "frame_index": 7,
          "frame_url": "https://1465544.xyz/?v=[OMITTED]"
        }
      ]
    }
  ],
  "note": "Cookie/Authorization 및 서명 URL 인증 쿼리 값은 저장하지 않음. userProject와 Range 관련 정보는 보존."
}
'''

## 진행 업데이트 (2026-11-16)

이 섹션이 위의 과거 진행 메모를 대체한다.

### 확인된 상태

- P0 헤더 보존, P1 헤더 재현, P2 URL 신선도/재탐색 코드는 저장소에 존재한다.
- 코어 테스트 43개는 통과했으나, 이전에는 자동 헤더 재현·타임스탬프 재탐색·billing 재시도를 직접 검증하는 테스트가 없었다.
- 제공된 수동 재생 JSON은 HTTP 206과 전체 Content-Range를 보이지만 `request_headers`가 없다. 따라서 `Range` 요청 값과 `x-goog-user-project`의 실제 존재 여부는 아직 확인되지 않았다. `downloads: []`이므로 브라우저 재생 요청 성공과 파일 저장 성공도 구분해야 한다.
- 타임스탬프와 관측 시각의 일치는 URL이 해당 시각에 발급됐다는 가설과 부합하지만, 만료 시점이나 billing 오류와의 인과관계를 증명하지 않는다.

### 이번 진단 보강

- 자동 다운로드 시도 로그에 실제 재생 헤더 값을 기록한다. 인증 헤더와 API 키 값은 제외하고, URL은 기존 진단 마스킹을 거친다.
- 다운로드 진단에 후보 URL 타임스탬프와 계산된 URL 나이를 함께 기록한다.
- 수동 재생 로그에 미디어가 아닌 HTTP 4xx/5xx 응답도 안전한 URL, 상태, 요청/응답 허용 헤더와 함께 기록한다. 응답 본문 전체는 저장하지 않는다.
- `download` 진단 헤더 필터와 URL 타임스탬프 파싱에 단위 테스트를 추가한다.

### 다음 검증 게이트

1. 새 수동 재생 로그에서 미디어 요청의 `request_headers`를 확인하고, `range` 및 `x-goog-user-project` 값을 기록한다.
2. 같은 페이지를 자동 처리한 로그에서 후보 URL 나이, 각 다운로드 프로필의 헤더 값, GCS의 오류 코드/메시지를 확인한다.
3. 두 로그의 실제 차이를 확인한 뒤 한 번에 한 조건만 바꿔 재현한다. 원인이 확인되기 전에는 Range 기본값이나 동일 세션 다운로드 동작을 추가 변경하지 않는다.
4. 실환경 성공 여부가 확인될 때까지 P1/P2는 “코드 반영, 원인 검증 미완료”로 취급한다.