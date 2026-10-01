# Mobile and desktop UI split

Decided 2026-09-07; revised 2026-09-08. There are two UIs and two builds:

- **Web UI (`frontend/`) — desktop only.** This is what a worker serves at
  `/`. Every browser, at any window width, including a phone browser, gets the
  desktop shell. A narrow window only resizes it: the sidebar becomes an
  overlay, drawers clamp to the viewport, rows wrap. It never turns into the
  mobile UI, and it carries no mobile-shell code or CSS at all.
- **Mobile UI (`mobile/`) — the Android app's.** A separate SvelteKit static
  app, based on the former mobile shell (compact app bar, slide-in drawer,
  collapsing composer with its controls sheet, fullscreen panels, touch-sized
  rows), free to diverge from the web UI over time. The app ships its own
  build of it and loads it from its own assets; the app talks to workers only
  through their HTTP/WebSocket APIs.

## Why

A worker on any version must never change what the phone shows. Workers are
self-hosted and upgrade on their own schedule; the phone may be paired with
several of them. If the worker served the phone's UI, every worker upgrade
(or downgrade, or a worker running a fork) would change the app's behaviour
and its security surface under the user's feet, and the app could not be
tested against a fixed UI. So the app bundles the UI and pins the worker to
an API contract instead.

## Where things live

| | `frontend/` (web UI) | `mobile/` (mobile UI) |
|---|---|---|
| Kind | SvelteKit static app served by the worker | SvelteKit static app bundled in the APK |
| Shell | `src/lib/components/layout/AppLayout.svelte` (desktop) | `src/shell/MobileAppLayout.svelte` (one AppTopBar + drawer) |
| Shared code | `src/lib` | the same `src/lib`, via `kit.files.lib = '../frontend/src/lib'` (`$lib` resolves there) |
| Page | `src/routes/+page.svelte` → shared `AppPage` with the desktop shell | `src/routes/+page.svelte` → the same `AppPage` with the mobile shell |
| Root layout | `src/routes/+layout.svelte` → shared `RootLayout` | `src/routes/+layout.svelte` → `RootLayout` with `mobileApp` |
| `<html data-platform>` | `desktop`, static in `src/app.html` | `mobile`, static in `src/app.html` |
| Mobile CSS | none | `src/mobile.css` (global rules) |
| Mobile-only modules | none | `src/shell/*` (AppTopBar, PageHeader, MobileComposerControlsSheet, mobile-composer-collapse, mobile-panel-gesture) |
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
At phone widths the Git trigger is icon-only to leave room for the chat title;
its accessible name and the opened panel still expose the full status.
Mobile header and Git-sheet actions use matching 44px touch targets; both halves
of a split action share the same height. Private-access dialogs use one scrolling
body below a fixed close/header row. Connection details wrap within the card and
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

## How the app gets the UI

1. `mobile/` builds with `paths.base = '/assets/mobile-ui'` and an
   `index.html` fallback (client-only, no service worker, no manifest).
2. `android/scripts/sync-mobile-ui.sh` (also `npm run sync:mobile` in
   `frontend/`) builds it and copies `mobile/build/` to
   `android/app/src/main/assets/mobile-ui/` (git-ignored), plus a
   `version.json` for bug reports.
3. Gradle's `checkMobileUi` task, a `preBuild` dependency, fails any APK
   build without `assets/mobile-ui/index.html`. CI runs the sync script before
   `gradlew` (`.github/workflows/android.yml`).
4. `ChatActivity` serves the bundle through `WebViewAssetLoader` at
   `https://appassets.androidplatform.net/assets/mobile-ui/` (`MobileUi.kt`),
   injects the runtime with `WebViewCompat.addDocumentStartJavaScript` scoped
   to that origin, and confines the WebView: the asset origin may load, the
   paired worker may be *called* (fetch / SSE / WebSocket / media), and nothing
   else — a document from the worker is refused.

### Runtime contract (`frontend/src/lib/api/runtime.ts`)

Before any page script runs, the app sets:

```js
window.__SHUFFLE_MOBILE__ = {
  apiOrigin: 'https://worker.example', // paired worker's HTTPS origin
  token: 'shc_…',                      // paired session, sent as Bearer
  version: '0.8.6',                    // app versionName (optional)
  deviceName: 'Pixel 9',               // optional
  sendCookies: false                   // true only for Cloudflare Access workers
};
```

Every request in `$lib` goes through the helpers: `apiBase()` (`'/api/v1'`
on the web UI, `'https://worker/api/v1'` when bundled), `apiOrigin()`,
`apiUrl(path)`, `authHeaders()` (`Authorization: Bearer …` when bundled),
`requestCredentials()` (`include` on the web UI, `omit` when bundled unless
`sendCookies`), `withAuth(init)` (applies both), `wsUrl(path, params)` /
`wsBaseUrl(path)` (`wss://worker…?token=…` when bundled) and
`isBundledMobile()`. A new token reaches a loaded page through
`window.shuffleMobileSetToken(token)` or a `message` event
`{ type: 'shuffle-mobile-token', token }` from the page's own origin — no
reload. On the worker, `Authorization: Bearer shc_…` is accepted by every
`require_auth` route, is CSRF-exempt, and the app origin is always in the CORS
allow-list (`app/core/cors.py`); the chat / global / browser WebSockets take
`?token=`.

Worker-hosted media uses `WorkerAssetLoader` in the native WebView client.
It intercepts approved file/media URLs (including legacy root-relative `/api/`
URLs at the bundle origin), supplies the paired Bearer token through the existing
worker HTTP client, and streams responses with Range/206 headers intact. It does
not put tokens in URLs or grant worker documents the native bridge. Redirects and
credentials remain restricted to the paired origin; token/worker changes cancel
in-flight transfers. Ordinary note attachment links, as well as download anchors,
use the native save bridge. Only the Cloudflare Access cookie is forwarded when
needed, not a worker session cookie.

### Release ordering

Merging no longer publishes an APK automatically. First deploy compatible workers
(the bundle origin must be allowed by CORS) and the account hub migrations/API,
then verify pairing, API requests, inline media and attachment downloads. A manual
Android workflow may publish only with `publish_download=true`. Versioned releases
also require the repository variable `ANDROID_WORKERS_READY=true`; leave it unset
until that rollout has been verified. Building an APK or Docker image does not
update running workers.

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
cd frontend && npm run check && npm test && npm run build

# Mobile UI, in a desktop browser against a local backend (same /api proxy)
cd mobile && npm run deps && npm run dev        # http://localhost:5173/assets/mobile-ui/
cd mobile && npm run check && npm test && npm run build   # → mobile/build

# Bundle it into the app, then build the APK
bash android/scripts/sync-mobile-ui.sh          # or: cd frontend && npm run sync:mobile
cd android && ./gradlew testDebugUnitTest assembleDebug
```

Manual: load the web build in a desktop browser at 390px wide and confirm the
desktop shell (StudioTopBar, floating sidebar), not the app bar; open the
Android app and confirm the mobile shell, that DevTools shows the document at
`https://appassets.androidplatform.net/assets/mobile-ui/`, and that API calls
go to the worker with an `Authorization: Bearer shc_…` header and no cookie.
