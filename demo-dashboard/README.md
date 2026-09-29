# Lightwell SDLC demo dashboard

This source was recovered from the running `demo-dashboard` pod in
`sdlc-6bq2v-1` on 2026-09-30. The four application files initially matched
`quay.io/bluesman/lightwell-demo-dashboard:latest` byte for byte. The backend
now also supports querying Lightwell Maven metadata directly with credentials
supplied by Helm. The `Containerfile` makes it buildable from this repository.

The dashboard shows remediated Maven versions for dependencies in the tenant
help-app `pom.xml`, whether each version is already cached by Nexus, and a
timeline with links to EDA, AAP, TPA, OpenCode, and GitLab. **Remediate**
fetches a real artifact through Nexus. The resulting `component CREATED`
webhook is a one-shot event for that tenant and version. See
[`../architecture.md`](../architecture.md) for the full flow.

## Run

The backend uses only the Python standard library. With an `oc` kubeconfig,
it can discover the tenant and read the needed secrets:

```sh
cp ../.env.secrets.example ../.env.secrets  # fill in real values first
set -a; . ../.env.secrets; set +a
python3 server.py --kubeconfig /path/to/kubeconfig --preflight
python3 server.py --kubeconfig /path/to/kubeconfig
```

Then open `http://127.0.0.1:8099`. Alternatively, supply the `LW_*`
variables below and run without `oc`. Set `LW_DASHBOARD_PASSWORD` to require
HTTP Basic authentication (`demo` is the username). The OpenShift deployment
sets this from the `opencode-server` Secret; `/healthz` remains unauthenticated
for probes. The dashboard must not be exposed publicly without a password.

For local use, source the repository-root `.env.secrets` before starting the
server to supply `LIGHTWELL_NETWORK_USERNAME` and
`LIGHTWELL_NETWORK_PASSWORD`. The backend uses those values to list available
versions directly from Lightwell Maven metadata. It does not call an LLM;
`OPENAI_API_KEY` configures OpenCode during tenant bootstrap. In OpenShift,
the dashboard receives Lightwell credentials through Secret references
rendered by Helm, with no `.env.secrets` file mounted in the pod.

## Configuration

| Variable | Purpose / deployed source |
| --- | --- |
| `LW_GUID`, `LW_DOMAIN` | Tenant GUID and OpenShift apps domain. |
| `LW_NEXUS_URL`, `LW_NEXUS_REPO` | Tenant Nexus route and remediated proxy repository. |
| `LW_LIGHTWELL_URL` | Remediated Maven repository URL; defaults to `https://packages.redhat.com/lightwell/java/remediated/`. |
| `LW_LIGHTWELL_USERNAME`, `LW_LIGHTWELL_PASSWORD` | Direct metadata lookup; Helm copies the Nexus upstream Secret into the dashboard namespace. For local use, `LIGHTWELL_NETWORK_USERNAME/PASSWORD` are also accepted. |
| `LW_AAP_URL`, `LW_TPA_URL`, `LW_SSO_URL` | AAP, TPA, and Keycloak routes. |
| `LW_GITLAB_URL`, `LW_OPENCODE_URL` | GitLab and tenant OpenCode routes. |
| `LW_HELP_APP`, `LW_HELP_APP_BRANCH` | GitLab project path and source branch. |
| `LW_TPA_REALM`, `LW_TPA_CLIENT` | Keycloak realm and TPA UI client. |
| `LW_TENANT_USER`, `LW_TENANT_PASSWORD` | Tenant identity; password from `opencode-server/password`. |
| `LW_AAP_USER`, `LW_AAP_PASSWORD` | AAP identity; password from `aap-admin-password/password`. |
| `LW_GITLAB_TOKEN` | PAT from `gitlab-root-pat/token`. |
| `LW_DASHBOARD_PASSWORD` | HTTP Basic password from `opencode-server/password`. |
| `LW_STATE_DIR` | Directory for `runs.json`; deployment mounts an `emptyDir` at `/var/lib/dashboard`. |
| `LW_STALL_SECONDS` | Time before an unfinished stage is marked stalled; default 900. |
| `PORT`, `BIND` | Listener; container defaults to `8080` and `0.0.0.0`. |

The live deployment, Service, and Route are named `demo-dashboard` in
`sdlc-6bq2v-1`. It exposes port 8080, uses `/healthz` for probes, mounts an
`emptyDir` for run history, and supplies the variables above via environment
values and Secret key references. Run history therefore resets on pod
replacement; all timeline stages are reconstructed from the live systems.
The tenant Helm chart now defines these resources under
`automation/gitops/bootstrap-tenant/templates/sdlc/dashboard.yaml` and enables
them with `sdlc.dashboard.enabled`. LiteLLM settings go to OpenCode. Lightwell
Network credentials go to Nexus and are copied to the dashboard namespace for
metadata lookup; the Remediate button still fetches the JAR through Nexus to
trigger EDA.

## Build

```sh
podman build -f Containerfile -t quay.io/bluesman/lightwell-demo-dashboard:<tag> .
podman push quay.io/bluesman/lightwell-demo-dashboard:<tag>
oc -n sdlc-6bq2v-1 set image deployment/demo-dashboard \
  dashboard=quay.io/bluesman/lightwell-demo-dashboard:<tag>
```

Use a new immutable tag and update the GitOps source of the deployment if it
is managed there. The source code has no third-party dependencies or frontend
build step.
