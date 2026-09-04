"""HTTP surface.

``/<path:url>`` keeps the exact contract the agentic-research ``web_render``
tool already speaks (HTML page, content in the body), so that tool works
against this deployment with only a base-URL swap. ``/text/<path:url>`` is the
same render without the markdown->HTML round trip the caller currently pays.
"""
import asyncio
import concurrent.futures as futures
import logging
import os

import markdown
from flask import jsonify, redirect, render_template, request
from playwright.async_api import TimeoutError as PlaywrightTimeoutError

logger = logging.getLogger(__name__)

# Ceiling on the whole request, not just navigation. PAGE_TIMEOUT_MS bounds
# page.goto; waiting for a free page-semaphore slot, or for a browser launch
# that never returns, is unbounded on its own. Default leaves headroom over
# PAGE_TIMEOUT_MS (45s) while staying under gunicorn's --timeout 90.
request_timeout_ms = int(os.getenv("REQUEST_TIMEOUT_MS", 75000))

# Every way a render can time out. These are three distinct classes on Python
# 3.10 -- asyncio.TimeoutError is not concurrent.futures.TimeoutError, and
# Playwright raises its own -- so catching one does not catch the others.
_TIMEOUT_ERRORS = (
    futures.TimeoutError,
    asyncio.TimeoutError,
    PlaywrightTimeoutError,
)


def init_routes(app, browser_service, async_loop):

    @app.route('/', methods=['GET', 'POST'])
    def home():
        if request.method == 'POST':
            user_url = request.form['url']
            return redirect(f"/{user_url}")

        return render_template('home.html')

    @app.route('/healthz')
    def healthz():
        """Liveness. A wedged or dead Chromium fails here so the kubelet
        restarts the pod -- the recovery the Render deployment never had.

        Deliberately does not render anything: it must stay answerable when
        every worker thread is stuck, which is the case it exists to catch.
        """
        if browser_service.healthy():
            return jsonify({"status": "ok"}), 200
        return jsonify({"status": "browser-unavailable"}), 503

    @app.route('/readyz')
    def readyz():
        """Readiness. Same signal, but pulls the pod out of the Service
        endpoints instead of restarting it."""
        if browser_service.healthy():
            return jsonify({"status": "ready"}), 200
        return jsonify({"status": "not-ready"}), 503

    @app.route('/text/<path:url>')
    def fetch_minimal_page_text(url):
        try:
            return _render(url), 200, {'Content-Type': 'text/plain; charset=utf-8'}
        except _TIMEOUT_ERRORS:
            logger.warning("render timed out: %s", url)
            return jsonify({"error": f"timed out rendering {url}"}), 504
        except Exception as e:
            logger.exception("render failed: %s", url)
            return jsonify({"error": str(e)}), 500

    @app.route('/<path:url>')
    def fetch_minimal_page(url):
        try:
            html_content = markdown.markdown(_render(url))
            return render_template('rendered_page.html', content=html_content)
        except _TIMEOUT_ERRORS:
            logger.warning("render timed out: %s", url)
            return jsonify({"error": f"timed out rendering {url}"}), 504
        except Exception as e:
            logger.exception("render failed: %s", url)
            return jsonify({"error": str(e)}), 500

    def _render(url):
        if not url.startswith('http'):
            full_url = f"http://{url}"
        else:
            full_url = url
        future = asyncio.run_coroutine_threadsafe(
            browser_service.render_page_and_extract_text(full_url), async_loop
        )
        try:
            return future.result(timeout=request_timeout_ms / 1000)
        except futures.TimeoutError:
            # Without a bound here the request thread blocks forever whenever
            # the coroutine cannot finish -- a wedged event loop, a launch that
            # never returns, a semaphore slot never released -- and gunicorn's
            # threads leak away one request at a time until the pod stops
            # answering. That is the exact failure this service exists to
            # survive, so it must not be reachable from the inside.
            # cancel() is a no-op once the coroutine is running, but it stops a
            # still-queued one from rendering a page nobody is waiting for.
            future.cancel()
            raise
