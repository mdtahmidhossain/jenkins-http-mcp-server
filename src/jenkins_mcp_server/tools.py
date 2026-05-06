from __future__ import annotations

from typing import Any

from mcp.server.fastmcp import FastMCP

from .client import JenkinsClient, append_api_json, job_path, normalize_relative_path, safe_segment
from .config import JenkinsConfig
from .errors import JenkinsMCPError
from .workspace_bundle import (
    cancel_workspace_bundle,
    read_workspace_bundle_status,
    start_workspace_bundle_download,
)


def _ok(data: Any) -> dict[str, Any]:
    return {"ok": True, "data": data}


def _run(fn):
    try:
        return _ok(fn())
    except JenkinsMCPError as exc:
        return exc.to_dict()


def _client() -> JenkinsClient:
    return JenkinsClient.from_env()


def _get_json(path: str, params: dict[str, Any] | None = None) -> Any:
    with _client() as client:
        return client.get_json(path, params=params)


def _get_text(path: str) -> str:
    with _client() as client:
        return client.get_text(path)


def _build_path(job: str | list[str], build: int | str) -> str:
    build_id = str(build)
    if not build_id or build_id in {".", ".."} or "/" in build_id:
        from .errors import PathValidationError

        raise PathValidationError("build must be a number or permalink path segment")
    return f"{job_path(job)}/{safe_segment(build_id, 'build')}"


def _query(tree: str | None = None, depth: int | None = None) -> dict[str, Any]:
    params: dict[str, Any] = {}
    if tree:
        params["tree"] = tree
    if depth is not None:
        params["depth"] = depth
    return params


def register_tools(mcp: FastMCP) -> None:
    @mcp.tool()
    def jenkins_whoami() -> dict[str, Any]:
        """Return the authenticated Jenkins identity from /whoAmI/api/json."""
        return _run(lambda: _get_json("whoAmI"))

    @mcp.tool()
    def jenkins_version() -> dict[str, Any]:
        """Return Jenkins version from the X-Jenkins response header."""

        def op() -> dict[str, Any]:
            with _client() as client:
                response = client.request("GET", "api/json", params={"tree": "mode"})
                return {
                    "version": response.headers.get("X-Jenkins"),
                    "session": response.headers.get("X-Jenkins-Session"),
                }

        return _run(op)

    @mcp.tool()
    def jenkins_health() -> dict[str, Any]:
        """Return a small health snapshot from top-level Jenkins JSON and version headers."""

        def op() -> dict[str, Any]:
            with _client() as client:
                response = client.request(
                    "GET",
                    "api/json",
                    params={
                        "tree": (
                            "mode,nodeDescription,nodeName,numExecutors,quietingDown,useCrumbs"
                        )
                    },
                )
                payload = response.json()
                payload["version"] = response.headers.get("X-Jenkins")
                return payload

        return _run(op)

    @mcp.tool()
    def jenkins_get_json(path: str, query: dict[str, str | int] | None = None) -> dict[str, Any]:
        """GET a relative Jenkins JSON API path. Rejects external URLs and traversal."""

        def op() -> Any:
            relative = append_api_json(normalize_relative_path(path))
            return _get_json(relative, params=query)

        return _run(op)

    @mcp.tool()
    def jenkins_list_jobs(
        tree: str = "jobs[name,fullName,url,color,_class]",
        depth: int | None = None,
    ) -> dict[str, Any]:
        """List jobs visible to the Jenkins user."""
        return _run(lambda: _get_json("api/json", params=_query(tree, depth)))

    @mcp.tool()
    def jenkins_get_job(job: str | list[str], tree: str | None = None) -> dict[str, Any]:
        """Get one job by Jenkins job path. Nested paths use slash-separated names or a list."""
        return _run(lambda: _get_json(job_path(job), params=_query(tree)))

    @mcp.tool()
    def jenkins_get_job_config(job: str | list[str]) -> dict[str, Any]:
        """Read job config.xml. Jenkins may redact secrets without Configure permission."""
        return _run(lambda: _get_text(f"{job_path(job)}/config.xml"))

    @mcp.tool()
    def jenkins_list_builds(
        job: str | list[str],
        tree: str = "builds[number,url,result,building,timestamp,duration]",
    ) -> dict[str, Any]:
        """List recent builds for a job."""
        return _run(lambda: _get_json(job_path(job), params={"tree": tree}))

    @mcp.tool()
    def jenkins_get_build(
        job: str | list[str],
        build: int | str,
        tree: str | None = None,
    ) -> dict[str, Any]:
        """Get a build by number or Jenkins permalink such as lastBuild."""
        return _run(lambda: _get_json(_build_path(job, build), params=_query(tree)))

    @mcp.tool()
    def jenkins_get_build_log(job: str | list[str], build: int | str) -> dict[str, Any]:
        """Get consoleText for a build, truncated by JENKINS_MCP_MAX_LOG_BYTES."""

        def op() -> dict[str, Any]:
            with _client() as client:
                return client.get_text_limited(
                    f"{_build_path(job, build)}/consoleText",
                    limit=client.config.max_log_bytes,
                )

        return _run(op)

    @mcp.tool()
    def jenkins_get_build_artifacts(job: str | list[str], build: int | str) -> dict[str, Any]:
        """List artifacts exported on a build JSON API response."""
        tree = "artifacts[displayPath,fileName,relativePath]"
        return _run(lambda: _get_json(_build_path(job, build), params={"tree": tree}))

    @mcp.tool()
    def jenkins_get_test_report(job: str | list[str], build: int | str) -> dict[str, Any]:
        """Get /testReport/api/json when a test-report plugin such as JUnit provides it."""
        return _run(lambda: _get_json(f"{_build_path(job, build)}/testReport"))

    @mcp.tool()
    def jenkins_list_queue(
        tree: str = "items[id,url,why,blocked,buildable,stuck]",
    ) -> dict[str, Any]:
        """List visible Jenkins queue items."""
        return _run(lambda: _get_json("queue", params={"tree": tree}))

    @mcp.tool()
    def jenkins_get_queue_item(item_id: int) -> dict[str, Any]:
        """Get one Jenkins queue item by ID."""
        return _run(lambda: _get_json(f"queue/item/{item_id}"))

    @mcp.tool()
    def jenkins_list_views(tree: str = "views[name,url,_class]") -> dict[str, Any]:
        """List Jenkins views visible to the user."""
        return _run(lambda: _get_json("api/json", params={"tree": tree}))

    @mcp.tool()
    def jenkins_get_view(view: str, tree: str | None = None) -> dict[str, Any]:
        """Get one Jenkins view by name."""
        return _run(lambda: _get_json(f"view/{safe_segment(view, 'view')}", params=_query(tree)))

    @mcp.tool()
    def jenkins_list_nodes(
        tree: str = (
            "computer[displayName,offline,temporarilyOffline,numExecutors,assignedLabels[name]]"
        ),
    ) -> dict[str, Any]:
        """List Jenkins computers/nodes visible to the user."""
        return _run(lambda: _get_json("computer", params={"tree": tree}))

    @mcp.tool()
    def jenkins_get_node(node: str) -> dict[str, Any]:
        """Get one Jenkins computer/node by name. Use (built-in) for the built-in node."""
        value = "(built-in)" if node == "" else node
        return _run(lambda: _get_json(f"computer/{safe_segment(value, 'node')}"))

    @mcp.tool()
    def jenkins_list_plugins(
        tree: str = "plugins[shortName,longName,version,active,enabled,pinned,hasUpdate,deleted]",
    ) -> dict[str, Any]:
        """List installed Jenkins plugins visible through pluginManager/api/json."""
        return _run(lambda: _get_json("pluginManager", params={"tree": tree}))

    @mcp.tool()
    def jenkins_trigger_build(job: str | list[str], delay: str | None = None) -> dict[str, Any]:
        """Trigger a non-parameterized job build. Requires JENKINS_MCP_ENABLE_WRITES=1."""

        def op() -> dict[str, Any]:
            config = JenkinsConfig.from_env()
            config.require_writes()
            with JenkinsClient(config) as client:
                params = {"delay": delay} if delay else None
                return client.post(f"{job_path(job)}/build", params=params)

        return _run(op)

    @mcp.tool()
    def jenkins_trigger_build_with_parameters(
        job: str | list[str],
        parameters: dict[str, str | int | float | bool],
        delay: str | None = None,
    ) -> dict[str, Any]:
        """Trigger a parameterized job build. Requires JENKINS_MCP_ENABLE_WRITES=1."""

        def op() -> dict[str, Any]:
            config = JenkinsConfig.from_env()
            config.require_writes()
            data = {key: str(value) for key, value in parameters.items()}
            if delay:
                data["delay"] = delay
            with JenkinsClient(config) as client:
                return client.post(f"{job_path(job)}/buildWithParameters", data=data)

        return _run(op)

    @mcp.tool()
    def jenkins_stop_build(job: str | list[str], build: int | str) -> dict[str, Any]:
        """Stop a running build. Requires JENKINS_MCP_ENABLE_WRITES=1."""

        def op() -> dict[str, Any]:
            config = JenkinsConfig.from_env()
            config.require_writes()
            with JenkinsClient(config) as client:
                return client.post(f"{_build_path(job, build)}/stop")

        return _run(op)

    @mcp.tool()
    def jenkins_cancel_queue_item(item_id: int) -> dict[str, Any]:
        """Cancel a Jenkins queue item. Requires JENKINS_MCP_ENABLE_WRITES=1."""

        def op() -> dict[str, Any]:
            config = JenkinsConfig.from_env()
            config.require_writes()
            with JenkinsClient(config) as client:
                return client.post("queue/cancelItem", params={"id": item_id})

        return _run(op)

    @mcp.tool()
    def jenkins_disable_job(job: str | list[str]) -> dict[str, Any]:
        """Disable a job. Requires JENKINS_MCP_ENABLE_WRITES=1."""

        def op() -> dict[str, Any]:
            config = JenkinsConfig.from_env()
            config.require_writes()
            with JenkinsClient(config) as client:
                return client.post(f"{job_path(job)}/disable")

        return _run(op)

    @mcp.tool()
    def jenkins_enable_job(job: str | list[str]) -> dict[str, Any]:
        """Enable a job. Requires JENKINS_MCP_ENABLE_WRITES=1."""

        def op() -> dict[str, Any]:
            config = JenkinsConfig.from_env()
            config.require_writes()
            with JenkinsClient(config) as client:
                return client.post(f"{job_path(job)}/enable")

        return _run(op)

    @mcp.tool()
    def jenkins_create_job(name: str, config_xml: str) -> dict[str, Any]:
        """Create a top-level job from config.xml. Requires write and job-config flags."""

        def op() -> dict[str, Any]:
            config = JenkinsConfig.from_env()
            config.require_job_config_write()
            with JenkinsClient(config) as client:
                return client.post(
                    "createItem",
                    params={"name": name},
                    content=config_xml,
                    headers={"Content-Type": "application/xml"},
                )

        return _run(op)

    @mcp.tool()
    def jenkins_copy_job(from_job: str, new_name: str) -> dict[str, Any]:
        """Copy a top-level job. Requires write and job-config flags."""

        def op() -> dict[str, Any]:
            config = JenkinsConfig.from_env()
            config.require_job_config_write()
            with JenkinsClient(config) as client:
                return client.post(
                    "createItem",
                    params={"mode": "copy", "from": from_job, "name": new_name},
                )

        return _run(op)

    @mcp.tool()
    def jenkins_update_job_config(job: str | list[str], config_xml: str) -> dict[str, Any]:
        """Replace job config.xml. Requires write and job-config flags."""

        def op() -> dict[str, Any]:
            config = JenkinsConfig.from_env()
            config.require_job_config_write()
            with JenkinsClient(config) as client:
                return client.post(
                    f"{job_path(job)}/config.xml",
                    content=config_xml,
                    headers={"Content-Type": "application/xml"},
                )

        return _run(op)

    @mcp.tool()
    def jenkins_delete_job(job: str | list[str]) -> dict[str, Any]:
        """Delete a job. Requires write, job-config, and delete flags."""

        def op() -> dict[str, Any]:
            config = JenkinsConfig.from_env()
            config.require_delete()
            with JenkinsClient(config) as client:
                return client.post(f"{job_path(job)}/doDelete")

        return _run(op)

    @mcp.tool()
    def jenkins_start_workspace_bundle_download(
        job: str | list[str],
        build: int | str = "lastBuild",
    ) -> dict[str, Any]:
        """Start async workspace zip download, extraction, cleanup, and console log save."""
        return _run(lambda: start_workspace_bundle_download(job, build))

    @mcp.tool()
    def jenkins_get_workspace_bundle_status(operation_id: str) -> dict[str, Any]:
        """Get download/extract/log progress, bytes, speed, paths, and final status."""
        return _run(lambda: read_workspace_bundle_status(operation_id))

    @mcp.tool()
    def jenkins_cancel_workspace_bundle_download(operation_id: str) -> dict[str, Any]:
        """Request cancellation of a running workspace bundle operation."""
        return _run(lambda: cancel_workspace_bundle(operation_id))


READ_ONLY_TOOLS = [
    "jenkins_whoami",
    "jenkins_version",
    "jenkins_health",
    "jenkins_get_json",
    "jenkins_list_jobs",
    "jenkins_get_job",
    "jenkins_get_job_config",
    "jenkins_list_builds",
    "jenkins_get_build",
    "jenkins_get_build_log",
    "jenkins_get_build_artifacts",
    "jenkins_get_test_report",
    "jenkins_list_queue",
    "jenkins_get_queue_item",
    "jenkins_list_views",
    "jenkins_get_view",
    "jenkins_list_nodes",
    "jenkins_get_node",
    "jenkins_list_plugins",
]

WRITE_TOOLS = [
    "jenkins_trigger_build",
    "jenkins_trigger_build_with_parameters",
    "jenkins_stop_build",
    "jenkins_cancel_queue_item",
    "jenkins_enable_job",
    "jenkins_disable_job",
]

OPTIONAL_JOB_CONFIG_TOOLS = [
    "jenkins_create_job",
    "jenkins_copy_job",
    "jenkins_update_job_config",
    "jenkins_delete_job",
]

WORKSPACE_BUNDLE_TOOLS = [
    "jenkins_start_workspace_bundle_download",
    "jenkins_get_workspace_bundle_status",
    "jenkins_cancel_workspace_bundle_download",
]
