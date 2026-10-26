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
//            Start 4 (owner decision, 1.1.0): launcher 3 installs (0.9.6, 1.0.0 installers;
//            1.0.1 ships 4) are told to install the latest installer. Updaters from stable
//            1.0.2 and earlier do not read start and may still start one release through
//            the update's own late restore point.
//
// When a launcher older than `minimum` starts a release, the release replaces
// it from its own verified source (desktop/LAUNCHER.md). Minimum 3 is the
// owner-accepted signing-key bridge. Minimum 4 replaces launchers without
// database restore points (0.9.6) and the 1.0.0 ones that report 3; every
// bump needs a Windows acceptance run. Launcher 6 retained the start-floor/staged
// replacement changes and waits for a timed-out self-test to close before cleanup
// (current version: desktop/launcher.json; minimum 4, start 4 since 1.1.0). Launcher 6's functional changes are source-updates.ts
// check() withdrawal handling and source-auth.ts/source-updates.ts lease/key transport
// removal (e02a8a6ff). prepareRollback, ColdClusterCopy and installed-entry.ts are untouched.
// The re-audited allowlist is 4, 5, 6: all fully hash ready points before selection.
// Any future launcher version needs a fresh audit.
export function readLauncher(text) {
 const value = JSON.parse(text);
 const count = n => Number.isSafeInteger(n) && n >= 0;
 if (!value || typeof value !== 'object' || !count(value.version) || value.version < 1 || !count(value.minimum) || value.minimum > value.version ||
     ('start' in value && (!count(value.start) || value.start > value.version))) {
  throw Error('desktop/launcher.json must hold {"version": n >= 1, "minimum": 0..version, "start"?: 0..version}');
 }
 return { version: value.version, minimum: value.minimum, ...('start' in value ? { start: value.start } : {}) };
}
