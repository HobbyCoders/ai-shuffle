# AI Shuffle mobile UI

The mobile UI the Android app bundles. A SvelteKit static app that shares
`$lib` with `../frontend` (`kit.files.lib = '../frontend/src/lib'`) and adds
the mobile shell on top: `src/shell/` (AppTopBar, PageHeader, the composer's
collapse / gesture / controls sheet) and `src/mobile.css`.

It never comes from a worker. The app serves it from its own assets at
`https://appassets.androidplatform.net/assets/mobile-ui/` and talks to the
paired worker only through its HTTP/WebSocket APIs
(`frontend/src/lib/api/runtime.ts`). See `docs/mobile-desktop-ui-split.md`.

```sh
npm run deps     # links node_modules -> ../frontend/node_modules (run `npm ci` there first)
npm run dev      # http://localhost:5173/assets/mobile-ui/ (same /api proxy as the web UI)
npm run check
npm test
npm run build    # -> build/ ; android/scripts/sync-mobile-ui.sh copies it into the APK
```
