# Deployment environment reference

Environment variables are for deployment and infrastructure, not feature choices.
This is the KEEP-ENV inventory: host paths, ports/topology, resource ceilings,
isolation, provisioning, SDK workload identity and conventional host inputs.
Optional names may be consumed by an external SDK/CLI rather than the app.
Keep credentials private; these names are **not** an instruction to persist them.

Process environment takes precedence over the configured env file for startup
settings. An empty non-text startup field retains its shipped default.

User behaviour belongs to persisted Settings or internal shipped defaults. For
one release the startup importer adopts explicitly set, valid, nonblank legacy
values only when the persisted key is absent. False and zero are explicit;
blank and absent are not adopted. Stored UI and synced peer values win. The
migration version is worker-local; imported owner-policy keys sync normally.
Remove the compatibility reader in the following release. Non-default overrides
removed for mandatory policy are not imported (including security bypasses).

Later area batches are tracked in `scripts/ci/env_allowlist.json`; their legacy
readers are temporary, **not** additions to this deployment contract. Scratch
cleanup is classified DEFAULT for PR 3: always on, no toggle, preserving its
retention and live-producer protections. Claude compaction remains the existing
persisted, default-on setting from PR #421.

## Infrastructure names

Only KEEP-ENV names are listed below. Platform/SDK inputs retain their original
host semantics and do not imply a credential import.

| Name | Deployment purpose |
| --- | --- |
| `ACCESS_LOG_SLOW_MS` | Threshold for logging slow HTTP requests |
| `ACCOUNT_BROWSER_ORIGIN` | Browser origin for the optional account service |
| `ADMIN_PASSWORD` | Legacy encrypted-credential adoption key, not a current human login password |
| `AGENT_ISOLATION` | Container service/agent UID split policy: off, on, required |
| `AGENT_ISOLATION_EXTRA_DIRS` | Extra host-mounted roots managed by the isolation ownership policy |
| `AGENT_ISOLATION_SERVICE_GID` | Service-account GID distinct from agent GID |
| `AGENT_ISOLATION_SERVICE_UID` | Service-account UID distinct from agent UID |
| `AIHUB_DEBUG_STREAM` | Legacy alternate streaming-frame trace switch |
| `AI_SHUFFLE_DEV_API` | Backend URL targeted by frontend Vite development proxy |
| `AI_SHUFFLE_ENV_FILE` | Point a child/worktree at one canonical env file without copying secrets |
| `AI_SHUFFLE_HOOK_BASH` | Select the Windows executable that interprets hook commands |
| `AI_SHUFFLE_PREVIEW` | Identify an isolated branch-preview process with restricted owner access |
| `ALLOW_PROCESS_INTROSPECTION` | Permit core dumps and same-UID process inspection instead of hardening the worker |
| `ALL_PROXY` | Fallback outbound proxy |
| `all_proxy` | Lowercase POSIX alias for ALL_PROXY outbound proxy policy |
| `ANDROID_HOME` | Locate existing Android SDK |
| `ANDROID_SDK_ROOT` | Locate existing Android SDK (legacy alias) |
| `API_DOCS_ENABLED` | Expose unauthenticated interactive API schema endpoints |
| `APPDATA` | Windows roaming application-data root |
| `APPIMAGE` | Path of the running Linux AppImage used for installed-host updates |
| `APP_VERSION` | Release version stamped into the product |
| `AWS_CA_BUNDLE` | aws ca bundle input to the selected Claude cloud credential/endpoint chain |
| `AWS_CONFIG_FILE` | aws config file input to the selected Claude cloud credential/endpoint chain |
| `AWS_CONTAINER_AUTHORIZATION_TOKEN` | aws container authorization token input to the selected Claude cloud credential/endpoint chain |
| `AWS_CONTAINER_AUTHORIZATION_TOKEN_FILE` | aws container authorization token file input to the selected Claude cloud credential/endpoint chain |
| `AWS_CONTAINER_CREDENTIALS_FULL_URI` | aws container credentials full uri input to the selected Claude cloud credential/endpoint chain |
| `AWS_CONTAINER_CREDENTIALS_RELATIVE_URI` | aws container credentials relative uri input to the selected Claude cloud credential/endpoint chain |
| `AWS_DEFAULT_REGION` | aws default region input to the selected Claude cloud credential/endpoint chain |
| `AWS_EC2_METADATA_DISABLED` | aws ec2 metadata disabled input to the selected Claude cloud credential/endpoint chain |
| `AWS_ENDPOINT_URL` | aws endpoint url input to the selected Claude cloud credential/endpoint chain |
| `AWS_ENDPOINT_URL_BEDROCK_RUNTIME` | aws endpoint url bedrock runtime input to the selected Claude cloud credential/endpoint chain |
| `AWS_PROFILE` | aws profile input to the selected Claude cloud credential/endpoint chain |
| `AWS_ROLE_ARN` | aws role arn input to the selected Claude cloud credential/endpoint chain |
| `AWS_ROLE_SESSION_NAME` | aws role session name input to the selected Claude cloud credential/endpoint chain |
| `AWS_SDK_LOAD_CONFIG` | aws sdk load config input to the selected Claude cloud credential/endpoint chain |
| `AWS_SHARED_CREDENTIALS_FILE` | aws shared credentials file input to the selected Claude cloud credential/endpoint chain |
| `AWS_WEB_IDENTITY_TOKEN_FILE` | aws web identity token file input to the selected Claude cloud credential/endpoint chain |
| `AZURE_ADDITIONALLY_ALLOWED_TENANTS` | azure additionally allowed tenants input to the selected Claude cloud credential/endpoint chain |
| `AZURE_AUTHORITY_HOST` | azure authority host input to the selected Claude cloud credential/endpoint chain |
| `AZURE_CLIENT_CERTIFICATE_PASSWORD` | azure client certificate password input to the selected Claude cloud credential/endpoint chain |
| `AZURE_CLIENT_CERTIFICATE_PATH` | azure client certificate path input to the selected Claude cloud credential/endpoint chain |
| `AZURE_CLIENT_ID` | azure client id input to the selected Claude cloud credential/endpoint chain |
| `AZURE_CLIENT_SECRET` | azure client secret input to the selected Claude cloud credential/endpoint chain |
| `AZURE_CLIENT_SEND_CERTIFICATE_CHAIN` | azure client send certificate chain input to the selected Claude cloud credential/endpoint chain |
| `AZURE_CONFIG_DIR` | azure config dir input to the selected Claude cloud credential/endpoint chain |
| `AZURE_FEDERATED_TOKEN_FILE` | azure federated token file input to the selected Claude cloud credential/endpoint chain |
| `AZURE_PASSWORD` | azure password input to the selected Claude cloud credential/endpoint chain |
| `AZURE_TENANT_ID` | azure tenant id input to the selected Claude cloud credential/endpoint chain |
| `AZURE_TOKEN_CREDENTIALS` | azure token credentials input to the selected Claude cloud credential/endpoint chain |
| `AZURE_USERNAME` | azure username input to the selected Claude cloud credential/endpoint chain |
| `BIND_HOST` | Host interface on which Compose publishes the worker |
| `BROWSER_EGRESS_ENTRY` | Location of the mandatory browser egress-proxy launcher |
| `BROWSER_LOOPBACK_HOST` | Rewrite loopback URLs to the reachable host of a remote browser |
| `BROWSER_MCP_ENTRY` | Override browser MCP executable/module location |
| `BROWSER_SSRF_ALLOW` | Explicit CIDR destinations allowed by browser egress policy |
| `BROWSER_SSRF_ALLOW_LOOPBACK` | Override automatic loopback accessibility in browser network policy |
| `BROWSER_SSRF_BLOCK` | Extra CIDR destinations blocked by browser egress policy |
| `BROWSER_SSRF_DENY_PORTS` | Deny browser access to worker-internal ports |
| `BROWSER_UPLOAD_DIR` | Allowed root for agent-uploaded files |
| `CHILD_PROCESS_NICE` | Lower scheduling priority of tool children |
| `CHROME_CDP` | Open Chrome’s loopback automation port |
| `CHROME_CDP_PORT` | Loopback browser automation attachment port |
| `CHROME_EXECUTABLE` | Browser executable on this host |
| `CLAUDE_CODE_BUBBLEWRAP` | Declare an intentional host sandbox for root CLI permission compatibility |
| `CLAUDE_CODE_CLIENT_CERT` | Client TLS certificate for Claude installer/proxy |
| `CLAUDE_CODE_CLIENT_KEY` | Client TLS private key path for Claude installer/proxy |
| `CLAUDE_CODE_CLIENT_KEY_PASSPHRASE` | Client TLS key passphrase for Claude installer/proxy |
| `CLAUDE_CODE_GIT_BASH_PATH` | Compatibility Git Bash executable override |
| `CLAUDE_CONFIG_DIR` | Stock Claude CLI configuration/transcript home |
| `CLAUDE_IMPORT_SOURCE_HOME` | Legacy alias for default provider-settings import source |
| `CLAUDE_PROJECTS_DIR` | Legacy Claude transcript root |
| `CLOUDSDK_CONFIG` | cloudsdk config input to the selected Claude cloud credential/endpoint chain |
| `CLUSTER_ADVERTISE_URL` | Peer-reachable install URL for legacy clustering |
| `CLUSTER_SECRET` | Legacy shared-database cluster bearer secret; generated and persisted if absent |
| `CODEX_APP_SERVER_LOGIN_CLIENT_ID` | OAuth application identity for Codex/ChatGPT sign-in |
| `CODE_MODE_OS_SANDBOX` | OS-level code-kernel isolation: auto, required or off |
| `COMSPEC` | Windows command interpreter fallback |
| `COOKIE_SECURE` | Mark cookies HTTPS-only |
| `CORS_ORIGINS` | Additional permitted browser origins |
| `CURL_CA_BUNDLE` | curl CA certificate bundle |
| `DATA_DIR` | Persistent app data, keys and managed database root |
| `DBUS_SESSION_BUS_ADDRESS` | Session DBus address used by Chromium |
| `DBUS_SYSTEM_BUS_ADDRESS` | System DBus address used by Chromium |
| `DISPLAY` | X11 display address for host computer-use tools |
| `FILE_LOG_BACKUP_COUNT` | Retain this many rotated log files |
| `FILE_LOG_LEVEL` | Minimum severity persisted to log files |
| `FILE_LOG_MAX_BYTES` | Rotate each log file at this byte size |
| `FIREBASE_CREDENTIALS` | Path to Firebase service-account credentials for push delivery |
| `FLUID_CHILD_ENV_PASSTHROUGH` | Names of additional non-secret host variables allowed into project children |
| `FLUID_CWD` | Working directory for standalone Fluid sessions |
| `FLUID_GUIDANCE_HOME` | Alternate home containing harness guidance files |
| `FLUID_PROJECTS_DIR` | Native per-project memory/session root |
| `FLUID_SHELL` | Explicit Bash executable for Fluid shell commands |
| `FLUID_SKILLS_DIR` | Directory for saved Fluid skills and episodic journal |
| `FLUID_TRACE_REQUESTS` | File path for raw provider-request diagnostic tracing |
| `FORCE_DB_MIGRATIONS` | Force the managed database migration path during maintenance |
| `GCLOUD_PROJECT` | gcloud project input to the selected Claude cloud credential/endpoint chain |
| `GENERATED_AUDIO_DIR` | Output root for generated audio |
| `GENERATED_IMAGES_DIR` | Output root for generated images |
| `GENERATED_MODELS_DIR` | Output root for generated 3D models |
| `GENERATED_VIDEOS_DIR` | Output root for generated videos |
| `GH_CONFIG_DIR` | GitHub CLI authentication/configuration root |
| `GH_TOKEN` | Inherited GitHub CLI credential |
| `GITHUB_TOKEN` | Alternate inherited GitHub CLI credential |
| `GIT_CEILING_DIRECTORIES` | Stop Git root discovery at these directories |
| `GIT_DISCOVERY_ACROSS_FILESYSTEM` | Allow repository discovery across mount/filesystem boundaries |
| `GOOGLE_CLOUD_PROJECT` | google cloud project input to the selected Claude cloud credential/endpoint chain |
| `GOOGLE_CLOUD_QUOTA_PROJECT` | google cloud quota project input to the selected Claude cloud credential/endpoint chain |
| `GOOGLE_EXTERNAL_ACCOUNT_ALLOW_EXECUTABLES` | google external account allow executables input to the selected Claude cloud credential/endpoint chain |
| `HOME` | Host user home; determines credentials, profiles and default paths |
| `HOMEDRIVE` | Windows home drive |
| `HOMEPATH` | Windows home path |
| `HOST` | Address the HTTP listener binds to |
| `HTTPS_PROXY` | HTTPS outbound proxy |
| `https_proxy` | Lowercase POSIX alias for HTTPS_PROXY outbound proxy policy |
| `HTTP_PROXY` | HTTP outbound proxy |
| `http_proxy` | Lowercase POSIX alias for HTTP_PROXY outbound proxy policy |
| `IDENTITY_ENDPOINT` | identity endpoint input to the selected Claude cloud credential/endpoint chain |
| `IDENTITY_HEADER` | identity header input to the selected Claude cloud credential/endpoint chain |
| `IDENTITY_SERVER_THUMBPRINT` | identity server thumbprint input to the selected Claude cloud credential/endpoint chain |
| `IMDS_ENDPOINT` | imds endpoint input to the selected Claude cloud credential/endpoint chain |
| `INSTANCE_ID` | Explicit legacy install identity; otherwise persist a generated ID |
| `IS_SANDBOX` | Declare an intentional host sandbox for root CLI permission compatibility |
| `JQ_SHIM_PYTHON` | Interpreter for packaged jq compatibility shim |
| `LANG` | Default process locale |
| `LANGUAGE` | Preferred message languages |
| `LC_ALL` | Process locale override (app forces C for machine-readable commands) |
| `LC_CTYPE` | Character classification/encoding locale |
| `LD_LIBRARY_PATH` | Native library lookup path preserved for application update children |
| `LOCALAPPDATA` | Windows local application-data root |
| `LOCAL_MODE` | Force local/container path and runtime detection |
| `LOGNAME` | Host login name |
| `LOG_LEVEL` | Console and in-memory log verbosity |
| `LOOP_STALL_THRESHOLD_S` | Start reporting event-loop stalls after this duration |
| `MALLOC_ARENA_MAX` | Limit allocator arenas for worker memory footprint |
| `MAX_REQUEST_BODY_MB` | Reject request bodies larger than this limit |
| `MOBILE_ACCOUNT_HUB_URL` | Endpoint of the optional account service |
| `MSI_ENDPOINT` | msi endpoint input to the selected Claude cloud credential/endpoint chain |
| `MSI_SECRET` | msi secret input to the selected Claude cloud credential/endpoint chain |
| `NODE_EXTRA_CA_CERTS` | Additional Node TLS trust bundle |
| `NO_PROXY` | Proxy bypass host patterns |
| `no_proxy` | Lowercase POSIX alias for NO_PROXY outbound proxy policy |
| `NPM_CONFIG_CA` | Installer npm ca input |
| `npm_config_ca` | Installer npm ca input |
| `NPM_CONFIG_CACHE` | Installer npm cache input |
| `npm_config_cache` | Installer npm cache input |
| `NPM_CONFIG_CAFILE` | Installer npm cafile input |
| `npm_config_cafile` | Installer npm cafile input |
| `NPM_CONFIG_GLOBALCONFIG` | Installer npm globalconfig input |
| `npm_config_globalconfig` | Installer npm globalconfig input |
| `NPM_CONFIG_HTTPS_PROXY` | Installer npm https proxy input |
| `npm_config_https_proxy` | Installer npm https proxy input |
| `NPM_CONFIG_NOPROXY` | Installer npm noproxy input |
| `npm_config_noproxy` | Installer npm noproxy input |
| `NPM_CONFIG_PREFIX` | Installer npm prefix input |
| `npm_config_prefix` | Installer npm prefix input |
| `NPM_CONFIG_PROXY` | Installer npm proxy input |
| `npm_config_proxy` | Installer npm proxy input |
| `NPM_CONFIG_REGISTRY` | Installer npm registry input |
| `npm_config_registry` | Installer npm registry input |
| `NPM_CONFIG_STRICT_SSL` | Installer npm strict ssl input |
| `npm_config_strict_ssl` | Installer npm strict ssl input |
| `NPM_CONFIG_USERCONFIG` | Installer npm userconfig input |
| `npm_config_userconfig` | Installer npm userconfig input |
| `NUMBER_OF_PROCESSORS` | Host logical processor count |
| `OS` | Windows OS identifier |
| `OSTYPE` | Select Windows compatibility path conversion |
| `OWNER_PASSWORD` | Provision an owner password through the startup bootstrap path |
| `PATH` | Executable search path inherited by subprocesses |
| `PATHEXT` | Windows executable extension search order |
| `PGID` | Container agent Unix group ID |
| `PORT` | HTTP listener port (Compose also uses PORT for the host mapping) |
| `PROCESSOR_ARCHITECTURE` | Host CPU architecture |
| `PROCESSOR_ARCHITEW6432` | Native CPU architecture visible to a 32-bit process |
| `PROGRAMDATA` | Windows machine-wide configuration root |
| `ProgramFiles` | Windows system application-install root |
| `ProgramFiles(x86)` | Windows 32-bit application-install root |
| `PSMODULEPATH` | PowerShell module search path |
| `PUID` | Container agent Unix user ID |
| `PYTHONPATH` | Python import path used for internal bundled-MCP subprocesses |
| `SDK_ADMISSION_MIN_AVAILABLE_MB` | Hold back new session admission when host available memory is low |
| `SDK_ADMISSION_RSS_MB` | Refuse new session admission above this tree RSS; 0 disables |
| `SDK_MAX_CHILD_PROCESSES` | Refuse new session admission above this host child-process budget; 0 disables |
| `SDK_NATIVE_SESSION_SOFT_CAP` | Early refusal cap for native sessions; 0 disables cap |
| `SDK_SESSION_POOL_SIZE` | Warm delegated-session pool capacity |
| `SDK_SUBPROCESS_OOM_SCORE_ADJ` | Make recoverable agent children preferable Linux OOM victims |
| `SERVICE_NAME` | Service label and fallback identity namespace |
| `SETTINGS_IMPORT_ALLOWED_HOMES` | Explicit external homes permitted for safe provider import |
| `SETTINGS_IMPORT_DESTINATION_HOME` | Destination home for active imported Fluid settings |
| `SETTINGS_IMPORT_SOURCE_HOME` | Default home to preview external provider settings from |
| `SHELL` | POSIX user shell fallback |
| `SHUFFLE_DESKTOP_DEV_URL` | Development-only frontend URL loaded by Electron |
| `SHUFFLE_DESKTOP_POSTGRES_DIR` | Development-only assembled PostgreSQL distribution root |
| `SHUFFLE_DESKTOP_REPO_DIR` | Development-only worker source checkout override |
| `SHUFFLE_DESKTOP_USER_DATA` | Development-only isolated Electron profile directory |
| `SHUFFLE_DESKTOP_WORKER_DIR` | Development-only assembled worker bundle root |
| `SHUFFLE_LOCAL_ACCESS` | Allow owner access from the configured local transport |
| `SHUFFLE_LOCAL_HOSTNAMES` | Hostnames accepted by local-owner access policy |
| `SHUFFLE_PREVIEW_HOST` | Bind address of the token-only preview listener; defaults to the local listener address, independent of owner/protected binds |
| `SHUFFLE_PREVIEW_PORT` | Port for isolated branch preview (also used by desktop preview tooling) |
| `SHUFFLE_PREVIEW_PROTECTED_PORT` | Private listener port of the isolated desktop preview |
| `SHUFFLE_PREVIEW_PYTHON` | Select development-preview interpreter |
| `SHUFFLE_PREVIEW_ROOT` | Locate isolated development-preview profile |
| `SHUFFLE_SOURCE_REPO` | Locate source repository for deployment-status inspection |
| `SSL_CERT_DIR` | TLS CA certificate directory |
| `SSL_CERT_FILE` | TLS CA certificate file |
| `STREAM_DEBUG` | Enable verbose streaming-frame ordering logs |
| `SystemRoot` | Windows installation root for PowerShell discovery |
| `TEMP` | Windows temporary-directory root |
| `TERM` | Terminal type |
| `TMP` | Alternate temporary directory |
| `TMPDIR` | Temporary directory for jq launcher |
| `TOOLS_DIR` | Location of the bundled Node AI tools |
| `TRUSTED_PROXIES` | Proxy IP/CIDR allowlist whose forwarding headers are trusted |
| `TRUSTED_PROXY_HEADERS` | Header names used to recover a proxied client address |
| `USER` | Host username used in container-mode detection |
| `USERNAME` | Windows account name used by Codex credential storage |
| `USERPROFILE` | Windows user home directory |
| `VERSION` | Legacy alias for the product release version |
| `WAYLAND_DISPLAY` | Wayland display socket for host computer-use tools |
| `WINDIR` | Windows installation directory |
| `WORKSPACE_DIR` | Default project workspace; Compose value also selects the host bind mount |
| `WS_RATE_LIMIT_MESSAGES` | WebSocket per-connection message budget |
| `WS_RATE_LIMIT_WINDOW_SECONDS` | Window for the WebSocket message budget |
| `XAI_OAUTH_CLIENT_ID` | OAuth application identity for xAI sign-in |
| `XAUTHORITY` | X11 authentication cookie-file path |
| `XDG_CACHE_HOME` | User cache directory |
| `XDG_CONFIG_HOME` | Linux user configuration root |
| `XDG_DATA_HOME` | Linux user data root |
| `XDG_RUNTIME_DIR` | Desktop session runtime sockets directory |
| `XDG_SESSION_TYPE` | Detect whether host desktop is Wayland or X11 |
| `XDG_STATE_HOME` | User persistent state directory |
