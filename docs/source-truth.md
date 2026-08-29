# Jenkins 2.579 Source Truth

Date checked: 2026-08-30

## Checkout

- Repository: https://github.com/jenkinsci/jenkins.git
- Exact release tag found: `jenkins-2.579`
- Commit SHA: `9095ea3a5c5e7dcd392695a5dd880af1c9910ddf`
- Checkout command used: `git checkout --detach jenkins-2.579`

## Version Evidence

- `vendor/jenkins/pom.xml:76` has `<revision>2.579</revision>`.
- `vendor/jenkins/pom.xml:77` has an empty `<changelist></changelist>`.
- `git describe --tags --exact-match` returned `jenkins-2.579`.
- `git rev-parse HEAD` returned `9095ea3a5c5e7dcd392695a5dd880af1c9910ddf`.

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
- `core/src/main/java/hudson/model/AbstractProject.java`
- `core/src/main/java/hudson/model/DirectoryBrowserSupport.java`
- `core/src/main/java/hudson/model/ItemGroupMixIn.java`

## Relevant Line References

- Top-level API: `Jenkins.getApi()` returns `new Api(this)` at `Jenkins.java:1368-1378`.
- API version header: `Api.setHeaders` sets `X-Jenkins` at `Api.java:307-314`.
- Jobs export: Jenkins exports top-level items as `jobs` at `Jenkins.java:1769-1772`.
- Nested job URL shape: official Remote Access API examples use repeated `/job/.../job/...` path segments.
- WhoAmI: `WhoAmI` exposes `/whoAmI` and returns `new Api(this)` at `WhoAmI.java:22-39`.
- Crumb issuer API: `CrumbIssuer.getApi()` returns `RestrictedApi` at `CrumbIssuer.java:238-240`.
- Crumb enforcement: `CrumbFilter` validates POST crumbs at `CrumbFilter.java:124-153`.
- API token auth: `BasicHeaderApiTokenAuthenticator` checks Basic auth token at `BasicHeaderApiTokenAuthenticator.java:31-48`.
- Job config endpoint: `AbstractItem.doConfigDotXml` maps GET and POST `config.xml` at `AbstractItem.java:837-877`.
- Job config read: `writeConfigDotXml` requires `EXTENDED_READ`, serializes the in-memory item, and applies redaction without `CONFIGURE` at `AbstractItem.java:880-905`.
- Job config update: `updateByXml` requires `CONFIGURE`, validates/deserializes the submission, then reserializes it so plaintext secrets are encrypted at `AbstractItem.java:918-951`.
- Build trigger endpoints: `ParameterizedJobMixIn.doBuild` and `doBuildWithParameters` are at `ParameterizedJobMixIn.java:205-255`.
- Queue cancellation endpoint: `Queue.doCancelItem` applies read and cancel checks at `Queue.java:756-774`.
- Queue item API: `Queue.Item.getUrl()` returns `queue/item/{id}/` at `Queue.java:2444-2452`.
- Build stop endpoint: `AbstractBuild.doStop()` is `@RequirePOST` at `AbstractBuild.java:1405-1415`.
- Enable/disable endpoints: `ParameterizedJob.doDisable` and `doEnable` are `@RequirePOST` at `ParameterizedJobMixIn.java:539-552`.
- Build JSON API: `Run.getApi()` returns `new Api(this)` at `Run.java:1530-1532`.
- Build log text: `Run.doConsoleText` serves raw UTF-8 console output at `Run.java:2217-2245`.
- Progressive log binding: `Run.getLogText()` binds log text at `Run.java:1510-1515`; `AnnotatedLargeText.doProgressiveText` delegates at `AnnotatedLargeText.java:127-141`.
- Workspace browsing: `AbstractProject.doWs` serves workspace files and checks `Item.WORKSPACE` at `AbstractProject.java:1904-1927`.
- Workspace/directory zip: `DirectoryBrowserSupport` recognizes `*zip*` at `DirectoryBrowserSupport.java:205-226` and writes zip archives at `DirectoryBrowserSupport.java:262-275`.
- Artifacts: `Run.getArtifacts()` is `@Exported` at `Run.java:1075-1080`; `Run.doArtifact()` serves artifacts at `Run.java:2183-2191`.
- Queue API: `Queue.getApi()` is at `Queue.java:1955-1957`; `Queue.getItems()` is exported at `Queue.java:787-810`.
- Nodes/computers API: `Jenkins.getComputer()` binds `/computer/` at `Jenkins.java:1478-1485`; `ComputerSet.getApi()` is at `ComputerSet.java:470-472`; `Computer.getApi()` is at `Computer.java:1423-1425`.
- Views API: Jenkins exports views at `Jenkins.java:1870-1876`; `View.getApi()` is at `View.java:623-625`; `View.getItems()` exports view jobs at `View.java:191-196`.
- Plugins API: `Jenkins.getPluginManager()` is at `Jenkins.java:1330-1332`; `PluginManager.getApi()` checks `SYSTEM_READ` at `PluginManager.java:416-418`; plugins are exported at `PluginManager.java:1268-1273`.

## Changes From 2.563

`git diff --shortstat jenkins-2.563..jenkins-2.579` reports 339 files changed, 9,266 insertions, and 4,205 deletions. Of the endpoint-relevant core classes listed above, six changed: `AnnotatedLargeText`, `AbstractItem`, `DirectoryBrowserSupport`, `Queue`, `WhoAmI`, and `Jenkins`.

- No route used by this MCP server changed.
- Jenkins 2.568 changed job `config.xml` reads and updates to serialize the in-memory item. XML formatting, comments, and ordering are not byte-preserving; submitted plaintext secrets are encrypted on reserialization. See the [2026-06-10 security advisory](https://www.jenkins.io/security/advisory/2026-06-10/).
- Jenkins 2.568 added an `Item/Read` check to the deprecated item-local queue cancellation endpoint. This server uses root `queue/cancelItem`, whose read and cancel checks remain at `Queue.java:756-774`.
- Jenkins 2.569 fixed HTML/progressive log tailing for partial lines. This server reads raw `consoleText`, whose route and behavior remain unchanged.
- Jenkins 2.570 added UTF-8 charsets when serving text files through directory browsers. Workspace file and zip byte streaming remain compatible.
- [Jenkins 2.574](https://www.jenkins.io/changelog/2.574/) stopped bundling JUnit and several other detached plugins. `jenkins_get_test_report` remains plugin-dependent.
- Jenkins 2.579 removed Apache Commons Lang 2 from core. The [2.579 changelog](https://www.jenkins.io/changelog/2.579/) instructs operators to update installed plugins before upgrading Jenkins.

The endpoint review found no required Python client, crumb, path encoding, workspace download, or MCP SDK change for Jenkins 2.579.
