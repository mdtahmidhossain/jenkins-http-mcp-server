# Jenkins 2.563 Source Truth

Date checked: 2026-05-06

## Checkout

- Repository: https://github.com/jenkinsci/jenkins.git
- Exact release tag found: `jenkins-2.563`
- Commit SHA: `d8b428cc7675c7769f7b98cf830c8cd8035b3866`
- Checkout command used: `git checkout --detach jenkins-2.563`

## Version Evidence

- `vendor/jenkins/pom.xml:76` has `<revision>2.563</revision>`.
- `vendor/jenkins/pom.xml:77` has an empty `<changelist></changelist>`.
- `git describe --tags --exact-match` returned `jenkins-2.563`.

## Files Inspected

- `pom.xml`
- `core/src/main/java/jenkins/model/Jenkins.java`
- `core/src/main/java/hudson/model/Api.java`
- `core/src/main/java/hudson/security/WhoAmI.java`
- `core/src/main/java/hudson/security/csrf/CrumbIssuer.java`
- `core/src/main/java/hudson/security/csrf/CrumbFilter.java`
- `core/src/main/java/jenkins/security/BasicHeaderApiTokenAuthenticator.java`
- `core/src/main/java/jenkins/security/BasicApiTokenHelper.java`
- `core/src/main/java/hudson/model/AbstractItem.java`
- `core/src/main/java/hudson/model/Job.java`
- `core/src/main/java/jenkins/model/ParameterizedJobMixIn.java`
- `core/src/main/java/hudson/model/AbstractBuild.java`
- `core/src/main/java/hudson/model/Run.java`
- `core/src/main/java/hudson/model/Queue.java`
- `core/src/main/java/hudson/model/ComputerSet.java`
- `core/src/main/java/hudson/model/Computer.java`
- `core/src/main/java/hudson/model/View.java`
- `core/src/main/java/hudson/PluginManager.java`
- `core/src/main/java/hudson/console/AnnotatedLargeText.java`
- `core/src/main/java/hudson/model/ItemGroupMixIn.java`

## Relevant Line References

- Top-level API: `Jenkins.getApi()` returns `new Api(this)` at `Jenkins.java:1368-1377`.
- API version header: `Api.setHeaders` sets `X-Jenkins` at `Api.java:307-314`.
- Jobs export: Jenkins exports top-level items as `jobs` at `Jenkins.java:1769-1772`.
- Nested job URL shape: official Remote Access API example uses repeated `/job/.../job/...` path segments.
- WhoAmI: `WhoAmI` exposes `/whoAmI` and returns `new Api(this)` at `WhoAmI.java:22-38`.
- Crumb issuer API: `CrumbIssuer.getApi()` returns `RestrictedApi` at `CrumbIssuer.java:238-240`.
- Crumb enforcement: `CrumbFilter` validates POST crumbs at `CrumbFilter.java:124-148`.
- API token auth: `BasicHeaderApiTokenAuthenticator` checks Basic auth token at `BasicHeaderApiTokenAuthenticator.java:31-48`.
- Job config read/write endpoint: `AbstractItem.doConfigDotXml` maps `config.xml` at `AbstractItem.java:831-867`.
- Job config read permission/redaction: `writeConfigDotXml` requires `EXTENDED_READ` and redacts if lacking `CONFIGURE` at `AbstractItem.java:874-890`.
- Build trigger endpoints: `ParameterizedJobMixIn.doBuild` and `doBuildWithParameters` at `ParameterizedJobMixIn.java:205-254`.
- Queue cancellation endpoint: `Queue.doCancelItem` at `Queue.java:755-773`.
- Queue item API: `Queue.Item.getUrl()` returns `queue/item/{id}/` at `Queue.java:2449-2451`.
- Build stop endpoint: `AbstractBuild.doStop()` is `@RequirePOST` at `AbstractBuild.java:1405-1415`.
- Enable/disable endpoints: `ParameterizedJob.doDisable` and `doEnable` are `@RequirePOST` at `ParameterizedJobMixIn.java:539-552`.
- Build JSON API: `Run.getApi()` returns `new Api(this)` at `Run.java:1530-1532`.
- Build log text: `Run.doConsoleText` serves raw console output at `Run.java:2217-2245`.
- Progressive log binding: `Run.getLogText()` binds log text at `Run.java:1510-1515`; `AnnotatedLargeText.doProgressiveText` delegates to progress text at `AnnotatedLargeText.java:127-141`.
- Workspace browsing: `AbstractProject.doWs` serves workspace files and checks `Item.WORKSPACE` at `AbstractProject.java:1905-1927`.
- Workspace/directory zip: `DirectoryBrowserSupport` recognizes `*zip*` at `DirectoryBrowserSupport.java:221-226` and writes zip archives at `DirectoryBrowserSupport.java:262-275`.
- Artifacts: `Run.getArtifacts()` is `@Exported` at `Run.java:1075-1080`; `Run.doArtifact()` serves artifacts at `Run.java:2183-2191`.
- Queue API: `Queue.getApi()` at `Queue.java:1955-1957`; `Queue.getItems()` is exported at `Queue.java:787-805`.
- Nodes/computers API: `Jenkins.getComputer()` binds `/computer/` at `Jenkins.java:1478-1485`; `ComputerSet.getApi()` at `ComputerSet.java:470-472`; `Computer.getApi()` at `Computer.java:1423-1425`.
- Views API: Jenkins exports views at `Jenkins.java:1870-1876`; `View.getApi()` at `View.java:623-625`; `View.getItems()` exports view jobs at `View.java:191-196`.
- Plugins API: `Jenkins.getPluginManager()` at `Jenkins.java:1330-1332`; `PluginManager.getApi()` checks `SYSTEM_READ` at `PluginManager.java:416-418`; plugins are exported at `PluginManager.java:1268-1273`.
