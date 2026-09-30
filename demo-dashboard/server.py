#!/usr/bin/env python3
"""Lightwell SDLC remediation dashboard.

One screen for the whole demo: which remediated packages exist upstream, which
have already been burned as a trigger on this tenant, a Remediate button that
fires a real one, and a live timeline of every stage with a deep link into the
console that proves it.

    python3 server.py                 # http://localhost:8099
    python3 server.py --preflight     # check every endpoint and credential, exit

Stdlib only — nothing to install. All cluster calls happen server-side: the
browser cannot make them itself (four different auth schemes, no CORS headers).

Configuration comes from the environment when present (how it runs in the pod)
and falls back to `oc` against a kubeconfig (how it runs on a laptop). See
README.md for the full variable list.
"""

from __future__ import annotations

import argparse
import base64
import hmac
import json
import os
import re
import ssl
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

HERE = Path(__file__).resolve().parent
STATIC = HERE / "static"
# Writable in the pod via an emptyDir; falls back beside the script locally.
RUNS_FILE = Path(os.environ.get("LW_STATE_DIR", HERE)) / "runs.json"

# A stage with nothing to show yet is "running", which on screen is
# indistinguishable from one that will never finish — the marker just pulses
# forever. Past this many seconds, say so instead. The runbook budgets 10-15
# minutes for a cold first run, so this is deliberately past the slow case: it
# should only fire on a stage that is genuinely dead.
STALL_AFTER = int(os.environ.get("LW_STALL_SECONDS", "900"))

# Cluster routes terminate on the ingress wildcard cert, which the pod does not
# trust by default; the runbook curls with -k throughout, so match that.
SSLCTX = ssl.create_default_context()
SSLCTX.check_hostname = False
SSLCTX.verify_mode = ssl.CERT_NONE


class SafeRedirectHandler(urllib.request.HTTPRedirectHandler):
    """Never forward credentials from a service to a redirected host.

    Lightwell Maven metadata redirects to a signed object-storage URL. That
    URL rejects a second Authorization mechanism, and must not receive the
    Lightwell Basic credential in the first place.
    """

    def redirect_request(self, req, fp, code, msg, headers, newurl):
        redirected = super().redirect_request(req, fp, code, msg, headers, newurl)
        source = urllib.parse.urlsplit(req.full_url)
        target = urllib.parse.urlsplit(newurl)
        if redirected and (source.scheme, source.netloc) != (target.scheme, target.netloc):
            for key in list(redirected.headers):
                if key.lower() in ("authorization", "proxy-authorization"):
                    del redirected.headers[key]
        return redirected


OPENER = urllib.request.build_opener(
    urllib.request.HTTPSHandler(context=SSLCTX), SafeRedirectHandler()
)


# ---------------------------------------------------------------------------
# version handling
# ---------------------------------------------------------------------------

RHLW = re.compile(r"^(?P<base>.+?)\.rhlw-(?P<build>\d+)$")


def version_key(v: str):
    """(base-number-tuple, rhlw-build).

    A plain upstream version sorts below any remediated build of the same base,
    which is what makes `1.4` < `1.4.0.rhlw-00001` come out right.
    """
    m = RHLW.match(v)
    base, build = (m.group("base"), int(m.group("build"))) if m else (v, -1)
    nums = [int(t) for t in re.findall(r"\d+", base)]
    return (tuple((nums + [0, 0, 0, 0])[:4]), build)


def classify(declared: str, candidate: str) -> dict:
    """Classify a published rhlw build against what the pom declares today.

    This is the check that stops the demo running backwards on screen: several
    published builds are of *older* base versions. They fire the webhook
    perfectly well, and then the agent argues for a downgrade in front of the
    audience.
    """
    d_base, d_build = version_key(declared)
    c_base, c_build = version_key(candidate)
    if (c_base, c_build) <= (d_base, d_build):
        return {
            "direction": "downgrade",
            "safe": False,
            "note": "Older than the version the pom declares. It would fire, but "
                    "the agent would argue for going backwards — do not present it.",
        }
    if c_base == d_base:
        return {
            "direction": "patch",
            "safe": True,
            "note": "Same base version, higher remediation build. Drop-in: the "
                    "verify build compiles by construction.",
        }
    old = ".".join(str(n) for n in d_base if n) or declared
    new = candidate.split(".rhlw-")[0]
    return {
        "direction": "cross-version",
        "safe": True,
        "note": f"Base version moves {old} to {new}. Legitimate, and a stronger "
                "story, but the API surface may have shifted — do a throwaway "
                "build before presenting or the verify step can fail.",
    }


# ---------------------------------------------------------------------------
# http helper
# ---------------------------------------------------------------------------


def http(url, method="GET", headers=None, data=None, timeout=60, raw=False):
    req = urllib.request.Request(url, method=method, data=data)
    for k, v in (headers or {}).items():
        req.add_header(k, v)
    try:
        with OPENER.open(req, timeout=timeout) as r:
            body = r.read()
            if raw:
                return r.status, body
            if not body:
                return r.status, None
            try:
                return r.status, json.loads(body)
            except ValueError:
                return r.status, body.decode("utf-8", "replace")
    except urllib.error.HTTPError as e:
        body = e.read()
        try:
            return e.code, json.loads(body)
        except ValueError:
            return e.code, body.decode("utf-8", "replace")
    except Exception as e:  # noqa: BLE001 — surfaced to the UI as a step error
        return 0, str(e)


def basic(user, password):
    return "Basic " + base64.b64encode(f"{user}:{password}".encode()).decode()


class ConfigError(RuntimeError):
    pass


# ---------------------------------------------------------------------------
# configuration
# ---------------------------------------------------------------------------


class Config:
    """Endpoints and credentials, resolved once at startup.

    In the pod every value arrives as an environment variable from the Helm
    chart, so no ServiceAccount permissions are needed. On a laptop the missing
    ones are filled in with `oc` against the tenant kubeconfig.
    """

    def __init__(self, kubeconfig: str | None = None):
        self.kubeconfig = kubeconfig
        self.lock = threading.Lock()
        self._tpa_token = (None, 0.0)
        self._gitlab_oauth = ""

        self.guid = os.environ.get("LW_GUID") or self._guid_from_cluster()
        self.domain = os.environ.get("LW_DOMAIN") or self._oc(
            "get", "ingresses.config.openshift.io", "cluster",
            "-o", "jsonpath={.spec.domain}",
        )

        g, d = self.guid, self.domain
        self.nexus = os.environ.get("LW_NEXUS_URL") or f"https://nexus-lightwell-nexus-{g}.{d}"
        self.repo = os.environ.get("LW_NEXUS_REPO") or f"redhat-packages-remediated-{g}"
        self.lightwell = os.environ.get(
            "LW_LIGHTWELL_URL", "https://packages.redhat.com/lightwell/java/remediated/"
        ).rstrip("/")
        # The local bootstrap env file uses LIGHTWELL_NETWORK_*; Helm supplies
        # the LW_* aliases from a Secret in the dashboard namespace.
        self.lightwell_user = (os.environ.get("LW_LIGHTWELL_USERNAME")
                               or os.environ.get("LIGHTWELL_NETWORK_USERNAME") or "")
        self.lightwell_password = (os.environ.get("LW_LIGHTWELL_PASSWORD")
                                   or os.environ.get("LIGHTWELL_NETWORK_PASSWORD") or "")
        if bool(self.lightwell_user) != bool(self.lightwell_password):
            raise ConfigError("Lightwell Network username and password must be set together")
        self.aap = os.environ.get("LW_AAP_URL") or f"https://aap-aap.{d}"
        self.tpa = os.environ.get("LW_TPA_URL") or f"https://server-lightwell-tpa.{d}"
        self.sso = os.environ.get("LW_SSO_URL") or f"https://sso.{d}"
        self.gitlab = os.environ.get("LW_GITLAB_URL") or f"https://gitlab-gitlab.{d}"
        self.opencode = os.environ.get("LW_OPENCODE_URL") or f"https://opencode-ui-sdlc-{g}.{d}"
        self.help_app = os.environ.get("LW_HELP_APP") or f"lightwell/lw-demo-help-app-{g}"
        self.realm = os.environ.get("LW_TPA_REALM", "trusted-profile-analyzer")
        self.tpa_client = os.environ.get("LW_TPA_CLIENT", "trustify-ui")
        self.branch = os.environ.get("LW_HELP_APP_BRANCH", "main")

        self.tenant_user = os.environ.get("LW_TENANT_USER") or f"user-{g}"
        self.tenant_password = os.environ.get("LW_TENANT_PASSWORD") or self._secret(
            "opencode-server", f"sdlc-{g}", "password"
        )
        # The timeline needs the platform admin. The tenant org user is NOT
        # enough: AAP returns 200 with count 0 for both /jobs/ and
        # /activations/, because those objects live in an org it has no read
        # role on. Verified on cluster-6bq2v — admin sees 61 jobs and 1
        # activation where user-6bq2v-1 sees none.
        #
        # In the pod the chart supplies the password from the Secret the
        # sync-aap-admin job copies out of the aap namespace. On a laptop, try
        # that Secret directly and only then fall back to the tenant password —
        # the two are equal on most deployments but not by construction, since
        # the admin password is bootstrap-infra's own admin.password.
        self.aap_ns = os.environ.get("LW_AAP_NAMESPACE", "aap")
        self.aap_user = os.environ.get("LW_AAP_USER") or "admin"
        self.aap_password = (
            os.environ.get("LW_AAP_PASSWORD")
            or self._secret("aap-admin-password", self.aap_ns, "password", optional=True)
            or self.tenant_password
        )
        self.gitlab_token = os.environ.get("LW_GITLAB_TOKEN") or self._secret(
            "gitlab-root-pat", f"sdlc-{g}", "token", optional=True
        )
        self.ui_password = os.environ.get("LW_DASHBOARD_PASSWORD") or ""

    # -- oc fallback ------------------------------------------------------

    def _oc(self, *args) -> str:
        if not self.kubeconfig:
            raise ConfigError(
                "missing configuration and no kubeconfig available — set the "
                "LW_* environment variables"
            )
        p = subprocess.run(
            ["oc", "--kubeconfig", self.kubeconfig, *args],
            capture_output=True, text=True, timeout=60,
        )
        if p.returncode != 0:
            tail = (p.stderr or "").strip().splitlines()
            msg = tail[-1] if tail else "oc failed"
            if "must be logged in" in msg or "credentials" in msg:
                raise ConfigError(
                    f"kubeconfig token expired — run `oc login` again "
                    f"(KUBECONFIG={self.kubeconfig})"
                )
            raise ConfigError(msg)
        return p.stdout.strip()

    def _guid_from_cluster(self) -> str:
        names = self._oc("get", "ns", "-o", "name").splitlines()
        guids = [n.split("/sdlc-", 1)[1] for n in names
                 if "/sdlc-" in n and "sandboxes" not in n]
        if not guids:
            raise ConfigError("no sdlc-<guid> namespace found — wrong cluster?")
        return guids[0]

    def _secret(self, name, namespace, key, optional=False) -> str:
        try:
            raw = self._oc("get", "secret", name, "-n", namespace,
                           "-o", "jsonpath={.data.%s}" % key)
        except ConfigError:
            if optional:
                return ""
            raise
        if not raw:
            if optional:
                return ""
            raise ConfigError(f"secret {namespace}/{name} has no key {key}")
        return base64.b64decode(raw).decode()

    # -- auth -------------------------------------------------------------

    def aap_headers(self):
        return {"Authorization": basic(self.aap_user, self.aap_password)}

    def opencode_headers(self):
        return {"Authorization": basic("opencode", self.tenant_password)}

    def gitlab_headers(self):
        """Prefer the root PAT; fall back to the tenant user's password grant.

        The fallback matters on a laptop, where the PAT lives in a Secret that
        an expired kubeconfig cannot read but the tenant password is known.
        """
        if self.gitlab_token:
            return {"PRIVATE-TOKEN": self.gitlab_token}
        with self.lock:
            if self._gitlab_oauth:
                return {"Authorization": f"Bearer {self._gitlab_oauth}"}
        body = urllib.parse.urlencode({
            "grant_type": "password",
            "username": self.tenant_user,
            "password": self.tenant_password,
        }).encode()
        status, data = http(
            f"{self.gitlab}/oauth/token", method="POST", data=body,
            headers={"Content-Type": "application/x-www-form-urlencoded"},
        )
        if status != 200 or not isinstance(data, dict):
            raise ConfigError(
                f"no GitLab credentials — the root PAT is unavailable and the "
                f"password grant for {self.tenant_user} failed ({status}). "
                "Set LW_GITLAB_TOKEN."
            )
        with self.lock:
            self._gitlab_oauth = data["access_token"]
        return {"Authorization": f"Bearer {self._gitlab_oauth}"}

    def tpa_headers(self):
        """Keycloak direct-access-grant token for the trustify-ui client.

        Depends on the realm's read.* client scopes; without them every call
        403s and the fix is fix-tpa-scopes.sh.
        """
        with self.lock:
            token, expiry = self._tpa_token
            if token and time.time() < expiry - 30:
                return {"Authorization": f"Bearer {token}"}
        body = urllib.parse.urlencode({
            "client_id": self.tpa_client,
            "grant_type": "password",
            "username": self.tenant_user,
            "password": self.tenant_password,
        }).encode()
        status, data = http(
            f"{self.sso}/realms/{self.realm}/protocol/openid-connect/token",
            method="POST", data=body,
            headers={"Content-Type": "application/x-www-form-urlencoded"},
        )
        if status != 200 or not isinstance(data, dict):
            raise ConfigError(f"Keycloak token request failed ({status})")
        with self.lock:
            self._tpa_token = (data["access_token"],
                               time.time() + int(data.get("expires_in", 300)))
        return {"Authorization": f"Bearer {data['access_token']}"}

    @property
    def project_api(self):
        return f"{self.gitlab}/api/v4/projects/{urllib.parse.quote(self.help_app, safe='')}"

    def consoles(self):
        return {
            "Nexus": f"{self.nexus}/#browse/browse:{self.repo}",
            "AAP": f"{self.aap}/execution/jobs",
            "TPA": f"{self.tpa}/sboms",
            "GitLab": f"{self.gitlab}/{self.help_app}",
            "OpenCode": self.opencode,
        }


# ---------------------------------------------------------------------------
# package inventory
# ---------------------------------------------------------------------------


def declared_dependencies(cfg: Config) -> list[dict]:
    """Dependencies declared in the help-app pom — the real blast radius."""
    status, body = http(
        f"{cfg.project_api}/repository/files/pom.xml/raw?ref={cfg.branch}",
        headers=cfg.gitlab_headers(), raw=True,
    )
    if status != 200:
        raise ConfigError(f"could not read the help-app pom.xml ({status})")
    ns = {"m": "http://maven.apache.org/POM/4.0.0"}
    root = ET.fromstring(body)
    props = {p.tag.split("}")[-1]: (p.text or "").strip()
             for p in root.findall("m:properties/*", ns)}

    def resolve(text):
        text = (text or "").strip()
        m = re.fullmatch(r"\$\{(.+?)\}", text)
        return props.get(m.group(1), text) if m else text

    out, seen = [], set()
    for dep in root.findall(".//m:dependencies/m:dependency", ns):
        # The smoke test reads the running app's /api/status. Test and provided
        # dependencies cannot appear there, even when Lightwell has a fix.
        scope = resolve(dep.findtext("m:scope", "", ns))
        if scope not in ("", "compile", "runtime"):
            continue
        gid = resolve(dep.findtext("m:groupId", "", ns))
        aid = resolve(dep.findtext("m:artifactId", "", ns))
        ver = resolve(dep.findtext("m:version", "", ns))
        if gid and aid and ver and not ver.startswith("${") and (gid, aid) not in seen:
            seen.add((gid, aid))
            out.append({"group": gid, "artifact": aid, "declared": ver})
    return out


def remediated_versions(cfg: Config, group: str, artifact: str) -> list[str]:
    """rhlw builds published upstream.

    When Lightwell credentials are available, read its Maven metadata directly.
    Otherwise read through the tenant Nexus proxy. In either case,
    maven-metadata.xml is an asset, not a component, so listing versions does
    not consume a trigger. The actual trigger still fetches the jar via Nexus.
    """
    path = f"{group.replace('.', '/')}/{artifact}/maven-metadata.xml"
    direct = bool(cfg.lightwell_user and cfg.lightwell_password)
    url = (f"{cfg.lightwell}/{path}" if direct else
           f"{cfg.nexus}/repository/{cfg.repo}/{path}")
    headers = {"Authorization": basic(cfg.lightwell_user, cfg.lightwell_password)} if direct else None
    status, body = http(url, headers=headers, raw=True, timeout=45)
    if direct and status not in (200, 404):
        raise ConfigError(f"Lightwell Maven metadata request failed ({status}) for {group}:{artifact}")
    if status != 200:
        return []
    try:
        root = ET.fromstring(body)
    except ET.ParseError:
        return []
    return sorted({v.text.strip() for v in root.findall(".//version")
                   if v.text and "rhlw" in v.text}, key=version_key)


def cached_components(cfg: Config) -> set:
    """Every component in the tenant repo — i.e. every trigger already burned."""
    seen, token = set(), None
    for _ in range(30):  # paginate defensively
        url = f"{cfg.nexus}/service/rest/v1/search?repository={cfg.repo}"
        if token:
            url += f"&continuationToken={token}"
        status, data = http(url, timeout=90)
        if status != 200 or not isinstance(data, dict):
            break
        for item in data.get("items", []):
            seen.add((item.get("group", ""), item.get("name", ""), item.get("version", "")))
        token = data.get("continuationToken")
        if not token:
            break
    return seen


def build_inventory(cfg: Config) -> list[dict]:
    deps = declared_dependencies(cfg)
    cached = cached_components(cfg)
    rows = []
    for dep in deps:
        versions = remediated_versions(cfg, dep["group"], dep["artifact"])
        if not versions:
            continue
        consumed_versions = [v for v in versions
                             if (dep["group"], dep["artifact"], v) in cached]
        # Highest build already triggered on this tenant. A candidate at or below
        # it is an upgrade on paper — the pom may still declare the original —
        # but presenting it after a higher build has already run reads as a
        # regression to the audience.
        high_water = max((version_key(v) for v in consumed_versions), default=None)
        entries = []
        for v in versions:
            consumed = v in consumed_versions
            verdict = classify(dep["declared"], v)
            # "downgrade" is the more informative label when both apply.
            if (not consumed and verdict["safe"]
                    and high_water and version_key(v) <= high_water):
                verdict = {
                    "direction": "superseded",
                    "safe": False,
                    "note": "A higher remediation build of this package has already "
                            "been triggered here. It would fire, but it walks the "
                            "story backwards.",
                }
            entries.append({"version": v, "consumed": consumed,
                            "usable": not consumed and verdict["safe"], **verdict})
        rows.append({**dep, "versions": entries,
                     "any_usable": any(e["usable"] for e in entries),
                     "remediated": sum(1 for e in entries if e["consumed"])})
    rows.sort(key=lambda r: (not r["any_usable"], r["artifact"]))
    return rows


# ---------------------------------------------------------------------------
# runs
# ---------------------------------------------------------------------------


def load_runs():
    if RUNS_FILE.exists():
        try:
            return json.loads(RUNS_FILE.read_text())
        except ValueError:
            pass
    return []


def save_runs(runs):
    try:
        RUNS_FILE.parent.mkdir(parents=True, exist_ok=True)
        RUNS_FILE.write_text(json.dumps(runs[:50], indent=2))
    except OSError:
        pass  # history is a convenience, not a requirement


def trigger(cfg: Config, group, artifact, version) -> dict:
    """The one human action in the demo: fetch the artifact through Nexus.

    Nexus caches it and emits `component CREATED` — but only on first cache, so
    refuse a version already present rather than let the demo look dead.
    """
    if (group, artifact, version) in cached_components(cfg):
        raise ConfigError(
            f"{artifact} {version} is already cached on this tenant. Nexus only "
            "emits CREATED on first cache, so nothing would fire. Choose a "
            "version that has not been consumed."
        )
    stem = f"{group.replace('.', '/')}/{artifact}/{version}/{artifact}-{version}"
    fetched = {}
    for ext in ("pom", "jar"):
        status, _ = http(f"{cfg.nexus}/repository/{cfg.repo}/{stem}.{ext}",
                         raw=True, timeout=180)
        fetched[ext] = status
    if fetched.get("jar") != 200:
        raise ConfigError(
            f"Nexus could not serve the jar (pom {fetched.get('pom')}, "
            f"jar {fetched.get('jar')}) — nothing was triggered."
        )
    return fetched


def step(key, title, what, console=None, detail=None):
    return {"key": key, "title": title, "what": what, "console": console,
            "links": [], "state": "pending", "detail": detail}


def collect_run(cfg: Config, run: dict) -> list[dict]:
    """Rebuild the timeline from live cluster state.

    Everything is filtered by the run's start time so several runs in one
    session stay separate.
    """
    started_ms = int(run["started"] * 1000)
    since = run["started_iso"]
    gav = (run["group"], run["artifact"], run["version"])
    steps = []

    # 1 — Nexus
    s = step("nexus", "Nexus caches the remediated artifact",
             "The only human action in the flow. Nexus pulls the build from the "
             "Lightwell network, caches it in the tenant repository and emits a "
             "genuine component CREATED webhook. Everything after this is automatic.",
             console=f"{cfg.nexus}/#browse/browse:{cfg.repo}")
    s["state"] = "done" if gav in cached_components(cfg) else "running"
    steps.append(s)

    # 2 — EDA
    s = step("eda", "EDA matches the rule",
             "The rulebook activation discards every component without an rhlw- "
             "version. That filter matters: the verify build later resolves "
             "hundreds of transitive dependencies through the same proxy and each "
             "one emits its own CREATED event. On a match it launches the job chain.")
    status, data = http(f"{cfg.aap}/api/eda/v1/activations/", headers=cfg.aap_headers())
    if status == 200 and isinstance(data, dict) and data.get("results"):
        a = data["results"][0]
        s["console"] = f"{cfg.aap}/decisions/rulebook-activations/{a['id']}/details"
        s["detail"] = f"activation {a['name']} is {a.get('status')}"
        if a.get("status") == "running":
            s["state"] = "done"
        else:
            s["state"] = "error"
            s["detail"] += " — restart it, it does not recover on its own"
    else:
        s["state"], s["detail"] = "error", f"AAP returned {status}"
    steps.append(s)

    # 3 — AAP job chain
    s = step("aap", "AAP runs the job chain",
             "SDLC Query TPA asks TPA which repositories are affected — the blast "
             "radius. The two Trigger jobs are launchers, not the work: they open "
             "an OpenCode session, fire the prompt asynchronously and return. None "
             "of the agent's reasoning appears in their output.",
             console=f"{cfg.aap}/execution/jobs")
    status, data = http(f"{cfg.aap}/api/controller/v2/jobs/?order_by=-created&page_size=30",
                        headers=cfg.aap_headers())
    if status == 200 and isinstance(data, dict):
        for job in reversed(data.get("results", [])):
            if (job.get("created") or "") < since:
                continue
            s["links"].append({
                "label": job.get("name"),
                "badge": job.get("status"),
                "url": f"{cfg.aap}/execution/jobs/playbook/{job['id']}/output",
                "state": {"successful": "ok", "failed": "bad"}.get(job.get("status"), "warn"),
            })
        s["state"] = "done" if s["links"] else "running"
    else:
        s["state"], s["detail"] = "error", f"AAP returned {status}"
    steps.append(s)

    # 4 — TPA
    s = step("tpa", "TPA supplies the blast radius",
             "TPA holds an SBOM per deployed application. The query matches on SBOM "
             "label and dependency coordinate — not version — and returns the "
             "repositories that genuinely ship this artifact.",
             console=f"{cfg.tpa}/sboms")
    try:
        # List the SBOMs rather than searching for one named after the artifact.
        # `?q=<artifact>` matches the SBOM *name* — which is help-im-vulnerable,
        # never the dependency — so it returned 200 with zero items and left this
        # stage pulsing "running" on every run, including successful ones.
        status, data = http(f"{cfg.tpa}/api/v2/sbom?limit=20", headers=cfg.tpa_headers())
        if status == 200 and isinstance(data, dict):
            for item in data.get("items", []):
                count = item.get("number_of_packages")
                name = item.get("name") or item.get("id")
                s["links"].append({
                    "label": f"{name} — {count} packages" if count else name,
                    "badge": (item.get("labels") or {}).get("name") or "sbom",
                    "url": f"{cfg.tpa}/sboms/{item.get('id')}",
                    "state": "ok"})
            s["state"] = "done" if s["links"] else "running"
            # The canonical coordinate TPA holds for this artifact. Worth showing:
            # it spells out the groupId, which is exactly what the EDA rulebook
            # drops on the way to the agent.
            st, pd = http(
                f"{cfg.tpa}/api/v2/purl?q={urllib.parse.quote(run['artifact'])}&limit=5",
                headers=cfg.tpa_headers())
            if st == 200 and isinstance(pd, dict):
                purls = sorted({(i.get("base") or {}).get("purl")
                                for i in pd.get("items", []) if i} - {None})
                if purls:
                    s["detail"] = "TPA tracks " + ", ".join(purls)
        elif status == 403:
            s["state"] = "error"
            s["detail"] = ("403 — the realm is missing its read.* client scopes. "
                           "Run fix-tpa-scopes.sh.")
        else:
            s["state"], s["detail"] = "error", f"TPA returned {status}"
    except ConfigError as e:
        s["state"], s["detail"] = "error", str(e)
    steps.append(s)

    # 5 — OpenCode
    s = step("opencode", "The agents do the work",
             "Two disjoint agents: impact-analyzer bumps the pom and opens the MR, "
             "mr-verifier builds, deploys and posts the verdict. Neither can call "
             "the other's skills. These links are the only place the reasoning, the "
             "skill invocation and the tool calls are visible.",
             console=cfg.opencode,
             detail="The OpenCode landing page is always empty — use these deep links.")
    status, data = http(f"{cfg.opencode}/session", headers=cfg.opencode_headers())
    if status == 200:
        sessions = data.get("data", data) if isinstance(data, dict) else (data or [])
        for sess in sorted(sessions, key=lambda x: x.get("time", {}).get("created", 0)):
            if sess.get("time", {}).get("created", 0) < started_ms:
                continue
            s["links"].append({"label": sess.get("title") or sess.get("id"),
                               "badge": "session",
                               "url": f"{cfg.opencode}/global/session/{sess['id']}",
                               "state": "ok"})
        s["state"] = "done" if s["links"] else "running"
    else:
        s["state"], s["detail"] = "error", f"OpenCode returned {status}"
    steps.append(s)

    # 6 — GitLab
    s = step("gitlab", "The merge request",
             "The impact-analyzer's output: a branch update-artifact-<version>, the "
             "pom bumped to the remediated build, and an impact analysis written "
             "into the description together with agent-handoff JSON for the verifier.",
             console=f"{cfg.gitlab}/{cfg.help_app}/-/merge_requests")
    try:
        status, data = http(
            f"{cfg.project_api}/merge_requests?order_by=created_at&sort=desc&per_page=20",
            headers=cfg.gitlab_headers())
        if status == 200 and isinstance(data, list):
            for mr in data:
                if (mr.get("created_at") or "") < since:
                    continue
                s["links"].append({"label": f"!{mr.get('iid')} {mr.get('title')}",
                                   "badge": mr.get("state"),
                                   "url": mr.get("web_url"),
                                   "state": "ok" if mr.get("state") == "opened" else "warn"})
            s["state"] = "done" if s["links"] else "running"
        else:
            s["state"], s["detail"] = "error", f"GitLab returned {status}"
    except ConfigError as e:
        s["state"], s["detail"] = "error", str(e)
    steps.append(s)

    # Only the earliest unfinished stage is worth flagging: everything after it
    # is legitimately waiting on it, and marking the whole tail stalled says
    # nothing useful about where the run actually stopped.
    elapsed = time.time() - run["started"]
    if elapsed > STALL_AFTER:
        for s in steps:
            if s["state"] == "running":
                s["state"] = "stalled"
                s["detail"] = (f"{s['detail']} — " if s["detail"] else "") + (
                    f"still nothing after {int(elapsed // 60)} min. The run has most "
                    "likely failed upstream rather than being slow; check the stage "
                    "above and the OpenCode transcript.")
                break

    return steps


# ---------------------------------------------------------------------------
# http server
# ---------------------------------------------------------------------------


class Handler(SimpleHTTPRequestHandler):
    cfg: Config = None
    runs: list = []
    lock = threading.Lock()

    def __init__(self, *a, **kw):
        super().__init__(*a, directory=str(STATIC), **kw)

    def log_message(self, fmt, *args):
        sys.stderr.write("  %s\n" % (fmt % args))

    def _json(self, payload, status=200):
        body = json.dumps(payload).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Cache-Control", "no-store")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _authorised(self) -> bool:
        """Basic auth, on by default in the pod — the Remediate button has real
        side effects and the Route is reachable by anyone who knows the host."""
        want = Handler.cfg.ui_password
        if not want:
            return True
        got = self.headers.get("Authorization", "")
        expect = basic("demo", want)
        if hmac.compare_digest(got, expect):
            return True
        self.send_response(401)
        self.send_header("WWW-Authenticate", 'Basic realm="Lightwell SDLC"')
        self.send_header("Content-Length", "0")
        self.end_headers()
        return False

    # -- routes -----------------------------------------------------------

    def do_GET(self):
        if self.path == "/healthz":  # probes must not need credentials
            return self._json({"ok": True})
        if not self._authorised():
            return
        route = urllib.parse.urlparse(self.path).path
        cfg = Handler.cfg
        try:
            if route == "/api/config":
                return self._json({"guid": cfg.guid, "domain": cfg.domain,
                                   "helpApp": cfg.help_app, "repo": cfg.repo,
                                   "consoles": cfg.consoles()})
            if route == "/api/packages":
                return self._json({"packages": build_inventory(cfg)})
            if route == "/api/runs":
                with Handler.lock:
                    Handler.runs = load_runs()
                    runs = list(Handler.runs)
                return self._json({"runs": runs})
            if route.startswith("/api/run/"):
                rid = route.rsplit("/", 1)[-1]
                with Handler.lock:
                    Handler.runs = load_runs()
                    run = next((r for r in Handler.runs if r["id"] == rid), None)
                if not run:
                    return self._json({"error": "no such run"}, 404)
                return self._json({"run": run, "steps": collect_run(cfg, run)})
        except ConfigError as e:
            return self._json({"error": str(e)}, 502)
        except Exception as e:  # noqa: BLE001
            return self._json({"error": f"{type(e).__name__}: {e}"}, 500)
        return super().do_GET()

    def do_POST(self):
        if not self._authorised():
            return
        if urllib.parse.urlparse(self.path).path != "/api/remediate":
            return self._json({"error": "not found"}, 404)
        length = int(self.headers.get("Content-Length") or 0)
        try:
            body = json.loads(self.rfile.read(length) or "{}")
            fetched = trigger(Handler.cfg, body["group"], body["artifact"], body["version"])
        except ConfigError as e:
            return self._json({"error": str(e)}, 409)
        except Exception as e:  # noqa: BLE001
            return self._json({"error": f"{type(e).__name__}: {e}"}, 500)
        now = time.time()
        run = {"id": f"run-{int(now)}", "group": body["group"],
               "artifact": body["artifact"], "version": body["version"],
               "started": now,
               "started_iso": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(now)),
               "fetched": fetched}
        with Handler.lock:
            Handler.runs = load_runs()
            Handler.runs.insert(0, run)
            save_runs(Handler.runs)
        return self._json({"run": run})


# ---------------------------------------------------------------------------


def preflight(cfg: Config) -> int:
    print(f"tenant {cfg.guid} on {cfg.domain}\n")
    worst = 0
    checks = [
        ("Nexus", f"{cfg.nexus}/service/rest/v1/status", None),
        ("AAP", f"{cfg.aap}/api/controller/v2/ping/", None),
        # /api/v4/version needs a token, so a 401 here still proves reachability;
        # the credential check below is what actually validates access.
        ("GitLab", f"{cfg.gitlab}/api/v4/version", None),
        ("TPA", f"{cfg.tpa}/", None),
        ("OpenCode", f"{cfg.opencode}/session", cfg.opencode_headers()),
        ("Keycloak", f"{cfg.sso}/realms/{cfg.realm}", None),
    ]
    for name, url, headers in checks:
        status, _ = http(url, headers=headers, timeout=25)
        flag = "ok" if 200 <= status < 400 else ("auth" if status in (401, 403) else "DOWN")
        worst |= flag == "DOWN"
        print(f"  {name:<10} {status:<5} {flag}")
    print()
    for label, fn in (("AAP creds", lambda: http(
                          f"{cfg.aap}/api/controller/v2/me/", headers=cfg.aap_headers())),
                      ("GitLab token", lambda: http(
                          f"{cfg.project_api}", headers=cfg.gitlab_headers())),
                      ("TPA token", lambda: (200, cfg.tpa_headers()))):
        try:
            status, _ = fn()
            ok = 200 <= status < 300
            worst |= not ok
            print(f"  {label:<14} {'ok' if ok else 'FAILED (%s)' % status}")
        except ConfigError as e:
            worst = 1
            print(f"  {label:<14} FAILED — {e}")

    # The demo-killer: the activation does not self-recover after a cluster
    # restart — it exhausts its restart budget and parks in "failed", and
    # nothing shows that until a trigger is fired and the pipeline stays silent.
    print()
    status, data = http(f"{cfg.aap}/api/eda/v1/activations/", headers=cfg.aap_headers())
    results = data.get("results") if status == 200 and isinstance(data, dict) else None
    if not results:
        worst = 1
        print(f"  {'EDA activation':<14} FAILED — AAP returned {status} with no activation")
    for a in results or []:
        ok = a.get("status") == "running"
        worst |= not ok
        print(f"  {'EDA activation':<14} {a['name']} is {a.get('status')}"
              f"{'' if ok else ' — restart it, it does not recover on its own:'}")
        if not ok:
            print(f"    curl -sk -u {cfg.aap_user}:$AAP_PASS -X POST "
                  f"{cfg.aap}/api/eda/v1/activations/{a['id']}/restart/")
    return int(worst)


def main():
    ap = argparse.ArgumentParser(description="Lightwell SDLC remediation dashboard")
    ap.add_argument("--port", type=int, default=int(os.environ.get("PORT", 8099)))
    ap.add_argument("--bind", default=os.environ.get("BIND", "127.0.0.1"))
    ap.add_argument("--kubeconfig", default=os.environ.get("KUBECONFIG", "./lightwell-demo"))
    ap.add_argument("--preflight", action="store_true",
                    help="check every endpoint and credential, then exit")
    args = ap.parse_args()

    kubeconfig = args.kubeconfig if Path(args.kubeconfig).exists() else None
    try:
        cfg = Config(kubeconfig)
    except ConfigError as e:
        sys.exit(f"error: {e}")

    if args.preflight:
        sys.exit(preflight(cfg))

    Handler.cfg = cfg
    Handler.runs = load_runs()
    server = ThreadingHTTPServer((args.bind, args.port), Handler)
    print(f"tenant {cfg.guid} on {cfg.domain}")
    print(f"listening on http://{args.bind}:{args.port}"
          f"{'  (basic auth enabled)' if cfg.ui_password else ''}\n")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\nbye")


if __name__ == "__main__":
    main()
