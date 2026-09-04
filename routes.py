"""HTTP surface.

``/<path:url>`` keeps the exact contract the agentic-research ``web_render``
tool already speaks (HTML page, content in the body), so that tool works
against this deployment with only a base-URL swap. ``/text/<path:url>`` is the
same render without the markdown->HTML round trip the caller currently pays.
"""
import asyncio
import logging

import markdown
from flask import jsonify, redirect, render_template, request

logger = logging.getLogger(__name__)


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
        restarts the pod -- the recovery the Render deployment never had."""
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
        except asyncio.TimeoutError:
            return jsonify({"error": f"timed out rendering {url}"}), 504
        except Exception as e:
            logger.exception("render failed: %s", url)
            return jsonify({"error": str(e)}), 500

    @app.route('/<path:url>')
    def fetch_minimal_page(url):
        try:
            html_content = markdown.markdown(_render(url))
            return render_template('rendered_page.html', content=html_content)
        except asyncio.TimeoutError:
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
        return future.result()
