# minwebrender

Headless-Chromium page renderer. Given a URL it loads the page in Chromium,
strips scripts/styles/images, and returns the readable text as markdown.

Its consumer is the `web_render` agent tool in
[safegraph-agentic-research](https://github.com/SafeGraphInc/safegraph-agentic-research),
which calls it for every page an agent needs to read. `web_render` falls back
to Firecrawl, then to Serper's scrape API, when this service is unreachable.

## Endpoints

| Route | Purpose |
|---|---|
| `GET /<url>` | Render as an HTML page. The contract `web_render` speaks today. |
| `GET /text/<url>` | Same render as `text/plain`, skipping the markdown→HTML round trip. |
| `GET /healthz` | Liveness. 503 when Chromium is not connected. |
| `GET /readyz` | Readiness. Same signal. |
| `GET /` | Human-facing form. |

## Configuration

| Env var | Default | Purpose |
|---|---|---|
| `DOMAIN` | `0.0.0.0:10000` | Host stamped onto rewritten `<a href>`s. Set to the Service address. |
| `MAX_PAGES` | `6` | Concurrent Chromium pages. Drives the memory limit. |
| `PAGE_TIMEOUT_MS` | `45000` | Per-page navigation budget. Keep under the caller's tool timeout. |
| `REQUEST_TIMEOUT_MS` | `75000` | Ceiling on the whole request, including the wait for a free page slot. Keep under gunicorn's `--timeout`. |
| `PORT` | `10000` | Dev server only; gunicorn binds 10000 in the image. |
| `LOG_LEVEL` | `INFO` | |

## Local development

```bash
pip install -r requirements.txt && playwright install chromium
python main.py                        # http://localhost:10000
curl localhost:10000/text/example.com
```

Or via the image, which is what CI and EKS run:

```bash
docker build -t minwebrender . && docker run --rm -p 10000:10000 minwebrender
```

## Deployment

Runs on SafeGraph's EKS cluster, built and shipped the same way as
safegraph-agentic-research:

```
GitLab CI (.gitlab-ci.yml)          Harness
build -> test -> push to ECR   -->  helm release `minwebrender`
                                    (deploy/helm/)
```

- **Image**: `$ECR_REGISTRY/minwebrender`, tag `latest` from the default branch.
  Built on Playwright's own base image so Chromium and its ~100 apt
  dependencies are already installed and version-matched to the pip package.
- **Values**: [deploy/helm/](deploy/helm/) — `base.yaml` plus
  `values-production.yaml` (3 replicas) and `values-staging.yaml` (1 replica).
- **Internal to the VPC**: the shared chart has no ClusterIP-only mode, so a
  `subdomain` is set and an internal ALB is created. `loadbalancerIsPublic`
  stays false, so nothing is reachable from the internet — the old public
  `minwebrender.net` was an unauthenticated open renderer where anyone could
  make SafeGraph infrastructure fetch any URL.
- **In-cluster callers use `http://minwebrender`** and never touch the load
  balancer. Port 80, not 10000: the chart puts the Service on 80 and forwards
  to the container's 10000.
- **No AWS permissions**: the renderer touches no S3, RDS, or Secrets Manager,
  so it needs no IRSA role and no secrets.
- **CI test**: boots the built image and renders a page end-to-end. That is the
  check that catches Chromium/playwright version skew and a browser that will
  not launch under the container's seccomp profile.
- **One gunicorn worker on purpose**: one worker means one Chromium. A second
  worker process would mean a second browser and double the memory for no extra
  throughput. Scale with replicas.

Previously deployed on Render.com behind `minwebrender.net`.

### Differences from the Render deployment

- `_ensure_browser()` relaunches a disconnected browser under a lock. The old
  code launched Chromium once at import and never re-checked it, so a dead
  browser wedged the process until someone redeployed — the cause of the
  2026-09-03 outage, when the single instance stopped answering after a handful
  of concurrent renders and did not recover.
- `/healthz` and `/readyz`, so the kubelet can restart what a relaunch cannot
  fix, and 3 replicas so there is no single instance to wedge.
- `--no-sandbox --disable-dev-shm-usage`, needed under a pod's seccomp profile
  and 64Mi `/dev/shm`.
- Not exposed to the internet.
