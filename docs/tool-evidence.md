# Tool Evidence

All source references are from Jenkins tag `jenkins-2.563`.

| Tool | Endpoint used | Evidence | Permission evidence / behavior |
| --- | --- | --- | --- |
| `jenkins_whoami` | `GET whoAmI/api/json` | `WhoAmI` exposes `/whoAmI` and `getApi()` at `hudson/security/WhoAmI.java:22-38`. | Unprotected root action, reports current authentication. |
| `jenkins_version` | `GET api/json`, read `X-Jenkins` header | `Api.setHeaders` sets `X-Jenkins` at `hudson/model/Api.java:307-314`; official docs say any `.../api/*` page exposes version header. | Same permission as top API. |
| `jenkins_health` | `GET api/json` | `Jenkins.getApi()` at `jenkins/model/Jenkins.java:1368-1377`; Remote Access API docs confirm top-level API. | Jenkins filters exported data by permissions. |
| `jenkins_get_json` | `GET <relative>/api/json` | Remote Access API docs say API is under `.../api/`; source uses `Api` across Jenkins model objects. | GET only; external URLs/traversal rejected locally. |
| `jenkins_list_jobs` | `GET api/json?tree=jobs[...]` | Jenkins exports `jobs` at `Jenkins.java:1769-1772`. | Only jobs visible to user are returned. |
| `jenkins_get_job` | `GET job/{name}/api/json` | `AbstractItem.getApi()` at `hudson/model/AbstractItem.java:598-603`; official docs show repeated `/job/...` path format. | Jenkins item permissions apply. |
| `jenkins_get_job_config` | `GET job/{name}/config.xml` | `AbstractItem.doConfigDotXml` maps `config.xml` at `AbstractItem.java:831-867`; read requires `EXTENDED_READ` and may redact without `CONFIGURE` at `AbstractItem.java:874-890`. | Requires Jenkins `EXTENDED_READ`; secrets may be redacted. |
| `jenkins_list_builds` | `GET job/{name}/api/json?tree=builds[...]` | `Job.getNewBuilds()` exports `builds` at `hudson/model/Job.java:787-793`. | Job read permissions apply. |
| `jenkins_get_build` | `GET job/{name}/{build}/api/json` | `Run.getApi()` at `hudson/model/Run.java:1530-1532`. | Build/job read permissions apply. |
| `jenkins_get_build_log` | `GET job/{name}/{build}/consoleText` | `Run.doConsoleText` serves raw console text at `Run.java:2217-2245`; `Run.getLogText()` and `AnnotatedLargeText.doProgressiveText` evidence progressive support at `Run.java:1510-1515` and `AnnotatedLargeText.java:127-141`. | Build/job read permissions apply; output is truncated locally. |
| `jenkins_get_build_artifacts` | `GET job/{name}/{build}/api/json?tree=artifacts[...]` | `Run.getArtifacts()` is exported at `Run.java:1075-1080`; artifact browser at `Run.java:2183-2191`. | `ARTIFACTS` permission may apply when enabled. |
| `jenkins_get_test_report` | `GET job/{name}/{build}/testReport/api/json` | Core references test report actions at `AbstractBuild.java:1096-1113`; official JUnit plugin docs state test report functionality was split from core. | Plugin-dependent; 404 is returned clearly when no test action/plugin exists. |
| `jenkins_list_queue` | `GET queue/api/json` | `Queue.getApi()` at `hudson/model/Queue.java:1955-1957`; `Queue.getItems()` exported at `Queue.java:787-805`. | Jenkins queue visibility/permissions apply. |
| `jenkins_get_queue_item` | `GET queue/item/{id}/api/json` | `Queue.Item.getUrl()` returns `queue/item/{id}/` at `Queue.java:2449-2451`; Jenkins test uses `queue/item/{id}/api/xml`. | Item read/discover permissions apply. |
| `jenkins_list_views` | `GET api/json?tree=views[...]` | Jenkins exports views at `Jenkins.java:1870-1876`. | Jenkins view permissions apply. |
| `jenkins_get_view` | `GET view/{name}/api/json` | `View.getApi()` at `hudson/model/View.java:623-625`; `View.getItems()` exports view jobs at `View.java:191-196`. | Jenkins view/item permissions apply. |
| `jenkins_list_nodes` | `GET computer/api/json` | `Jenkins.getComputer()` binds `/computer/` at `Jenkins.java:1478-1485`; `ComputerSet.getApi()` at `ComputerSet.java:470-472`. | Jenkins computer permissions apply. |
| `jenkins_get_node` | `GET computer/{name}/api/json` | `Jenkins.getComputer(name)` resolves names at `Jenkins.java:1986-1994`; `Computer.getApi()` at `Computer.java:1423-1425`. | Jenkins computer permissions apply. |
| `jenkins_list_plugins` | `GET pluginManager/api/json` | `Jenkins.getPluginManager()` at `Jenkins.java:1330-1332`; `PluginManager.getApi()` checks `SYSTEM_READ` at `PluginManager.java:416-418`; plugins exported at `PluginManager.java:1268-1273`. | Requires `SYSTEM_READ`; non-admin users may get 403. |
| `jenkins_start_workspace_bundle_download` | `GET job/{name}/{build}/api/json`, `GET job/{name}/ws/**/*zip*/{safe-job}{build}.zip`, `GET job/{name}/{build}/consoleText` | Build number is resolved through `Run.getApi()` at `Run.java:1530-1532`; workspace files are served by `AbstractProject.doWs` at `AbstractProject.java:1905-1927`; `DirectoryBrowserSupport` recognizes `*zip*` at `DirectoryBrowserSupport.java:221-226` and writes zip archives at `DirectoryBrowserSupport.java:262-275`; console text is served at `Run.java:2217-2245`. | Requires local workspace download gate and Jenkins `Item.WORKSPACE` plus build/job read permissions. Workspace is job-level/current available workspace, not guaranteed to be an immutable build snapshot. |
| `jenkins_get_workspace_bundle_status` | Local progress file read | Reads `.progress.json` written by the server under `JENKINS_MCP_WORKSPACE_DOWNLOAD_DIR`. | Requires local workspace download config. No Jenkins request. |
| `jenkins_cancel_workspace_bundle_download` | Local cancel marker write | Writes `.cancel` under the operation directory; the background worker checks it during archive download, extraction, and log download. | Requires local workspace download config. No Jenkins request. |
| `jenkins_trigger_build` | `POST job/{name}/build` | Official docs say POST `JENKINS_URL/job/JOBNAME/build`; source `doBuild` at `ParameterizedJobMixIn.java:205-237`. | Requires local write gate and Jenkins build permission. |
| `jenkins_trigger_build_with_parameters` | `POST job/{name}/buildWithParameters` | Official docs show `buildWithParameters`; source at `ParameterizedJobMixIn.java:243-254`. | Requires local write gate and Jenkins build permission. |
| `jenkins_stop_build` | `POST job/{name}/{build}/stop` | `AbstractBuild.doStop()` is `@RequirePOST` at `AbstractBuild.java:1405-1415`. | Requires local write gate and Jenkins build stop/cancel permission. |
| `jenkins_cancel_queue_item` | `POST queue/cancelItem?id={id}` | `Queue.doCancelItem` is `@RequirePOST` at `Queue.java:755-773`. | Requires local write gate and queue/item cancel permission. |
| `jenkins_enable_job` | `POST job/{name}/enable` | `ParameterizedJob.doEnable` is `@RequirePOST` at `ParameterizedJobMixIn.java:547-552`. | Requires local write gate and Jenkins `CONFIGURE`. |
| `jenkins_disable_job` | `POST job/{name}/disable` | `ParameterizedJob.doDisable` is `@RequirePOST` at `ParameterizedJobMixIn.java:539-545`. | Requires local write gate and Jenkins `CONFIGURE`. |
| `jenkins_create_job` | `POST createItem?name=...` | `Jenkins.doCreateItem` is `@RequirePOST` at `Jenkins.java:4229-4232`; XML creation path at `ItemGroupMixIn.java:147-193`. | Optional; requires write + job-config gate and Jenkins `CREATE`. |
| `jenkins_copy_job` | `POST createItem?mode=copy&from=...&name=...` | Copy mode in `ItemGroupMixIn.java:173-188`; permission checks in `ItemGroupMixIn.java:231-255`. | Optional; requires write + job-config gate and Jenkins permissions. |
| `jenkins_update_job_config` | `POST job/{name}/config.xml` | POST branch in `AbstractItem.doConfigDotXmlImpl` at `AbstractItem.java:864-867`; update path at `AbstractItem.java:917-943`. | Optional; requires write + job-config gate and Jenkins `CONFIGURE`. |
| `jenkins_delete_job` | `POST job/{name}/doDelete` | `AbstractItem.doDoDelete` is `@RequirePOST` at `AbstractItem.java:687-705`. | Optional; requires write + job-config + delete gates and Jenkins `DELETE`. |

## Explicitly Not Implemented

Script console, restart, safe restart, quiet down, plugin install/update, credential read/write, node creation/deletion, global config changes, and user management are intentionally absent.
