#!/usr/bin/env python3
"""Take screenshots of the OpenBuchhaltung UI using Playwright.

Usage example:
    python tools/screenshot_ui.py --url http://127.0.0.1:5000/ --output artifacts/ui-home.png
"""

from __future__ import annotations

import argparse
from pathlib import Path


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Capture a full-page screenshot of the running OpenBuchhaltung UI.",
    )
    parser.add_argument(
        "--url",
        default="http://127.0.0.1:5000/",
        help="URL to capture (default: %(default)s)",
    )
    parser.add_argument(
        "--output",
        default="artifacts/ui-screenshot.png",
        help="Path for the screenshot file (default: %(default)s)",
    )
    parser.add_argument(
        "--width",
        type=int,
        default=1440,
        help="Viewport width in pixels (default: %(default)s)",
    )
    parser.add_argument(
        "--height",
        type=int,
        default=1024,
        help="Viewport height in pixels (default: %(default)s)",
    )
    parser.add_argument(
        "--wait-ms",
        type=int,
        default=1200,
        help="Extra wait time after load, in milliseconds (default: %(default)s)",
    )
    return parser.parse_args()


def main() -> int:
    args = parse_args()

    try:
        from playwright.sync_api import sync_playwright
    except ImportError:
        print("Playwright is not installed.")
        print("Install with: pip install -r requirements-dev.txt")
        print("Then install browser binaries with: python -m playwright install chromium")
        return 2

    output_path = Path(args.output)
    output_path.parent.mkdir(parents=True, exist_ok=True)

    with sync_playwright() as playwright:
        browser = playwright.chromium.launch(headless=True)
        page = browser.new_page(viewport={"width": args.width, "height": args.height})
        page.goto(args.url, wait_until="networkidle")
        if args.wait_ms > 0:
            page.wait_for_timeout(args.wait_ms)
        page.screenshot(path=str(output_path), full_page=True)
        browser.close()

    print(f"Saved screenshot to {output_path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
