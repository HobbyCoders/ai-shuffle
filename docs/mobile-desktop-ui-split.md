# Mobile and desktop UI split

Decided 2026-09-07; revised 2026-09-08; delivery changed 2026-10-02 (the
worker serves the phone UI, see "How the apps get the UI"). There are two UIs
and two builds:

- **Web UI (`frontend/`) — desktop only.** This is what a worker serves at
  `/`. Every browser, at any window width, including a phone browser, gets the
  desktop shell. A narrow window only resizes it: the sidebar becomes an
  overlay, drawers clamp to the viewport, rows wrap. It never turns into the
  mobile UI, and it carries no mobile-shell code or CSS at all.
- **Mobile UI (`mobile/`) — the phone apps'.** A separate SvelteKit static
  app, based on the former mobile shell (compact app bar, slide-in drawer,
  collapsing composer with its controls sheet, fullscreen panels, touch-sized
  rows), free to diverge from the web UI over time. Every worker serves it at
  `/mobile/`, beside the web UI, and the Android and iOS apps load it from the
  paired worker.

## Why

The two UIs are separate builds so the phone UI can diverge from the desktop
one without either carrying the other's code. That separation is about
*code*; it says nothing about *where* the phone UI is delivered from.

From 2026-09-07 the Android app bundled its own build of `mobile/`, loaded it
from its asset origin and pinned workers to an API contract. That made every
phone UI change an app release: build, sign, upload to Play internal testing,
wait for testers to update. Most phone UI work is ordinary frontend work in
the shared `$lib`, so in practice almost every UI change needed a Play build,
and the phone lagged the worker until it was installed.

So the phone UI now ships with the worker, like the desktop UI. The app
release is only for what really is native: phone tools, permissions, pairing,
the WebView shell and the native bridges. The trade-off is that the page now
changes with the worker. A worker update can change what the phone shows, and
a page must cope with app builds older or newer than itself. The capability
descriptor below is the contract for that, and the apps' confinement (only the
paired worker's `/mobile/` is a chat document, only that worker may be
reached) is unchanged.

| Change | What ships it |
|---|---|
| Anything under `mobile/` or the shared `frontend/src/lib` | a worker update |
| Phone tools, permissions, pairing, a new or changed native bridge | an app release (Play internal testing / TestFlight) |
| A new native capability the page uses | an app release first, then a worker release whose page checks `nativeCapability()` |

## Where things live

| | `frontend/` (web UI) | `mobile/` (mobile UI) |
|---|---|---|
| Kind | SvelteKit static app served by the worker at `/` | SvelteKit static app served by the worker at `/mobile/` |
| Shell | `src/lib/components/layout/AppLayout.svelte` (desktop) | `src/shell/MobileAppLayout.svelte` (one AppTopBar + drawer) |
| Shared code | `src/lib` | the same `src/lib`, via `kit.files.lib = '../frontend/src/lib'` (`$lib` resolves there) |
| Page | `src/routes/+page.svelte` → shared `AppPage` with the desktop shell | `src/routes/+page.svelte` → the same `AppPage` with the mobile shell |
| Root layout | `src/routes/+layout.svelte` → shared `RootLayout` | `src/routes/+layout.svelte` → `RootLayout` with `mobileApp` |
| `<html data-platform>` | `desktop`, static in `src/app.html` | `mobile`, static in `src/app.html` |
| Mobile CSS | none | `src/mobile.css` (global rules) |
| Mobile-only modules | none | `src/shell/*` (AppTopBar, ConnectedWorkerBar, MobileComposerControlsSheet, mobile-composer-collapse, mobile-panel-gesture) |
| Build flag | `VITE_SHUFFLE_SHELL` unset → desktop | `VITE_SHUFFLE_SHELL=mobile` (set by `vite.config.js`) |
| Dependencies | `node_modules` | linked to `../frontend/node_modules` (`npm run deps`) |

### How the shell is chosen

By build, and by nothing else. `frontend/src/lib/utils/platform.ts` reads
`import.meta.env.VITE_SHUFFLE_SHELL`; `platform.svelte.ts` exposes it as
`platform.isMobile` / `platform.isDesktop` (reactive) and `isMobileUi()` for
plain modules. In the web build this is a constant `desktop`: there is no
user-agent sniffing, no `?ui=` override and no localStorage key. The mobile
app additionally calls `setPlatformForMobileApp()` in its root layout, before
anything renders. The value is fixed for the life of the page; resize
listeners must not change it.

### The seam for shared components

Shared components keep their `platform.isMobile` *behaviour* switches — a
`$derived(platform.isMobile)` that changes what a component does is fine to
share. What they must not do is import mobile-only code. The composer's
mobile behaviour (collapse, swipe-to-open gesture, controls sheet) is reached
through `frontend/src/lib/components/chat/composer-shell.ts`: the web build
gets desktop no-op defaults, and `mobile/src/shell/register.ts` registers the
real implementations at startup. `MainPanel` takes an optional `header`
snippet so the mobile app can put its `AppTopBar` where the web UI renders
`StudioTopBar`. The mobile title bar hosts the session breadcrumbs and navigation.
Chats have a dedicated second action row: transcript download pinned to the left
and the single Git dropdown pinned to the right. Other pages keep just the title
bar. The action row does not use an overflow rail that could clip the Git panel.
Swarm workflow controls, stages, and history live in that dropdown; non-Git chats
get a standalone workflow dropdown instead. The conversation's live task strip
shows only running tasks, not completed/failed receipts or full swarm boards.
At phone widths the Git trigger keeps a short label: the desktop label (or the
+/− numbers), or the branch name for an unreadable checkout, truncated with an
ellipsis to leave room for the chat title. The dimmer detail drops; the
accessible name and the opened panel still expose the full status.
Mobile header and Git-sheet actions use matching 44px touch targets; both halves
of a split action share the same height. Private access opens from a 44px shield
icon in the phone header as a bottom sheet (an anchored panel on desktop), with
one scrolling body below a fixed close/header row. Connection details wrap within the card and
saved host-key fingerprints remain available through a disclosure. Mobile fields
retain their touch size on unfolded screens; desktop cards do not inherit mobile
spacing or platform rules. Authentication and approval behavior are unchanged.

Git writes lock by checkout/branch, not just the viewed chat. Sessions sharing a
worktree or logical branch disable mutations while status/swarm views stay
readable; independent identified branches remain usable. Fresh owner-side leases,
draining turns, and active background jobs also guard local/peer API writes.
Unknown activity fails closed. PR merges check their head/base before writing,
including a fresh check at the queued merge stage.

Git uses a draggable bottom sheet in the mobile build at every width (including
unfolded phones). Narrow or coarse-pointer web screens also use that sheet;
wide fine-pointer web screens retain the anchored popover. Drag the handle down
to dismiss, or tap/activate it; a short/cancelled drag snaps back. The panel
content scrolls normally, independently of the handle.

### Mobile CSS

Svelte scoped styles cannot be split by build, so every rule that used to sit
behind `:global(html[data-platform='mobile'])` in a shared component now
lives in `mobile/src/mobile.css` as a global rule, one section per source
file. The selector prefix there is `html:root[data-platform='mobile']`: the
`:root` adds the one class level the Svelte scoping hash used to add, so each
rule keeps exactly the specificity it had and still wins over the component's
own scoped desktop declarations. Author class names survive Svelte scoping, so
no markup changes were needed to target them. `mobile.css` is imported last by
the mobile page so it lands after the shared component styles in the bundle.

When you add mobile-specific styling to a shared component, add it to
`mobile.css` under that file's section. `frontend/scripts/check-no-mobile-ui.mjs`
(run by `npm run check` and after `npm run build`) fails the web build if
`data-platform='mobile'` reappears under `frontend/src` or in the output.

## How the apps get the UI

1. `mobile/` builds with `paths.base = '/mobile'` and an `index.html`
   fallback (client-only, no service worker, no manifest).
2. The frontend build's `postbuild` runs `frontend/scripts/build-mobile-ui.mjs`,
   which builds `mobile/` into `frontend/build/mobile`. Every install path
   copies `frontend/build` into `app/static` (Docker image, source archive,
   desktop source build), so every worker has `app/static/mobile`. A build
   without it fails unless `SHUFFLE_SKIP_MOBILE_UI=1` is set on purpose.
3. `app/mobile_ui.py` serves it at `/mobile/`: hashed assets from
   `/mobile/_app`, real files as themselves, every other route the entry page,
   and missing file-like paths as 404. `/mobile` redirects to `/mobile/`. The
   page is public like the desktop `index.html`; every API call it makes is
   authenticated. Branch previews (`app/preview.py`) serve the branch's
   `/mobile/` under the same owner check.
4. **Android.** `ChatActivity` loads `MobileUi.indexUrl(base)`
   (`https://<worker>/mobile/`). It injects the runtime with
   `WebViewCompat.addDocumentStartJavaScript` scoped to the worker origin and
   guarded to the top-level `/mobile/` page. It confines the WebView: only the
   worker's `/mobile/` pages may be the document (`MobileUi.isUiUrl`, no dot
   segments), the paired worker may be *called* (fetch / SSE / WebSocket /
   media), and nothing else. Any other worker document (the desktop UI, an API
   response) is refused, and worker files go to the save bridge. Every native
   bridge checks `MobileUi.isUiUrl` on the top-level document.
5. **iOS.** `ChatWebController` loads `MobileUI.indexURL(base:)`. WebKit user
   scripts cannot be scoped to an origin, so the bootstrap checks
   `window.top`, `location.origin` and the `/mobile/` path itself. A content
   rule list (`ChatPolicy.contentRules`) blocks every load except the worker's
   `https://`/`wss://` origin, `data:` and `blob:`.
6. **Errors.** If the worker answers `/mobile/` with an HTTP error, both apps
   show a status instead of a page. A 404 means the worker is from before
   `/mobile/` existed: "This worker is too old to serve the phone UI. Update
   the worker, then refresh chat." A connection failure says the worker could
   not be reached.
7. **Offline demo.** The APK still bundles one copy of the phone UI, only for
   Settings → offline demo, which has no worker.
   `android/scripts/sync-offline-demo.sh` copies `mobile/build` to
   `android/app/src/main/assets/offline-demo/` (git-ignored), and Gradle's
   `checkOfflineDemo` task fails an APK build without it. The demo is served
   at `https://offline-demo.shuffle.invalid/mobile/demo`, so the same
   `/mobile` base works. Instrumentation smoke tests serve this copy as the
   fixture worker's `/mobile/` (`PhoneUiFixture`).

The phone UI never registers a service worker. Inside the apps it also
removes any it finds on the worker origin (`pwa.removeAppServiceWorkers`):
Android builds from before the bundled UI loaded the worker's root page, and
its `/sw.js` (scope `/`) would otherwise sit in front of `/mobile/`.

### Runtime contract (`frontend/src/lib/api/runtime.ts`)

Before any page script runs, the app sets:

```js
window.__SHUFFLE_MOBILE__ = {
  apiOrigin: 'https://worker.example', // paired worker's HTTPS origin
  token: 'shc_…',                      // paired session, sent as Bearer
  version: '0.8.6',                    // app versionName (optional)
  deviceName: 'Pixel 9',               // optional
  sendCookies: false,                  // true only for Cloudflare Access workers
  native: {                            // what this app build offers
    platform: 'android',               // or 'ios'
    bridge: 1,                         // bumped when a capability changes shape
    capabilities: ['file-save', 'file-chooser', 'microphone', 'device-tools', 'worker-switch']
  }
};
```

The page uses the paired session, never the worker's cookie, even though it
is now same-origin with the worker. Every request in `$lib` goes through the
helpers:

- `apiBase()`: `'/api/v1'` on the web UI, `'https://worker/api/v1'` in the app.
- `apiOrigin()` and `apiUrl(path)`.
- `authHeaders()`: `Authorization: Bearer …` in the app.
- `requestCredentials()`: `include` on the web UI; `omit` in the app unless
  `sendCookies` is set.
- `withAuth(init)`: applies both.
- `wsUrl(path, params)` / `wsBaseUrl(path)`: `wss://worker…?token=…` in the app.
- `isBundledMobile()`.

A new token reaches a loaded page through
`window.shuffleMobileSetToken(token)` or a `message` event
`{ type: 'shuffle-mobile-token', token }` from the page's own origin, with no
reload. On the worker:

- `Authorization: Bearer shc_…` is accepted by every `require_auth` route and
  is CSRF-exempt.
- The chat, global and browser WebSockets take `?token=`.
- The origins of app builds from before `/mobile/` stay in the CORS allow-list
  (`app/core/cors.py`): Android's asset origin and iOS's `aishuffle://app`.

### Native capabilities

The page outlives any one app build: a worker update reaches every installed
app version at once. So the page must not assume a native feature exists.
`nativeBridge()` returns the descriptor, and `nativeCapability(name)` is
true only when the app showing the page offers `name`:

| Capability | Android | iOS | What it is |
|---|---|---|---|
| `file-save` | yes | no | the `shuffleFileSave` download bridge (system Save dialog) |
| `file-chooser` | yes | yes | `<input type=file>` opens the system picker |
| `microphone` | yes | yes | `getUserMedia` audio for the page's own voice mode |
| `device-tools` | yes | no | phone tools the worker can call on this device |
| `worker-switch` | yes | no | the app connects to another paired worker when the page navigates to `/mobile/?connect=<origin>` |

Both are false on the web UI and in app builds from before the descriptor.
Those builds bundle their own UI, so they never show a worker page anyway.
The rules for changing the contract:

- Adding a native capability means adding its name to `MobileUi.CAPABILITIES`
  (Android) or `MobileUI.capabilities` (iOS) in the same app release.
- Changing a bridge's shape incompatibly means bumping `BRIDGE_VERSION` /
  `bridgeVersion` on both platforms.
- Names are never renamed or reused.

### Switching workers and the worker bar

The phone UI's worker chip (`mobile/src/shell/ConnectedWorkerBar.svelte`) sits
on the second line of the one app bar (`AppTopBar.svelte`), under the page or
chat title. It names the worker the app is connected to; tapping it opens the
"Connected to" sheet, which lists the owner's other workers.
Tapping one in an Android build with `worker-switch` navigates the page to
`/mobile/?connect=<worker origin>`. The app's WebView client catches that
navigation and never loads it (`ui/WorkerSwitch.kt`):

- The URL must be exactly `/mobile/?connect=<value>` on the current worker,
  with no other parameter and no fragment.
- The value must be a bare HTTPS origin: no path, query, fragment or
  credentials.
- It must equal, after normalisation, the saved origin of a worker this phone
  is already paired with (local pairings and the signed-in account's).

A known worker opens the same way the Workers screen opens it, landing on its
chat list. An account worker this phone holds no key for opens the Workers
screen to authorize it. Anything else shows a short message and the app stays
where it is. The URL never carries credentials, and this path never pairs a
new worker.

The app draws its own bar (back, worker name, menu with Refresh chat and
Workers) until the page says it draws the worker switcher itself. The page
says so over the `shuffleShell` web-message bridge, which is scoped like
`shuffleFileSave` to the top-level phone UI page:

- `{"type":"worker-bar","shown":true}` when the worker chip mounts in the app
  bar, `false` when it goes. The app hides or restores its bar.
- `{"type":"workers"}` from the sheet's "Pair, rename or remove workers"
  button, which opens the app's Workers screen.

A worker UI from before the page drew its own worker switcher sends nothing, so the app keeps
its own bar there. The app also restores its bar when the page fails to load
or chat cannot open, so Refresh chat stays reachable. The page side is in
`mobile/src/shell/native-shell.ts` and `worker-connection.ts`.

Worker-hosted media on Android uses `WorkerAssetLoader` in the native WebView
client. It intercepts approved file/media URLs and supplies the paired Bearer
token through the existing worker HTTP client. Responses stream with
Range/206 headers intact. It does not put tokens in URLs or grant other worker
documents the native bridge. Redirects and credentials stay restricted to the
paired origin, and token or worker changes cancel in-flight transfers.
Ordinary note attachment links and download anchors use the native save
bridge. Only the Cloudflare Access cookie is forwarded when needed, never a
worker session cookie. iOS has no equivalent yet: WKWebView cannot intercept
`https`.

### Release ordering

**Workers first, then the apps.** An app that loads `/mobile/` needs a worker
that serves it; an older worker answers 404 and the app shows "update the
worker".

1. Release and deploy workers that serve `/mobile/`. Check
   `https://<worker>/mobile/` in a browser. It is served if it shows the phone
   UI, or the phone UI's "session expired" screen when the browser is not
   signed in to that worker.
   Already-installed apps keep working throughout: they bundle their own UI
   and still reach the worker through CORS.
2. Only then build the apps. On Android, a versioned release publishes to Play
   internal testing only with the repository variable
   `ANDROID_WORKERS_READY=true`; a manual workflow run needs
   `publish_download=true`. On iOS, `IOS_WORKERS_READY=true` plays the same
   role (`ios/RELEASING.md`). Leave each unset until step 1 has been verified
   on the workers that testers use.
3. After that, phone UI changes need only step 1. An app release is needed
   only for native changes. If a worker change needs a new capability, ship
   the app first and gate the page on `nativeCapability()`.

Building an APK or a Docker image does not update running workers.

## Rules for component code

**Script.** Anything that means "mobile" reads `platform.isMobile`, never
`window.innerWidth`. `isNarrowViewportWidth` in `$lib/utils/viewport` is only
for desktop-shell narrow-window resizing and must not gate mobile-only
behaviour. Mobile-only *modules* go in `mobile/src/shell` and are wired in
through a seam in `$lib` (see above), never imported by shared code.

**Styles.** Shared components carry desktop styles only. Mobile overrides go
in `mobile/src/mobile.css`:

```css
/* mobile/src/mobile.css */
html:root[data-platform='mobile'] .composer { … }
```

Keep the rule bodies as they would be in the component; the prefix supplies
the specificity.

**Smaller width queries stay.** `max-width: 639px`, `560px`, `767px` and
similar are the "minor resizing" the desktop shell keeps in a narrow browser
window. They also apply naturally inside the app because a phone is narrow.
Convert one of them only if its body switches to a different UI (hides the
desktop chrome, shows a mobile-only control) rather than tightening spacing —
and then it belongs in `mobile.css`.

**Input-modality queries stay.** `(pointer: coarse)` and `(hover: none)` are
about the input device, not the shell, and remain valid on touch laptops.

**Slide-out panels and drawers.** On the mobile shell they are fullscreen. On
the desktop shell they are side drawers at every width; clamp them with
`max-width: 100vw` (or `min(<design width>, 100vw)`) instead of switching to
fullscreen at 1080px.

**Tests.** A test of a shared component's mobile *behaviour* calls
`platform.set('mobile')` from `$lib/stores/platform.svelte` (and resets to
`'desktop'`). A test that needs the mobile shell itself (the sheet, the
gesture, mobile CSS) lives in `mobile/src/shell/*.test.ts`, calls
`installMobileShell()` from `./register`, and reads styles from
`src/mobile.css`.

## Sidebar behaviour by shell

| | Desktop, wide | Desktop, narrow (≤1080px) | Mobile |
|---|---|---|---|
| Top bar | StudioTopBar in the main panel | StudioTopBar | AppTopBar (compact bar + drawer) |
| Sidebar | Docked, resizable, collapse persisted | Overlay, starts collapsed, not persisted | Drawer inside AppTopBar |
| Dismiss | Toggle in StudioTopBar | Scrim click or any navigation | Drawer close |

## Running each

```sh
# Web UI (desktop), served by the worker
cd frontend && npm ci && npm run dev            # http://localhost:5173
cd frontend && npm run check && npm test && npm run build   # → build/, plus build/mobile

# Mobile UI, in a desktop browser against a local backend (same /api proxy)
cd mobile && npm run deps && npm run dev        # http://localhost:5173/mobile/
cd mobile && npm run check && npm test && npm run build   # → mobile/build

# A worker running from this checkout serves both: / and /mobile/

# APK (bundles the offline demo copy first)
bash android/scripts/sync-offline-demo.sh
cd android && ./gradlew testDebugUnitTest assembleDebug
```

Manual checks:

- Load the web build in a desktop browser at 390px wide. Confirm the desktop
  shell (StudioTopBar, floating sidebar), not the app bar.
- Open a worker in the Android app and confirm the mobile shell. In DevTools,
  the document should be `https://<worker>/mobile/`, and API calls should go
  to the worker with an `Authorization: Bearer shc_…` header and no cookie.
- Pair the app with a worker from before `/mobile/`. It should show the
  "update the worker" message.
