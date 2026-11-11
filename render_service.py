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
# Concurrent pages per pod. Lowered from 6: the memory limit is sized on an
# ~80Mi-per-page average, but the real distribution has a long tail, and the
# fewer heavy pages can overlap the smaller the worst case. Caller-side
# demand does not need six -- TOOL_MAX_CONCURRENCY is 24 per poller process
# and renders are a minority of tool calls, so typical load is a few
# concurrent renders per replica, not six.
max_pages_env = int(os.getenv("MAX_PAGES", 4))

# Per-renderer V8 heap ceiling, in MB. Four pages at this ceiling plus the
# browser base still fits the limit with room for the Python side.
js_heap_mb = int(os.getenv("JS_HEAP_MB", 256))

# Per-page navigation budget. Kept below the caller's tool timeout so a slow
# page surfaces as a 504 here rather than as a hung request upstream.
page_timeout_ms = int(os.getenv("PAGE_TIMEOUT_MS", 45000))

# Ceiling on the HTML this process will parse, and on the text it returns.
#
# extract_text_content holds four representations of one page at once -- the
# raw HTML, the BeautifulSoup tree, the mutated str(soup) copy, and the
# markdown -- so peak memory is several times the page size. With MAX_PAGES
# concurrent renders sharing one memory limit, a single large page is enough
# to OOM the pod, and on 2026-11-10 one did.
#
# The output cap is the same bound seen from the caller's side: the rendered
# text goes into an LLM context, and an uncapped page has pushed single-pass
# agents past 500k tokens. Truncating is strictly better than the two things
# that happen without it -- the pod dies, or the caller pays for a page it
# cannot use.
#
# Both are generous: a real business website is a few hundred KB of HTML and
# a few tens of KB of text. Anything past these is a document dump, an
# infinite scroll, or a misdirected URL.
max_html_bytes = int(os.getenv("MAX_HTML_BYTES", 5_000_000))
max_text_chars = int(os.getenv("MAX_TEXT_CHARS", 200_000))

# Appended so the caller can tell a truncated page from a short one. The
# distinction matters: the prompt reads "any website content confirms open",
# and silence about truncation would let a 5MB page look like a thin one.
TRUNCATION_NOTE = "\n\n[truncated by minwebrender: page exceeded the size limit]"

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
            # --js-flags=--max-old-space-size bounds the V8 heap PER RENDERER
            # process. Without it a page can allocate until the pod's cgroup
            # limit is hit, which is what has been happening: measured steady
            # state is ~800Mi of 2048Mi across six pages, matching the ~80Mi
            # per page this was sized for, and then one JS-heavy site takes
            # several hundred megabytes on its own and takes the pod with it.
            #
            # The output cap added for the OOM does not help here -- it
            # measures HTML and text size, and a page can hold half a gigabyte
            # of browser memory while its HTML is 200KB. Zero truncations
            # fired in the 24h that included an OOMKill, which is how we know.
            #
            # A page that hits the ceiling fails that one render instead of
            # killing five other in-flight ones plus the browser.
            self.browser = await self.playwright.chromium.launch(
                args=["--no-sandbox", "--disable-dev-shm-usage",
                      f"--js-flags=--max-old-space-size={js_heap_mb}"],
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
    # Before the soup, not after. Parsing is where the memory goes, so a cap
    # applied to the output would be measured after the damage is done.
    truncated = False
    if len(html_content) > max_html_bytes:
        logger.warning(
            "html %d bytes exceeds MAX_HTML_BYTES %d for %s; parsing a prefix",
            len(html_content), max_html_bytes, original_url,
        )
        html_content = html_content[:max_html_bytes]
        truncated = True

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

    if len(formatted_markdown) > max_text_chars:
        logger.warning(
            "text %d chars exceeds MAX_TEXT_CHARS %d for %s; truncating",
            len(formatted_markdown), max_text_chars, original_url,
        )
        formatted_markdown = formatted_markdown[:max_text_chars]
        truncated = True

    if truncated:
        formatted_markdown += TRUNCATION_NOTE

    return formatted_markdown