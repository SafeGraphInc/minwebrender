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
#
# Bookworm was tried (PR #16) together with a CI bump to Docker 28, which
# would have removed the clone3 constraint. The image built and ran, but the
# dind service stopped starting on the runner and every job failed before
# reaching `docker build` -- first on the derived service hostname, then on an
# explicit alias, so the service itself is not coming up on Docker 28. That
# needs the runner's service log to diagnose, so the base stays on Debian 11
# and only the dead mirror is fixed below.
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

# Debian 11 left LTS on 2026-08-31 and its packages moved off deb.debian.org,
# so `playwright install --with-deps` began failing with 404 on every security
# package it needs. archive.debian.org still serves bullseye (verified:
# dists/bullseye/Release and dists/bullseye-security/Release both 200).
#
# Check-Valid-Until is disabled because an archived suite's Release file is
# past its expiry by definition; apt would otherwise refuse the index outright.
#
# This is a holding position, not a fix: an archived suite gets no further
# security updates. The real fix is a newer base, which needs the CI runner on
# Docker 20.10.10+ -- see the note above.
RUN printf '%s\n' \
      'deb http://archive.debian.org/debian bullseye main' \
      'deb http://archive.debian.org/debian-security bullseye-security main' \
      > /etc/apt/sources.list && \
    printf 'Acquire::Check-Valid-Until "false";\n' \
      > /etc/apt/apt.conf.d/99archived-suite

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
# requests, and must leave headroom ABOVE MAX_PAGES for the probes.
#
# Every render blocks its thread on future.result() while the work happens on
# the asyncio loop, so concurrent requests consume threads one for one. At
# --threads 12 with MAX_PAGES 6, twelve in-flight requests -- six rendering,
# six queued on the page semaphore -- leave nothing to answer /healthz, and
# the probe times out in the socket backlog rather than returning 503. The
# kubelet then kills a pod that is working fine: observed 2026-11-10, healthz
# 200 at 21:17:22 and SIGTERM at 21:17:23, three times across the fleet.
#
# >= MAX_PAGES was the old rule and it is not enough: it guarantees renders
# are not thread-starved, not that anything is left for a probe. 32 is
# generous on purpose -- a thread parked on I/O costs almost nothing, while
# the page semaphore still caps real concurrency at MAX_PAGES. --timeout must exceed PAGE_TIMEOUT_MS (45s) or
# gunicorn kills the worker -- and thus the browser -- mid-render.
CMD ["gunicorn", "--bind", "0.0.0.0:10000", \
     "-w", "1", "--threads", "32", "--worker-class", "gthread", \
     "--timeout", "90", "--graceful-timeout", "30", \
     "--access-logfile", "-", "--error-logfile", "-", \
     "main:app"]
