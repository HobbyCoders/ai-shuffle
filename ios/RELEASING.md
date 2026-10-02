# Releasing the iOS app (TestFlight)

TestFlight is the iOS counterpart of the Play internal track: testers install
builds without a public App Store release. Nothing in this repository signs or
uploads an iOS build. `ios.yml` only builds for the Simulator. Every step below
that involves Apple accounts, money or credentials is the owner's.

## Owner prerequisites

1. **Apple Developer Program membership, $99 a year.** Required for TestFlight,
   App Store Connect and distribution certificates; a free Apple Account only
   signs for 7 days on up to 3 devices. Decide individual or organization
   first. An organization (HobbyCoders as the seller) needs a D-U-N-S number,
   which is free but can take days or weeks.
2. **Two-factor authentication** on the Account Holder's Apple Account.
3. **App ID.** In Certificates, Identifiers & Profiles, register the explicit
   bundle ID `com.hobbycoders.aishuffle` (the same ID as the Android app).
4. **App Store Connect app record.** Apps > + > New App: platform iOS, name
   AI Shuffle, bundle ID `com.hobbycoders.aishuffle`, any SKU.
5. **App Store Connect API key.** Users and Access > Integrations > App Store
   Connect API, role App Manager. Keep the `.p8` file (it downloads once), the
   Key ID and the Issuer ID. For a future CI upload job, store them as
   secrets of a protected GitHub environment named `testflight-internal`:
   `ASC_KEY_P8`, `ASC_KEY_ID`, `ASC_ISSUER_ID`, plus the Team ID as the
   variable `APPLE_TEAM_ID`. Never paste them into a chat or an issue.
6. **Internal testers.** Add them as App Store Connect users with the Account
   Holder, Admin, App Manager, Developer or Marketing role (up to 100).
7. **A Mac** for the first build and for debugging the WebKit shell. Routine
   builds can move to CI later.

## Before the first upload

- **Worker support.** The app shows the phone UI the paired worker serves at
  `/mobile/` (`app/mobile_ui.py`), so it needs a worker release that serves it.
  Deploy that release, and only then set a repository variable
  `IOS_WORKERS_READY=true`, the same kind of gate as `ANDROID_WORKERS_READY`.
  UI changes after that ship with worker updates; an app build is only for
  native changes (see docs/mobile-desktop-ui-split.md).
- **Toolchain.** Per the design on issue #1617, uploads since April 28, 2026
  must be built with Xcode 26 and the iOS 26 SDK. The deployment target stays
  iOS 16.
- **Export compliance.** The app uses only Apple's HTTPS stack (URLSession and
  WebKit) and the Keychain. If the owner confirms that, add
  `ITSAppUsesNonExemptEncryption: false` to the Info.plist properties in
  `project.yml`; otherwise App Store Connect asks the question for every build.
- **App icon.** `AIShuffle/Assets.xcassets/AppIcon.appiconset/AppIcon.png` is
  the desktop icon (`desktop/build/icon.png`) with its transparent corners
  filled, since App Store icons cannot have an alpha channel. Replace it if a
  dedicated iOS icon exists.

## Build and upload to TestFlight (manual, on a Mac)

Write the API key to a file outside the repository first, for example
`~/.appstoreconnect/private_keys/AuthKey_<KEY_ID>.p8`. Then, from the
repository root:

```sh
cd ios
xcodegen generate

VERSION=0.9.0                       # the release tag without the v
BUILD=$(( $(date +%s) / 60 ))       # must increase for every upload
KEY=~/.appstoreconnect/private_keys/AuthKey_$ASC_KEY_ID.p8

xcodebuild archive \
  -project AIShuffle.xcodeproj -scheme AIShuffle -configuration Release \
  -destination 'generic/platform=iOS' -archivePath build/AIShuffle.xcarchive \
  -allowProvisioningUpdates \
  -authenticationKeyPath "$KEY" -authenticationKeyID "$ASC_KEY_ID" -authenticationKeyIssuerID "$ASC_ISSUER_ID" \
  DEVELOPMENT_TEAM="$APPLE_TEAM_ID" MARKETING_VERSION="$VERSION" CURRENT_PROJECT_VERSION="$BUILD"

xcodebuild -exportArchive \
  -archivePath build/AIShuffle.xcarchive -exportOptionsPlist ExportOptions.plist \
  -exportPath build/export -allowProvisioningUpdates \
  -authenticationKeyPath "$KEY" -authenticationKeyID "$ASC_KEY_ID" -authenticationKeyIssuerID "$ASC_ISSUER_ID"
```

`ExportOptions.plist` sets `destination` to `upload`, so the export step sends
the build to App Store Connect. Automatic signing with the API key creates the
Apple Distribution certificate and App Store provisioning profile on first use.
If the upload step fails, export with `destination` set to `export` and upload
the `.ipa` with Apple's Transporter app instead.

## After the upload

1. In App Store Connect > the app > TestFlight, wait for the build to finish
   processing and answer the export compliance question if asked.
2. Create an internal group, add the internal testers and the build. Internal
   builds do not go through Beta App Review.
3. Testers install the TestFlight app and accept the invitation.
4. Every TestFlight build expires after 90 days, so upload a fresh build at
   least that often.

**External testing** (up to 10,000 testers by email or public link) is
optional. The first build in an external group goes through Beta App Review,
which needs reviewer notes, a way to try the app without the owner's worker
(a demo worker or the offline demo, not ported yet), and a privacy policy URL.

**App Store release** is a separate, later decision: privacy nutrition labels,
in-app account deletion (built for Android, not ported), store listing and
App Review.

## Automating uploads later

A `testflight` job can follow `android-play.yml`: owner-only
`workflow_dispatch` on `main` or a `v*` tag, the `testflight-internal`
environment for the API key, a macOS runner, and the commands above. It is not
part of this change because the account and secrets do not exist yet.
