# main.py 수동 재생 진단 옵션 변경 코드

`apply_manual_playback_patch.py`를 실행하면 아래 변경이 `main.py`에 자동 반영됩니다. 함수 본문 전체는 패치 스크립트에 포함되어 있습니다.

## 1. import 추가

기존 `diagnostics` import 목록에 `attach_page_diagnostics`를 추가하고, 아래 import를 추가합니다.

```python
from media_tools import get_browser_launch_kwargs, get_context_kwargs
from playwright.sync_api import sync_playwright
```

## 2. CLI 옵션 추가 (`_build_parser`)

```python
parser.add_argument(
    "--manual-playback",
    action="store_true",
    help="--url 페이지를 실제 브라우저로 열어 사용자가 직접 재생하고 네트워크/플레이어 로그 수집 (다운로드 안 함)",
)
```

## 3. `_dispatch` 진입부에 분기 추가

```python
if args.manual_playback:
    if not args.url:
        raise ValueError("--manual-playback 옵션은 --url 과 함께 사용해야 합니다.")
    _manual_playback(args.url, proxy=args.proxy)
    return None
```

위 분기는 기존 `if args.url:`보다 먼저 실행되어야 합니다.

## 4. `_manual_playback` 함수

`_dispatch` 함수 정의 바로 앞에 추가됩니다. 전체 함수 코드는 `apply_manual_playback_patch.py`의 `function` 문자열에 포함되어 있습니다.

## 5. 진단 metadata 변경

기존 `init_diagnostics(..., meta={...})`의 `mode` 항목을 다음과 같이 변경합니다.

```python
"mode": "manual_playback" if args.manual_playback else "url" if args.url else "crawl" if args.crawl else "discover_only" if args.discover_only else "targets",
```

## 실행

```powershell
python main.py --url "게시글_URL" --manual-playback
```
