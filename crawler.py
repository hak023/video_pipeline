from __future__ import annotations

import re
from datetime import date, datetime
from typing import Any
from urllib.parse import urljoin, urlparse

from playwright.sync_api import sync_playwright

from media_tools import get_browser_launch_kwargs, get_context_kwargs
from diagnostics import attach_page_diagnostics, get_diagnostics, log_event, log_exception

MONTH_NAMES = {
    "jan": 1, "january": 1,
    "feb": 2, "february": 2,
    "mar": 3, "march": 3,
    "apr": 4, "april": 4,
    "may": 5,
    "jun": 6, "june": 6,
    "jul": 7, "july": 7,
    "aug": 8, "august": 8,
    "sep": 9, "september": 9,
    "oct": 10, "october": 10,
    "nov": 11, "november": 11,
    "dec": 12, "december": 12,
}


def parse_post_date(text: str | None) -> date | None:
    """Extract and parse a date from raw text or datetime attribute."""
    if not text:
        return None
    text = text.strip()

    # 1. Try ISO or dot/slash with 4-digit year first: YYYY-MM-DD, YYYY/MM/DD, YYYY.MM.DD
    m_ymd = re.search(r"\b(\d{4})[-/.년\s]+(\d{1,2})[-/.월\s]+(\d{1,2})일?\b", text)
    if m_ymd:
        y, mo, d = m_ymd.groups()
        try:
            return date(int(y), int(mo), int(d))
        except ValueError:
            pass

    # 2. Try Month DD, YYYY (e.g., "March 24, 2026" or "Mar 24 2026")
    m_eng1 = re.search(r"([A-Za-z]+)\.?\s+(\d{1,2}),?\s+(\d{4})", text)
    if m_eng1:
        mon_str, day_str, yr_str = m_eng1.groups()
        mon = MONTH_NAMES.get(mon_str.lower())
        if mon:
            try:
                return date(int(yr_str), mon, int(day_str))
            except ValueError:
                pass

    # 3. Try DD Month YYYY (e.g., "24 March 2026")
    m_eng2 = re.search(r"(\d{1,2})\s+([A-Za-z]+)\.?\s+(\d{4})", text)
    if m_eng2:
        day_str, mon_str, yr_str = m_eng2.groups()
        mon = MONTH_NAMES.get(mon_str.lower())
        if mon:
            try:
                return date(int(yr_str), mon, int(day_str))
            except ValueError:
                pass

    # 4. Try DD/MM/YYYY or DD-MM-YYYY or DD.MM.YYYY
    m_dmy = re.search(r"\b(\d{1,2})[-/.](\d{1,2})[-/.](\d{4})\b", text)
    if m_dmy:
        p1, p2, yr = m_dmy.groups()
        n1, n2, y = int(p1), int(p2), int(yr)
        try:
            if n1 > 12:  # Definitely DD/MM/YYYY
                return date(y, n2, n1)
            elif n2 > 12:  # MM/DD/YYYY
                return date(y, n1, n2)
            else:
                # Default to DD/MM/YYYY
                return date(y, n2, n1)
        except ValueError:
            pass

    return None


def is_date_in_range(
    post_date: date | None,
    since: date | None = None,
    until: date | None = None,
    include_undated: bool = False,
) -> bool:
    """Check if post_date satisfies [since, until] range."""
    if post_date is None:
        return include_undated

    if since and post_date < since:
        return False
    if until and post_date > until:
        return False
    return True


# JavaScript snippet executed inside Playwright page to extract posts
JS_EXTRACT_POSTS = """
() => {
    const results = [];
    const seenUrls = new Set();

    // 1. First search for typical article / video containers
    const containers = Array.from(document.querySelectorAll(
        "article, .video-block, .video-item, .item-video, .post, .thumb-block, .entry-box"
    ));

    const processElement = (el) => {
        // Find anchor
        const anchor = el.querySelector("a[href]") || (el.tagName === 'A' ? el : null);
        if (!anchor) return;
        const href = anchor.href;
        if (!href || href.startsWith("javascript:") || href.includes("#") && href.split("#")[0] === window.location.href.split("#")[0]) {
            return;
        }

        // Title
        let title = anchor.getAttribute("title") || "";
        if (!title) {
            const h = el.querySelector("h1, h2, h3, h4, .entry-title, .title, .post-title");
            if (h) title = h.innerText.trim();
        }
        if (!title) {
            const img = el.querySelector("img[alt]");
            if (img) title = img.getAttribute("alt") || "";
        }
        if (!title) {
            title = anchor.innerText.trim();
        }

        // Date
        let dateText = "";
        const timeEl = el.querySelector("time");
        if (timeEl) {
            dateText = timeEl.getAttribute("datetime") || timeEl.innerText.trim();
        }
        if (!dateText) {
            const dateEl = el.querySelector(
                ".mg-blog-date, .date, .post-date, .entry-date, .posted-on, .publish-date, [class*='date'], [class*='time']"
            );
            if (dateEl) dateText = dateEl.innerText.trim();
        }

        if (!seenUrls.has(href)) {
            seenUrls.add(href);
            results.push({ url: href, title: title, date_text: dateText });
        }
    };

    if (containers.length > 0) {
        containers.forEach(processElement);
    } else {
        // Fallback: collect all links inside main content areas
        const main = document.querySelector("main, #content, .content, #main, .site-main") || document.body;
        const links = main.querySelectorAll("a[href]");
        links.forEach(a => {
            const href = a.href;
            const text = a.innerText.trim();
            if (href && text && text.length > 5 && !seenUrls.has(href)) {
                // Ignore navigation/category links
                if (!href.includes("/category/") && !href.includes("/tag/") && !href.includes("/page/")) {
                    seenUrls.add(href);
                    results.push({ url: href, title: text, date_text: "" });
                }
            }
        });
    }

    return results;
}
"""


def _host(url: str) -> str:
    try:
        return urlparse(url).hostname or ""
    except Exception:
        return ""


_SECOND_LEVEL = {"co", "com", "or", "ne", "go", "ac", "org", "net", "gov", "edu"}


def _site_label(host: str) -> str:
    """s28.testonline.co -> testonline (미러/서브도메인/TLD 변경 흡수). a.site.co.kr -> site."""
    parts = [x for x in (host or "").lower().split(".") if x]
    if len(parts) >= 3 and parts[-2] in _SECOND_LEVEL and len(parts[-1]) == 2:
        return parts[-3]
    return parts[-2] if len(parts) >= 2 else (host or "").lower()


def build_page_url(base_url: str, page_num: int) -> str:
    """Build paginated URL.

    Example:
      base_url='https://s28.testonline.co', page=1 -> 'https://s28.testonline.co/'
      base_url='https://s28.testonline.co', page=2 -> 'https://s28.testonline.co/page/2/'
      base_url='https://site.com/videos/page/2', page=3 -> 'https://site.com/videos/page/3/'
    """
    clean_base = base_url.rstrip("/")
    m = re.search(r"(/page/)\d+", clean_base)
    if m:
        prefix = clean_base[:m.start(1)]
        if page_num == 1:
            return f"{prefix}/"
        return f"{prefix}/page/{page_num}/"

    if page_num == 1:
        return f"{clean_base}/"
    return f"{clean_base}/page/{page_num}/"


def crawl_page(
    page_url: str,
    proxy: str | None = None,
    headless: bool = True,
    wait_ms: int = 5000,
) -> list[dict[str, Any]]:
    """Load a single paginated catalog page and return list of post items."""
    results: list[dict[str, Any]] = []

    with sync_playwright() as p:
        launch_kwargs = get_browser_launch_kwargs(headless=headless, proxy=proxy)
        browser = p.chromium.launch(**launch_kwargs)
        ctx = browser.new_context(**get_context_kwargs(
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"))
        # Anti-detection stealth script
        ctx.add_init_script("""
            Object.defineProperty(navigator, 'webdriver', { get: () => undefined });
            window.chrome = { runtime: {} };
        """)

        # Auto-close popup tabs (ads), keeping intentional pages open
        ctx.on("page", lambda new_p: new_p.close() if new_p.opener() is not None else None)
        page = ctx.new_page()
        attach_page_diagnostics(page, "crawl")
        get_diagnostics().note_page(page_url)
        success = False
        status = None
        for attempt in range(3):
            try:
                resp = page.goto(page_url, wait_until="domcontentloaded", timeout=25000)
                status = resp.status if resp else None
                success = True
                break
            except Exception as e:
                if attempt == 2:
                    print(f"[Crawl Error] {page_url}: {e}")
                    log_event("crawl", "goto_failed", f"목록 페이지 접속 실패: {str(e).splitlines()[0]}",
                              level="ERROR", page_url=page_url, attempts=3)
                else:
                    log_event("crawl", "goto_retry", f"목록 페이지 접속 재시도 {attempt + 1}/3", level="WARNING",
                              page_url=page_url, error=str(e).splitlines()[0])
                    page.wait_for_timeout(2000 * (attempt + 1) ** 2)  # 2s, 8s 백오프

        if not success:
            browser.close()
            return []

        # 오류 페이지(451/403/5xx)의 링크를 게시글로 오인하지 않도록 즉시 중단
        if status is not None and status >= 400:
            hint = ""
            if status == 451:
                hint = " - 법적 사유로 차단됨: 도메인이 막힌 것일 수 있으니 새 주소로 --crawl URL 을 바꾸거나 --proxy 를 사용하세요"
            elif status in (403, 429):
                hint = " - 접근 거부/요청 제한: 잠시 후 재시도하거나 --proxy 를 사용하세요"
            print(f"[Crawl Error] {page_url}: HTTP {status}{hint}")
            log_event("crawl", "http_error", f"목록 페이지 HTTP {status}", level="ERROR", page_url=page_url, status=status)
            browser.close()
            return []
            
        final_host = _host(page.url)
        if final_host and _site_label(final_host) != _site_label(_host(page_url)):
            print(f"[Crawl Error] {page_url}: 다른 사이트로 리다이렉트됨 ({final_host}) - 차단/안내 페이지일 수 있습니다")
            log_event("crawl", "redirected_other_site", f"다른 사이트로 리다이렉트: {final_host}", level="ERROR", page_url=page_url)
            browser.close()
            return []

        try:
            if wait_ms > 0:
                page.wait_for_timeout(wait_ms)
            raw_posts = page.evaluate(JS_EXTRACT_POSTS)
            base_label = _site_label(_host(page_url))
            for item in raw_posts:
                if _site_label(_host(item["url"])) != base_label:
                    continue   # 외부 사이트 링크(광고/안내/제휴)는 게시글이 아님
                parsed_dt = parse_post_date(item.get("date_text"))
                iso_str = parsed_dt.isoformat() if parsed_dt else None
                results.append({
                    "url": item["url"],
                    "title": item.get("title") or item["url"],
                    "date": iso_str,
                    "date_obj": parsed_dt,
                })
            log_event("crawl", "posts_found", f"게시글 {len(results)}개 발견", page_url=page_url, posts=len(results),
                      raw_links=len(raw_posts))
        except Exception as e:
            print(f"[Crawl Evaluation Error] {page_url}: {e}")
            log_exception("crawl", e, page_url=page_url)
        finally:
            browser.close()

    return results