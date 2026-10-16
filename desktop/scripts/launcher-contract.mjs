// desktop/launcher.json (#1821 D5): which Windows launcher this source packs
// into app.asar, and the oldest launcher a release built from it still runs
// under.
//
//   version  stamped into an installer's desktop-build.json as `launcher`, and
//            passed by that launcher to the updated app as
//            SHUFFLE_LAUNCHER_VERSION. Raise it whenever a file in the
//            launcher's import closure (scripts/ci/update_reach.py) changes;
//            that check fails a range that does not (launcher-version).
//   minimum  signed into every published manifest as desktop.launcher. 0 means
//            any launcher, including those built before the stamp existed.
//   start    signed as desktop.start: the oldest launcher that can START a
//            release built from this source, before any launcher replacement
//            (which only happens after the first healthy start). Updaters
//            refuse such a release before downloading it when their launcher
//            is older, and say a newer installer is needed. Optional; 0..version.
//            Start 3: launcher 3 (0.9.6, 1.0.x) starts these builds through the
//            update's own late restore point; older launchers cannot verify them.
//
// When a launcher older than `minimum` starts a release, the release replaces
// it from its own verified source (desktop/LAUNCHER.md). Minimum 3 is the
// owner-accepted signing-key bridge. Minimum 4 replaces launchers without
// database restore points (0.9.6) and the 1.0.x ones that report 3; every
// bump needs a Windows acceptance run. Version 5 adds the start-floor check
// and staged-build replacement to the launcher's own updater (#97, #98);
// minimum stays 4, so launcher-4 installs are not replaced for it.
export function readLauncher(text) {
 const value = JSON.parse(text);
 const count = n => Number.isSafeInteger(n) && n >= 0;
 if (!value || typeof value !== 'object' || !count(value.version) || value.version < 1 || !count(value.minimum) || value.minimum > value.version ||
     ('start' in value && (!count(value.start) || value.start > value.version))) {
  throw Error('desktop/launcher.json must hold {"version": n >= 1, "minimum": 0..version, "start"?: 0..version}');
 }
 return { version: value.version, minimum: value.minimum, ...('start' in value ? { start: value.start } : {}) };
}
