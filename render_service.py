import asyncio
import logging
import os
import re
from urllib.parse import urljoin, urlparse

import html2text
from bs4 import BeautifulSoup

from playwright.async_api import async_playwright

logger = logging.getLogger(__name__)

# Prefix stamped onto rewritten <a href>s so links stay inside the renderer.
# On Render this was the public hostname; in EKS it is the Service DNS name.
host = os.getenv("DOMAIN", "0.0.0.0:10000")

# Concurrent Chromium pages per pod. Each page is ~50-80MB of RSS, so this is
# the knob that sets the memory limit in deploy/helm/base.yaml. Scale
# throughput with replicas, not with this.
max_pages_env = int(os.getenv("MAX_PAGES", 6))

# Per-page navigation budget. Kept below the caller's tool timeout so a slow
# page surfaces as a 504 here rather than as a hung request upstream.
page_timeout_ms = int(os.getenv("PAGE_TIMEOUT_MS", 45000))

class BrowserService:
    def __init__(self, max_pages=max_pages_env):
        self.browser = None
        self.playwright = None
        self.max_pages = max_pages
        self.page_semaphore = asyncio.Semaphore(self.max_pages)
        self._launch_lock = asyncio.Lock()

    async def start(self):
        """Launch Chromium. Idempotent -- safe to call on every request path."""
        await self._ensure_browser()

    async def stop(self):
        """Stop the browser and playwright."""
        if self.browser:
            await self.browser.close()
            self.browser = None
        if self.playwright:
            await self.playwright.stop()
            self.playwright = None

    async def _ensure_browser(self):
        """Launch the browser, or re-launch it if it died.

        The original code launched Chromium exactly once at import and never
        checked it again, so a crashed or wedged browser wedged the whole
        process until someone redeployed -- which is how minwebrender.net went
        dark on 2026-09-03 after a handful of concurrent renders.

        Serialized on a lock so a burst of requests arriving after a crash
        triggers one relaunch rather than one per request.
        """
        if self.browser is not None and self.browser.is_connected():
            return
        async with self._launch_lock:
            # Re-check: another waiter may have relaunched while we queued.
            if self.browser is not None and self.browser.is_connected():
                return
            logger.warning("Chromium not connected; launching")
            if self.playwright is None:
                self.playwright = await async_playwright().start()
            # --no-sandbox: the container already runs unprivileged, and
            # Chromium's own sandbox needs syscalls the default pod seccomp
            # profile denies. --disable-dev-shm-usage: /dev/shm is 64MB in a
            # pod by default and Chromium crashes when it fills it.
            self.browser = await self.playwright.chromium.launch(
                args=["--no-sandbox", "--disable-dev-shm-usage"],
            )
            logger.info("Chromium launched (max_pages=%s)", self.max_pages)

    def healthy(self) -> bool:
        """True when a live browser is attached. Drives the liveness probe.

        Reads self.browser once into a local: this runs on a Flask request
        thread while stop() and _ensure_browser() mutate the attribute on the
        async loop, so checking and then dereferencing self.browser could see
        None on the second read and raise AttributeError.
        """
        browser = self.browser
        return browser is not None and browser.is_connected()

    async def render_page_and_extract_text(self, url):
        """Render a page, waiting for a free slot in the page semaphore."""
        async with self.page_semaphore:
            await self._ensure_browser()
            return await self.process_page(url)

    async def process_page(self, url):
        """Process a single page and return the content."""
        page = await self.browser.new_page()
        try:
            await page.goto(url, timeout=page_timeout_ms)
            content = await page.content()
            return extract_text_content(content, url, host)
        finally:
            await page.close()


def extract_text_content(html_content, original_url, host_url):
    soup = BeautifulSoup(html_content, 'html.parser')

    for element in soup(['script', 'style', 'img']):
        element.decompose()

    for a in soup.find_all('a', href=True):
        original_href = a['href']
        parsed_href = urlparse(original_href)

        if parsed_href.scheme and parsed_href.netloc:
            new_href = f"{host_url}/{original_href.lstrip('/')}"
        else:
            full_href = urljoin(original_url, original_href)
            new_href = f"{host_url}/{full_href.lstrip('/')}"

        if not new_href.startswith(('http://', 'https://')):
            new_href = f"http://{new_href}"

        a['href'] = new_href

    markdown_content = html2text.HTML2Text()
    markdown_content.ignore_links = False
    markdown_content.body_width = 0
    markdown_text = markdown_content.handle(str(soup))

    markdown_text = re.sub(r'[ \t]+', ' ', markdown_text).strip()
    paragraphs = markdown_text.split('\n\n')
    formatted_markdown = '\n\n'.join(paragraph.strip() for paragraph in paragraphs)

    return formatted_markdown