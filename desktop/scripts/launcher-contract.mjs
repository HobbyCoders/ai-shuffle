// desktop/launcher.json (#1821 D5): which Windows launcher this source packs
// into app.asar, and the oldest launcher a release built from it still runs
// under.
//
//   version  stamped into an installer's desktop-build.json as `launcher`, and
//            passed by that launcher to the updated app as
//            SHUFFLE_LAUNCHER_VERSION. Raise it whenever a file in the
//            launcher's import closure (scripts/ci/update_reach.py) changes.
//   minimum  signed into every published manifest as desktop.launcher. 0 means
//            any launcher, including those built before the stamp existed.
//
// When a launcher older than `minimum` starts a release, the release replaces
// it from its own verified source (desktop/LAUNCHER.md). Minimum 3 is the
// owner-accepted signing-key bridge; future bumps need Windows acceptance.
export function readLauncher(text) {
 const value = JSON.parse(text);
 const count = n => Number.isSafeInteger(n) && n >= 0;
 if (!value || typeof value !== 'object' || !count(value.version) || value.version < 1 || !count(value.minimum) || value.minimum > value.version) {
  throw Error('desktop/launcher.json must hold {"version": n >= 1, "minimum": 0..version}');
 }
 return { version: value.version, minimum: value.minimum };
}
