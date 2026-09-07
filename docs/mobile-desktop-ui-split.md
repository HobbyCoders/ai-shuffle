# Mobile and desktop UI split

Decided 2026-09-07. The web frontend renders one of two shells, chosen by
platform, never by window width.

- **Desktop shell**: every web browser, at any width, including a phone
  browser. A narrow window only resizes it: the sidebar becomes an overlay,
  drawers clamp to the viewport, rows wrap. It never turns into the mobile UI.
- **Mobile shell**: the Android app's embedded WebView only. This is the UI
  that used to appear under 1080px: the compact app bar with the slide-in
  drawer, the collapsing composer with its controls sheet, fullscreen panels,
  touch-sized rows.

## How the shell is chosen

1. `ChatActivity` appends `AIShuffleAndroid/<versionName>` to the WebView user
   agent (`android/.../ui/WebUserAgent.kt`).
2. An inline script in `frontend/src/app.html` runs before first paint and
   stamps `<html data-platform="mobile">` or `"desktop"`. It also honours a
   `?ui=mobile` / `?ui=desktop` override, persisted in localStorage under
   `ai-shuffle-ui-platform`; `?ui=auto` clears it. Use the override to work on
   the mobile shell in a desktop browser.
3. `frontend/src/lib/utils/platform.ts` holds the same detection as a pure,
   tested function. `frontend/src/lib/stores/platform.svelte.ts` exposes it to
   components as `platform.isMobile` / `platform.isDesktop` (reactive) and
   `isMobileUi()` for plain modules.

The value is fixed for the life of the page. Resize listeners must not change it.

## Rules for component code

**Script.** Anything that used to read `isPanelFullscreenWidth(window.innerWidth)`
or its own `window.innerWidth <= 1080` check to mean "mobile" now reads
`platform.isMobile`. Drop the resize listeners that existed only to recompute
that. `isPanelFullscreenWidth` no longer exists; `isNarrowViewportWidth` in
`$lib/utils/viewport` is only for desktop-shell narrow-window resizing and must
not gate mobile-only behaviour.

**Styles.** Svelte scoped styles cannot express the platform as a media query,
so the 1080px queries become attribute selectors on `<html>`:

```css
/* before */
@media (max-width: 1080px) { .composer { … } }
@media (min-width: 1081px) { .dock { … } }

/* after */
:global(html[data-platform='mobile']) .composer { … }
:global(html[data-platform='desktop']) .dock { … }
```

Inside a `:global { … }` block or a plain `.css` file, write
`html[data-platform='mobile'] .composer`. Keep the rule bodies unchanged; the
specificity bump from the attribute selector is intended, since these rules
already sat at the end of each style block to win.

**Smaller width queries stay.** `max-width: 639px`, `560px`, `767px` and
similar are the "minor resizing" the desktop shell keeps in a narrow browser
window. They also apply naturally inside the app because a phone is narrow.
Convert one of them only if its body switches to a different UI (hides the
desktop chrome, shows a mobile-only control) rather than tightening spacing.

**Input-modality queries stay.** `(pointer: coarse)` and `(hover: none)` are
about the input device, not the shell, and remain valid on touch laptops.

**Slide-out panels and drawers.** On the mobile shell they are fullscreen, as
before. On the desktop shell they are side drawers at every width; clamp them
with `max-width: 100vw` (or `min(<design width>, 100vw)`) instead of switching
to fullscreen at 1080px.

**Tests.** A test that set `window.innerWidth` to exercise mobile behaviour
now calls `platform.set('mobile')` from `$lib/stores/platform.svelte` (and
resets to `'desktop'` afterwards). Source-scraping tests that looked for
`@media (max-width: 1080px)` look for `html[data-platform='mobile']` instead.

## Sidebar behaviour by shell

| | Desktop, wide | Desktop, narrow (≤1080px) | Mobile |
|---|---|---|---|
| Top bar | StudioTopBar in the main panel | StudioTopBar | AppTopBar (compact bar + drawer) |
| Sidebar | Docked, resizable, collapse persisted | Overlay, starts collapsed, not persisted | Drawer inside AppTopBar |
| Dismiss | Toggle in StudioTopBar | Scrim click or any navigation | Drawer close |

## Verification

```sh
cd frontend && npm run check && npm test && npm run build
cd android && ./gradlew testDebugUnitTest
```

Manual: load the web build in a desktop browser at 390px wide and confirm the
desktop shell (StudioTopBar, floating sidebar) rather than the app bar; load
`/?ui=mobile` and confirm the mobile shell at any width; on the Android app,
confirm the mobile shell and that `navigator.userAgent` ends with the token.
