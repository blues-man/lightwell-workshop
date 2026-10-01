#!/usr/bin/env python3
"""Reset one (or every) Lightwell demo package for another dashboard run.

Uses the current OpenShift context and credentials already held in tenant
Secrets. No credential is accepted on the command line or written to disk.
"""

from __future__ import annotations

import argparse
import base64
import json
import re
import ssl
import subprocess
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass


class CleanupError(Exception):
    pass


class Cluster:
    def __init__(self, context: str | None):
        self.prefix = ["oc"] + (["--context", context] if context else [])

    def run(self, *args: str, input_data: bytes | None = None, missing_ok: bool = False) -> bytes:
        try:
            result = subprocess.run(
                [*self.prefix, *args], input=input_data, capture_output=True, timeout=90
            )
        except subprocess.TimeoutExpired as exc:
            raise CleanupError(f"oc {' '.join(args[:3])} timed out") from exc
        if result.returncode:
            message = result.stderr.decode("utf-8", "replace").strip()
            if missing_ok and ("NotFound" in message or "not found" in message):
                return b""
            raise CleanupError(f"oc {' '.join(args[:3])} failed: {message.splitlines()[-1] if message else result.returncode}")
        return result.stdout

    def get_json(self, *args: str, missing_ok: bool = False) -> dict | None:
        raw = self.run("get", *args, "-o", "json", missing_ok=missing_ok)
        return json.loads(raw) if raw else None

    def secret(self, namespace: str, name: str, key: str) -> str:
        resource = self.get_json("secret", name, "-n", namespace)
        try:
            return base64.b64decode(resource["data"][key], validate=True).decode()
        except (KeyError, ValueError) as exc:
            raise CleanupError(f"Secret {namespace}/{name} has no valid {key} key") from exc


class API:
    def __init__(self, base: str, headers: dict[str, str], insecure: bool):
        self.base = base.rstrip("/")
        self.headers = headers
        self.context = ssl._create_unverified_context() if insecure else ssl.create_default_context()

    def request(self, method: str, path: str, payload: dict | None = None, missing_ok: bool = False):
        headers = dict(self.headers)
        data = None
        if payload is not None:
            headers["Content-Type"] = "application/json"
            data = json.dumps(payload).encode()
        req = urllib.request.Request(self.base + path, data=data, headers=headers, method=method)
        try:
            with urllib.request.urlopen(req, context=self.context, timeout=40) as response:
                body = response.read()
                return response.status, json.loads(body) if body else None
        except urllib.error.HTTPError as exc:
            if missing_ok and exc.code == 404:
                return 404, None
            raise CleanupError(f"{method} {self.base}{path} returned HTTP {exc.code}") from exc
        except (OSError, ValueError) as exc:
            raise CleanupError(f"{method} {self.base}{path} failed: {exc}") from exc


@dataclass(frozen=True)
class Package:
    group: str
    artifact: str
    versions: tuple[str, ...]

    @property
    def coordinate(self) -> str:
        return f"{self.group}:{self.artifact}"


def discover_guid(cluster: Cluster, explicit: str | None) -> str:
    if explicit:
        if not re.fullmatch(r"[a-z0-9-]+", explicit):
            raise CleanupError("--guid must contain only lowercase letters, digits, and hyphens")
        return explicit
    namespaces = cluster.get_json("namespaces")["items"]
    guids = sorted(
        x["metadata"]["name"].removeprefix("lightwell-tenant-")
        for x in namespaces
        if x["metadata"]["name"].startswith("lightwell-tenant-")
    )
    if len(guids) != 1:
        raise CleanupError(f"Found {len(guids)} tenant namespaces; specify --guid")
    return guids[0]


def dashboard_inventory(cluster: Cluster, sdlc_ns: str) -> list[Package]:
    code = (
        'import json,sys;sys.path.insert(0,"/opt/app");'
        'from server import Config,build_inventory;'
        'print(json.dumps(build_inventory(Config())))'
    )
    raw = cluster.run("exec", "-n", sdlc_ns, "deploy/demo-dashboard", "--", "python3", "-c", code)
    rows = json.loads(raw)
    return [
        Package(row["group"], row["artifact"], tuple(v["version"] for v in row["versions"]))
        for row in rows
    ]


def choose_packages(inventory: list[Package], name: str | None) -> list[Package]:
    if not name:
        return sorted(inventory, key=lambda p: p.coordinate)
    matches = [
        p for p in inventory
        if name.casefold() in (p.artifact.casefold(), p.coordinate.casefold())
    ]
    if len(matches) != 1:
        available = ", ".join(sorted(p.coordinate for p in inventory))
        raise CleanupError(f"Package {name!r} matched {len(matches)} entries. Available: {available}")
    return matches


def dashboard_runs(cluster: Cluster, sdlc_ns: str) -> list[dict]:
    code = (
        'import pathlib; p=pathlib.Path("/var/lib/dashboard/runs.json");'
        'print(p.read_text() if p.exists() else "[]")'
    )
    raw = cluster.run("exec", "-n", sdlc_ns, "deploy/demo-dashboard", "--", "python3", "-c", code)
    return json.loads(raw)


def gitlab_mrs(api: API, project: str) -> list[dict]:
    result = []
    encoded = urllib.parse.quote(project, safe="")
    for page in range(1, 101):
        _, rows = api.request("GET", f"/api/v4/projects/{encoded}/merge_requests?state=all&per_page=100&page={page}")
        if not isinstance(rows, list):
            raise CleanupError("GitLab returned an invalid merge request list")
        result.extend(rows)
        if len(rows) < 100:
            return result
    raise CleanupError("GitLab merge request pagination exceeded 100 pages")


def package_mr(mr: dict, selected: set[str]) -> bool:
    match = re.fullmatch(r"chore\(deps\): update ([^:]+):([^ ]+) to (.+)", mr.get("title", ""))
    return bool(
        match
        and f"{match[1]}:{match[2]}" in selected
        and ".rhlw-" in match[3]
        and mr.get("source_branch") == f"update-artifact-{match[3]}"
    )


def nexus_components(api: API, repository: str, package: Package) -> list[dict]:
    components = []
    token = None
    for _ in range(100):
        query = {"repository": repository, "group": package.group, "name": package.artifact}
        if token:
            query["continuationToken"] = token
        _, data = api.request("GET", "/service/rest/v1/search?" + urllib.parse.urlencode(query))
        if not isinstance(data, dict):
            raise CleanupError("Nexus returned an invalid search result")
        components.extend(
            item for item in data.get("items", [])
            if (item.get("repository"), item.get("group"), item.get("name"))
            == (repository, package.group, package.artifact)
            and re.search(r"\.rhlw-\d+$", item.get("version", ""))
        )
        token = data.get("continuationToken")
        if not token:
            return components
    raise CleanupError("Nexus component pagination exceeded 100 pages")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("package", nargs="?", help="artifact name or group:artifact; omitted means all tracked packages")
    parser.add_argument("--guid", help="tenant GUID (required when the cluster has multiple tenants)")
    parser.add_argument("--context", help="oc context (defaults to the current context)")
    parser.add_argument("--dry-run", action="store_true", help="show the exact cleanup plan without changing anything")
    parser.add_argument("--insecure", action="store_true", help="allow an untrusted lab Route certificate")
    args = parser.parse_args()

    cluster = Cluster(args.context)
    guid = discover_guid(cluster, args.guid)
    configmap = cluster.get_json("configmap", "tenant-integration", "-n", f"lightwell-tenant-{guid}")
    config = configmap["data"]
    if config.get("GUID") != guid:
        raise CleanupError("Tenant integration ConfigMap GUID does not match the selected tenant")
    sdlc_ns = config["SDLC_NAMESPACE"]
    nexus_ns = config["NEXUS_NAMESPACE"]
    project = config["REMEDIATION_APP_GITLAB_PATH"]
    repos = [r.strip() for r in config["NEXUS_WEBHOOK_REPOSITORIES"].split(",")]
    matching_repos = [r for r in repos if r == f"redhat-packages-remediated-{guid}"]
    if len(matching_repos) != 1:
        raise CleanupError("Tenant config has no unique remediated Nexus repository")
    repo = matching_repos[0]

    inventory = dashboard_inventory(cluster, sdlc_ns)
    if not inventory:
        raise CleanupError("Dashboard package inventory is empty; refusing to claim a complete cleanup")
    packages = choose_packages(inventory, args.package)
    selected = {p.coordinate for p in packages}

    gitlab_token = cluster.secret(sdlc_ns, "gitlab-root-pat", "token")
    nexus_user = cluster.secret(nexus_ns, "nexus-admin-secret", "user")
    nexus_password = cluster.secret(nexus_ns, "nexus-admin-secret", "password")
    basic = base64.b64encode(f"{nexus_user}:{nexus_password}".encode()).decode()
    gitlab = API(config["GITLAB_URL"], {"PRIVATE-TOKEN": gitlab_token}, args.insecure)
    nexus = API(config["NEXUS_URL"], {"Authorization": f"Basic {basic}"}, args.insecure)
    encoded_project = urllib.parse.quote(project, safe="")

    mrs = [mr for mr in gitlab_mrs(gitlab, project) if package_mr(mr, selected)]
    components = [(p, item) for p in packages for item in nexus_components(nexus, repo, p)]
    runs = dashboard_runs(cluster, sdlc_ns)
    removed_runs = [r for r in runs if f"{r.get('group')}:{r.get('artifact')}" in selected]
    retained_runs = [r for r in runs if r not in removed_runs]

    branches = set()
    for mr in mrs:
        if mr["state"] != "merged":
            branches.add(mr["source_branch"])
    for package in packages:
        for version in package.versions:
            if not re.search(r"\.rhlw-\d+$", version):
                continue
            branch = f"update-artifact-{version}"
            if branch in branches:
                continue
            path = f"/api/v4/projects/{encoded_project}/repository/branches/{urllib.parse.quote(branch, safe='')}"
            status, data = gitlab.request("GET", path, missing_ok=True)
            title = (data or {}).get("commit", {}).get("title", "") if status == 200 else ""
            if status == 200 and title == f"chore(deps): update {package.artifact} to {version}":
                branches.add(branch)

    print(f"Tenant {guid}: {project} / {repo}")
    print("Packages:", ", ".join(p.coordinate for p in packages))
    print("Merge requests:", ", ".join(f"!{m['iid']} ({m['state']})" for m in mrs) or "none")
    print("Branches:", ", ".join(sorted(branches)) or "none")
    print("Nexus components:", ", ".join(f"{p.artifact}:{c['version']}" for p, c in components) or "none")
    print("MR test resource candidates:", ", ".join(f"!{m['iid']}" for m in mrs) or "none")
    print("Dashboard runs:", ", ".join(r["id"] for r in removed_runs) or "none")
    if args.dry_run:
        print("Dry run: no changes made.")
        return 0

    # Every step is idempotent: rerun after an interruption to finish cleanup.
    for mr in mrs:
        if mr["state"] == "opened":
            path = f"/api/v4/projects/{encoded_project}/merge_requests/{mr['iid']}"
            _, updated = gitlab.request("PUT", path, {"state_event": "close"})
            if updated.get("state") != "closed":
                raise CleanupError(f"GitLab did not close MR !{mr['iid']}")
            print(f"Closed MR !{mr['iid']}")
    for branch in sorted(branches):
        path = f"/api/v4/projects/{encoded_project}/repository/branches/{urllib.parse.quote(branch, safe='')}"
        status, _ = gitlab.request("DELETE", path, missing_ok=True)
        print(f"Branch {branch}: {'already absent' if status == 404 else 'deleted'}")
    for mr in mrs:
        iid = str(mr["iid"])
        cluster.run("delete", "job", f"verify-mr-{iid}", "-n", "sdlc-sandboxes", "--ignore-not-found")
        cluster.run("delete", "namespace", f"pr-test-mr-{iid}", "--ignore-not-found", "--wait=false")
    for package, component in components:
        path = "/service/rest/v1/components/" + urllib.parse.quote(component["id"], safe="")
        nexus.request("DELETE", path, missing_ok=True)
        print(f"Deleted {package.coordinate}:{component['version']} from Nexus")
    if removed_runs:
        pods = cluster.get_json("pods", "-n", sdlc_ns, "-l", "app=demo-dashboard")["items"]
        if len(pods) != 1:
            raise CleanupError(f"Expected one dashboard pod, found {len(pods)}")
        pod_name = pods[0]["metadata"]["name"]
        old_restarts = pods[0]["status"]["containerStatuses"][0]["restartCount"]
        payload = json.dumps(retained_runs, indent=2).encode()
        code = (
            'import pathlib,sys; p=pathlib.Path("/var/lib/dashboard/runs.json");'
            'p.write_bytes(sys.stdin.buffer.read())'
        )
        cluster.run("exec", "-i", "-n", sdlc_ns, "deploy/demo-dashboard", "--", "python3", "-c", code, input_data=payload)
        # The backend holds runs in memory. Restart its container (same pod and
        # emptyDir) to reload the filtered file without losing other packages.
        try:
            cluster.run(
                "exec", "-n", sdlc_ns, pod_name, "--", "python3", "-c",
                "import os,signal;os.kill(1,signal.SIGTERM)",
            )
        except CleanupError:
            pass  # exec may disconnect when PID 1 exits
        for _ in range(45):
            time.sleep(2)
            try:
                pod = cluster.get_json("pod", pod_name, "-n", sdlc_ns)
                container = pod["status"]["containerStatuses"][0]
                if (container["restartCount"] > old_restarts and container["ready"]
                        and dashboard_runs(cluster, sdlc_ns) == retained_runs):
                    break
            except (CleanupError, KeyError, IndexError):
                continue
        else:
            raise CleanupError("Dashboard did not reload the filtered run history")
        print(f"Cleared {len(removed_runs)} dashboard run(s); kept {len(retained_runs)}")

    # Re-read Nexus so the script reports a demonstrably reusable trigger.
    remaining = [(p, c) for p in packages for c in nexus_components(nexus, repo, p)]
    if remaining:
        raise CleanupError(f"{len(remaining)} selected Nexus components remain cached")
    print("Done. Selected remediated packages are no longer cached in Nexus.")
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except (CleanupError, KeyError, json.JSONDecodeError) as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        sys.exit(1)
