# Debian 11 (glibc 2.31) on purpose, NOT a newer base.
#
# glibc 2.34+ calls clone3 before falling back to clone, and the CI runner's
# Docker 19.03 seccomp profile answers unknown syscalls with EPERM rather than
# ENOSYS -- so glibc gives up and NO thread can start. Playwright's own image
# is Ubuntu 22.04 (glibc 2.35), where gunicorn died at import on
# `async_thread.start()` with "RuntimeError: can't start new thread", and pip
# died the same way earlier in the build. clone3 was only allowlisted in Docker
# 20.10.10. See moby/moby#42680.
#
# Playwright's focal image would also be glibc 2.31, but it installs Ubuntu
# 20.04's python3 (3.8) and the pinned Werkzeug 3.1.5 and Markdown 3.8.1 both
# require >=3.9. Debian 11 gives glibc 2.31 AND Python 3.10.
FROM python:3.10-bullseye

# Outside $HOME so the unprivileged runtime user can read the browsers.
ENV PLAYWRIGHT_BROWSERS_PATH=/ms-playwright

# Match the convention in galaxy's Dockerfiles. PIP_PROGRESS_BAR also keeps
# pip from spawning a progress-refresh thread -- unnecessary now that glibc is
# 2.31, but harmless and it keeps build logs readable.
ENV PIP_PROGRESS_BAR=off \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    PIP_NO_CACHE_DIR=1

WORKDIR /app

# No `pip install --upgrade pip`: the base image's pip is fine and every
# requirement here is pinned.
COPY requirements.txt ./
RUN pip install -r requirements.txt

# Chromium plus its apt dependencies, at the revision the installed playwright
# package expects. This is a STRONGER version guarantee than picking a prebuilt
# image tag by hand -- the browser is chosen by the pip package rather than by
# a tag someone has to remember to bump alongside requirements.txt.
RUN playwright install --with-deps chromium && \
    chmod -R a+rX /ms-playwright

COPY . ./

# Drop privileges. Chromium still runs with --no-sandbox (see render_service.py)
# because its in-process sandbox needs syscalls the pod's seccomp profile
# denies.
RUN useradd --create-home --shell /usr/sbin/nologin appuser
USER appuser

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
