# Installing AI Shuffle

These files are the buyer-facing install kit. Their paths are stable so the
website can link to them.

| Path | What it is |
| --- | --- |
| [`compose/compose.yaml`](compose/compose.yaml) | Docker Compose file for Linux servers, NAS boxes and desktops. |
| [`compose/env.example`](compose/env.example) | The settings a buyer fills in, copied to `.env`. |
| [`compose/README.md`](compose/README.md) | Install, update, back up and remove with Compose. |

Every file pins the current release. Each release bumps `AI_SHUFFLE_VERSION`
in `compose/env.example` and `compose/compose.yaml` together with
`app/__init__.py`; `tests/core/test_install_files.py`
fails when they disagree. Every release is installed from `compose/` on a clean
runner before its in-app update is published.

## Website copies

The website is maintained in the private `HobbyCoders/hobbycoders-cloud`
repository. After changing install files, refresh its committed source snapshots
and served copies with `AI_SHUFFLE_CHECKOUT=/path/to/ai-shuffle npm run sync-install`
from its `site/` directory (see that repository's README). This app checkout stays
the source of truth; no private Cloud checkout is needed to run app tests.

## Release channels

| Channel | Image tags | In-app update feed | Who uses it |
| --- | --- | --- | --- |
| stable | `X.Y.Z` (and `X.Y`, `X`, `latest` for the newest final release) | `/v1/desktop/manifest` | Every customer install. |
| edge | `edge`, `sha-<commit>` | `/v1/desktop/manifest/edge` | The owner's own workers, with `UPDATE_CHANNEL=edge`. |

A stable in-app update is signed only after the release image passed the
container verification and the clean-runner install, and only for a tag on
`main`. The signed manifest names its channel, version, release notes and a
digest of every source file; the worker checks all of them, and re-checks the
files before every start.

## Update checks

This text is the basis for the privacy policy's update section (decision D22:
checks run on launch and daily, with no consent prompt).

- **What is sent.** A plain `GET https://hobbycoders.com/v1/desktop/manifest`
  with no cookies, no account token, no license key, no device or install ID
  and no information about your projects, sessions or settings. Like any web
  request it reveals your IP address and a generic HTTP client name.
- **When.** About 15 seconds after the app starts, then once every 24 hours,
  and whenever you choose **Check for updates**.
- **What comes back.** A small signed file naming the newest release. Nothing
  is downloaded or installed until you choose **Download update** and then
  **Update and restart**.
- **Where it goes.** The request is served by a Cloudflare Worker on
  hobbycoders.com. The Worker keeps no record of update checks; Cloudflare's
  own request logging applies.
- **Turning it off.** **Settings → General → Allow updates** stops checks,
  downloads and installs on every worker of your account. New images still
  arrive only when you change the version yourself.
