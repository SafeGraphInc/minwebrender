# Debian 12 (bookworm). The previous pin to Debian 11 existed because the CI
# runner used Docker 19.03, whose seccomp profile answers the clone3 syscall
# with EPERM rather than ENOSYS -- so glibc 2.34+ gave up and NO thread could
# start. gunicorn died at import on `async_thread.start()` with "RuntimeError:
# can't start new thread", and pip died the same way earlier in the build.
# clone3 was allowlisted in Docker 20.10.10 (moby/moby#42680), and CI now runs
# Docker 28, so the constraint is gone.
#
# Removing it matters: Debian 11 left LTS on 2026-08-31 and its packages moved
# off deb.debian.org, so `playwright install --with-deps` began failing with
# 404s on every security package. Bookworm is supported into 2028.
#
# If threads ever fail to start again, check the CI Docker version before
# changing this line -- that is the cause, not the base image.
FROM python:3.10-bookworm

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
