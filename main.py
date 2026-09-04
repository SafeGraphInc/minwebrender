import asyncio
import atexit
import logging
import os
import threading

from flask import Flask

from render_service import BrowserService
from routes import init_routes

logging.basicConfig(
    level=os.environ.get("LOG_LEVEL", "INFO"),
    format="%(asctime)s %(levelname)s %(name)s %(message)s",
)

app = Flask(__name__)

browser_service = BrowserService()

def start_async_loop(loop):
    asyncio.set_event_loop(loop)
    loop.run_forever()

# Chromium lives on a dedicated background loop; Flask's sync request threads
# marshal work onto it with run_coroutine_threadsafe. This is why the app must
# run with ONE gunicorn worker (see Dockerfile): a second worker process means
# a second browser and double the memory for no extra throughput. Scale with
# replicas instead.
async_loop = asyncio.new_event_loop()
async_thread = threading.Thread(target=start_async_loop, args=(async_loop,), daemon=True)
async_thread.start()

async def start_browser_service():
    await browser_service.start()

def stop_browser_service():
    asyncio.run_coroutine_threadsafe(browser_service.stop(), async_loop).result()

asyncio.run_coroutine_threadsafe(start_browser_service(), async_loop).result()

atexit.register(stop_browser_service)

init_routes(app, browser_service, async_loop)

if __name__ == '__main__':
    port = int(os.environ.get('PORT', 10000))
    app.run(host='0.0.0.0', port=port)
