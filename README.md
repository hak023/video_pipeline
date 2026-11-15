# CDP browser mode support

This change adds a `--cdp` option which connects to an already-running Chrome instance with remote debugging enabled.

Example:

  chrome.exe --remote-debugging-port=9222 --user-data-dir="C:/chrome-cdp"
  python main.py --url "https://example.com/post" --cdp http://127.0.0.1:9222

Behavior:

- The existing Chrome profile and AdBlock Plus/other extensions remain active.
- The script uses that browser as the discovery layer instead of spawning a fresh Playwright browser.
- The downloader still uses the existing yt-dlp/ffmpeg path; CDP is treated as a browser control layer, not a replacement for the media download engine.
- When the remote Chrome is not reachable, the project emits a clear CDP connection error and falls back to the normal browser flow only if the code path allows it.

This keeps the architecture aligned with the earlier design recommendation: browser-level media discovery and request inspection, then separate download execution.
