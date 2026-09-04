# minwebrender

Headless-Chromium page renderer. Given a URL it loads the page in Chromium,
strips scripts/styles/images, and returns the readable text as markdown.

Its consumer is the `web_render` agent tool in
[safegraph-agentic-research](https://github.com/SafeGraphInc/safegraph-agentic-research),
which calls it for every page an agent needs to read. `web_render` falls back
to Firecrawl, then to Serper's scrape API, when this service is unreachable.

## Local development

```bash
pip install -r requirements.txt && playwright install chromium
python main.py                        # http://localhost:10000
curl localhost:10000/example.com
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
- **Internal-only**: ClusterIP, no ingress. Callers use
  `http://minwebrender.<namespace>.svc.cluster.local:10000`. The old public
  `minwebrender.net` was an unauthenticated open renderer — anyone could make
  SafeGraph infrastructure fetch any URL.
- **No AWS permissions**: the renderer touches no S3, RDS, or Secrets Manager,
  so it needs no IRSA role and no secrets.
- **CI test**: boots the built image and renders a page end-to-end. That is the
  check that catches Chromium/playwright version skew and a browser that will
  not launch under the container's seccomp profile.
- **One gunicorn worker on purpose**: one worker means one Chromium. A second
  worker process would mean a second browser and double the memory for no extra
  throughput. Scale with replicas.

Previously deployed on Render.com behind `minwebrender.net`.
