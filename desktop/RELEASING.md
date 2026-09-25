# Releasing the desktop app

This is the owner's checklist for building, signing and shipping AI Shuffle
Desktop. Every credential below is a GitHub Actions **repository secret**
(Settings > Secrets and variables > Actions > New repository secret, or
`gh secret set NAME`). No secret is ever committed, printed or written to
`$GITHUB_ENV`. `scripts/signing.mjs` checks which secrets are set and picks the
signing method.

With no signing secrets at all, every workflow builds unsigned installers as
before, so forks and pull requests keep working. A method that is only partly
configured (for example an Azure client ID without its secret) fails the build
instead of shipping unsigned by accident. Once signing works, set the
repository **variable** `DESKTOP_REQUIRE_SIGNING=true` so an unsigned Windows
or macOS release can never be built again.

Production/signing jobs require the repository owner’s immutable GitHub actor
ID and a matching rerun actor on a permitted main/release-tag ref. A collaborator
cannot reuse an owner-triggered run to publish. Unsigned PR verification remains
automatic; a merge alone does not deploy or publish an edge update. These
workflow gates plus environment ref restrictions are the owner-approved GitHub
Team design, not Enterprise required-reviewer protection. See
`docs/ci/release-channels.md` for the exact trigger/ref and secret-custody limits.

CI's artifact-policy checks reject duplicate YAML keys across every workflow,
including nested job and step mappings. Keep the Windows verification job's
`EXPECT_SIGNED` and `RELEASE_TAG` together in one `env` mapping: a duplicate
mapping is rejected by GitHub before jobs start, even if a permissive local
YAML parser keeps the last value. The regression also asserts both inputs remain
wired into that job.

## Which workflow builds what

| Workflow | Trigger | Output | Signed |
| --- | --- | --- | --- |
| `desktop.yml` | pull requests, manual | typecheck, tests, a Windows preview installer kept for one day | never; pull requests never see signing secrets |
| `desktop-installer.yml` | `release.yml` with `build_desktop`, or manual with a `vX.Y.Z` tag | the stable Windows x64 NSIS installer, attached to the GitHub release and served by the hub at `/desktop` | Authenticode when the Windows secrets are set |
| `desktop-macos-artifact.yml` | manual, `build_macos` ticked | Apple Silicon DMG and ZIP, `SHA256SUMS`, provenance, as a one-day Actions artifact | Developer ID and notarized when the macOS secrets are set |
| `desktop-linux-artifact.yml` | manual | x64 AppImage and deb, `SHA256SUMS`, provenance, as a one-day Actions artifact | no, by design |
| `desktop-source.yml` | owner dispatch from `main` | the Ed25519-signed source manifest that installed Windows apps update from | always, with `DESKTOP_APPLICATION_SIGNING_KEY` |

The macOS and Linux artifacts are downloaded from the run and attached to the
release by hand for now; the hub only serves the Windows installer.

## Windows: Authenticode

Pick one method. Azure Trusted Signing is the recommended one: it is the
simplest to set up and run from CI, costs about USD 10 a month, and needs no
hardware token. Microsoft has been renaming the service to Azure Artifact
Signing; the portal may show either name.

Since June 2023 publicly trusted code-signing certificates must keep their
private key in a hardware security module, so a certificate authority will no
longer hand you an exportable `.pfx` for a new OV or EV certificate. The PFX
method is therefore only for a certificate you already hold as a `.pfx`, or a
self-signed one for testing. Cloud HSM products (DigiCert KeyLocker, SSL.com
eSigner and similar) need their own signing tool and are not wired up here.

### Option A (recommended): Azure Trusted Signing

1. In an Azure subscription, create a **Trusted Signing account** in a region
   near you. Its overview page shows the account **endpoint**, for example
   `https://eus.codesigning.azure.net/`.
2. On the account, start **Identity validation** for Public Trust. Validate
   the business (HobbyCoders LLC) or yourself as an individual developer.
   Microsoft decides eligibility (country, business history); check its current
   requirements before counting on it. Validation can take several days.
3. Create a **Certificate profile** of type Public Trust from that validation.
   Note the profile name and the certificate's subject common name, for example
   `HobbyCoders LLC`. That exact string is the publisher name.
4. In Microsoft Entra ID, create an **App registration** for CI and a **client
   secret** for it. Note the tenant ID, client (application) ID and secret
   value.
5. On the Trusted Signing account, Access control (IAM), give that app the
   **Trusted Signing Certificate Profile Signer** role.

| Secret | Value |
| --- | --- |
| `AZURE_TENANT_ID` | Entra tenant ID |
| `AZURE_CLIENT_ID` | app registration's client ID |
| `AZURE_CLIENT_SECRET` | app registration's client secret value (it expires; set a reminder) |
| `AZURE_TRUSTED_SIGNING_ENDPOINT` | the account endpoint, for example `https://eus.codesigning.azure.net/` |
| `AZURE_TRUSTED_SIGNING_ACCOUNT` | the Trusted Signing account name |
| `AZURE_TRUSTED_SIGNING_PROFILE` | the certificate profile name |
| `AZURE_TRUSTED_SIGNING_PUBLISHER` | the certificate subject's common name, exactly, for example `HobbyCoders LLC` |

electron-builder installs Microsoft's `TrustedSigning` PowerShell module on the
runner and signs the app executable and the installer, timestamped by
Microsoft's timestamp service.

### Option B: a PFX certificate

| Secret | Value |
| --- | --- |
| `WINDOWS_CERTIFICATE_PFX_BASE64` | the `.pfx` file, base64 on one line: PowerShell `[Convert]::ToBase64String([IO.File]::ReadAllBytes('cert.pfx'))` |
| `WINDOWS_CERTIFICATE_PASSWORD` | the `.pfx` password |

Set the secrets of one option only; both at once fails the build.

### What the workflow checks

After building, `desktop-installer.yml` requires `Get-AuthenticodeSignature`
to report `Valid` with a timestamp for both the installer and
`AI Shuffle.exe`, and the verify job checks the downloaded candidate is still
signed. SmartScreen reputation is earned by the certificate over downloads;
the first releases may still show "Windows protected your PC" for a while.

## macOS: Developer ID signing and notarization

1. Join the **Apple Developer Program** (USD 99 a year). Enroll as an
   organization in the LLC's name (Apple asks for a D-U-N-S number) so the
   certificate shows the company, or as an individual.
2. As the **Account Holder**, create a **Developer ID Application**
   certificate (Certificates, Identifiers & Profiles > Certificates > +). A
   "Developer ID Installer" certificate is not needed; the app ships as DMG and
   ZIP, not as a `.pkg`.
3. Install it in Keychain Access on a Mac, then export the certificate with
   its private key as a `.p12` with a password.
4. Pick one notarization credential:
   - **App Store Connect API key (recommended).** App Store Connect > Users
     and Access > Integrations > Team Keys > +, access **Developer**. Download
     the `.p8` (only possible once) and note the Key ID and Issuer ID.
   - **Apple ID and app-specific password.** At account.apple.com, Sign-In
     and Security > App-Specific Passwords, create one for "AI Shuffle
     notarization". Your Team ID is on the developer account's Membership
     page.

| Secret | Value |
| --- | --- |
| `MACOS_CERTIFICATE_P12_BASE64` | the `.p12`, base64 on one line: `base64 -i DeveloperID.p12 \| tr -d '\n'` |
| `MACOS_CERTIFICATE_PASSWORD` | the `.p12` export password |
| `APPLE_API_KEY_P8` | API key option: the full text of `AuthKey_XXXXXXXXXX.p8`, including the BEGIN and END lines |
| `APPLE_API_KEY_ID` | API key option: the Key ID |
| `APPLE_API_ISSUER` | API key option: the Issuer ID |
| `APPLE_ID` | Apple ID option: the Apple ID email |
| `APPLE_APP_SPECIFIC_PASSWORD` | Apple ID option: the app-specific password |
| `APPLE_TEAM_ID` | Apple ID option: the 10-character Team ID |

Set the certificate plus one notarization option. The certificate alone gives
a signed but not notarized app, which Gatekeeper still blocks on other Macs;
notarization secrets without the certificate fail the build.

electron-builder signs every binary in the bundle (Electron, the bundled
Python, Node, PostgreSQL and cloudflared) with the hardened runtime and
`build/entitlements.mac.plist`, submits the app with `notarytool`, and staples
the ticket. The workflow then runs `codesign --verify --deep --strict`,
`xcrun stapler validate` and `spctl --assess` on the app. Notarization can
queue at Apple; the job allows 150 minutes.

The first notarized build is the real test of the entitlements: if the bundled
Python or Node fails to start under the hardened runtime on a clean Mac, the
fix belongs in `build/entitlements.mac.plist`, not in turning the hardened
runtime off.

## Linux: unsigned packages with checksums

The AppImage and deb are not signed. Distribution-level signing (an APT
repository key) is out of scope until there is a repository. Each build writes
`SHA256SUMS` next to the packages and checks it before upload. Publish it with
the packages and tell users to run `sha256sum --check SHA256SUMS` in the
download folder.

## Update checks

- **Windows (installed).** The app checks the hub's signed manifest
  (`https://hobbycoders.com/v1/desktop/manifest`) 15 seconds after launch,
  again whenever its worker becomes ready, and every 24 hours while it runs.
  There is no consent prompt; the privacy policy lists what is sent (the
  request itself: IP address and time, no account or device identifier). The
  manifest is an Ed25519 signature over the revision, sequence, size and
  SHA-256 of the source archive, verified against `build/application-update-key.pem`
  before anything is downloaded; the archive must then match the signed size
  and SHA-256, and a lower or equal sequence is never installed. The account's
  "Allow updates" setting still stops checks and installs. The signing key is
  the `DESKTOP_APPLICATION_SIGNING_KEY` secret used by `desktop-source.yml`.
- **Preview builds** never check for updates.
- **macOS and Linux**: the hub now lists a signed installer per platform at
  `https://hobbycoders.com/v1/desktop/installers` (see
  `android/hub/README.md`, "Installers per platform") and serves the bytes at
  `/v1/desktop/installer/mac` and `/v1/desktop/installer/linux`. The app does
  not read it yet: the desktop updater still has to check it on launch and
  every 24 hours (D22), verify `signature` over `payload` with
  `build/application-update-key.pem`, compare the signed version, and point
  the user at the download, checking its size and SHA-256.

## Release steps

1. Tag `vX.Y.Z` and run `release.yml` with `build_desktop` ticked (or run
   `desktop-installer.yml` against the existing tag). Check the "Choose code
   signing" step says `azure-trusted-signing` or `windows-pfx`.
2. Run `desktop-macos-artifact.yml` with the same version. Check the step says
   `developer-id+notary-api-key` or `developer-id+notary-apple-id` and that the
   Gatekeeper step passed.
3. Run `desktop-linux-artifact.yml` with the same version.
   Run both builds with **Use workflow from** set to the tag, so the artifact
   name carries the tag's commit.
4. Within a day (the artifacts expire), run `platform-installer.yml`
   from the tag once per platform (`mac`, `linux`) with that build's run ID. It
   attaches the DMG, ZIP, AppImage, deb, provenance and `SHA256SUMS-macos` /
   `SHA256SUMS-linux` to the release, then signs the DMG or AppImage entry and
   points `/v1/desktop/installers` at it. It refuses a Mac build that is not
   notarized, an artifact built from another commit or version, and a
   version older than the one already published. The same workflow publishes
   the Android APK (platform `android`); see `android/RELEASING.md`.
5. Install each on a clean machine. Windows: the installer's Properties >
   Digital Signatures tab shows the publisher. macOS: it opens without
   `xattr` or right-click > Open.
