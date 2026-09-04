# Playwright's own image: Chromium plus the ~100 apt libs it needs are already
# installed and version-matched to the pip package, so the build does not have
# to run `playwright install --with-deps` (slow, and a frequent source of
# browser/driver version skew).
# Tag MUST match playwright in requirements.txt (1.46.0).
FROM mcr.microsoft.com/playwright/python:v1.46.0-jammy

WORKDIR /app

# PIP_PROGRESS_BAR=off is load-bearing, not cosmetic: pip's rich progress bar
# spawns a refresh thread, and the dind runner's thread/pid limit makes that
# fail with "RuntimeError: can't start new thread", killing the build mid-
# download. The other two match the convention in galaxy's Dockerfiles.
ENV PIP_PROGRESS_BAR=off \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    PIP_NO_CACHE_DIR=1

# No `pip install --upgrade pip`: the base image ships 24.2, every requirement
# here is pinned, and self-upgrading pip was the step the runner died on.
COPY requirements.txt ./
RUN pip install -r requirements.txt

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
