# Playwright's own image: Chromium plus the ~100 apt libs it needs are already
# installed and version-matched to the pip package, so the build does not have
# to run `playwright install --with-deps` (slow, and a frequent source of
# browser/driver version skew).
# Tag MUST match playwright in requirements.txt (1.46.0).
FROM mcr.microsoft.com/playwright/python:v1.46.0-jammy

WORKDIR /app

COPY requirements.txt ./
RUN pip install --no-cache-dir --upgrade pip && \
    pip install --no-cache-dir -r requirements.txt

COPY . ./

# Drop privileges. pwuser ships with the Playwright image and owns the browser
# install; Chromium runs with --no-sandbox (see render_service.py) because the
# in-process sandbox needs syscalls the pod's seccomp profile denies.
USER pwuser

EXPOSE 10000

# -w 1: one worker == one Chromium (see main.py). --threads serves concurrent
# requests; keep it >= MAX_PAGES so a full page semaphore is the bottleneck
# rather than thread starvation. --timeout must exceed PAGE_TIMEOUT_MS (45s) or
# gunicorn kills the worker -- and thus the browser -- mid-render.
CMD ["gunicorn", "--bind", "0.0.0.0:10000", \
     "-w", "1", "--threads", "12", "--worker-class", "gthread", \
     "--timeout", "90", "--graceful-timeout", "30", \
     "--access-logfile", "-", "--error-logfile", "-", \
     "main:app"]
