// The nightly display version is read from Git tags, which can change while a
// publication runs. The release flow tags the version-bump commit on main while
// that commit's nightly is in flight, so a describe in the UI build job gave
// 1.1.0-dev.186+gaea5a10d4 and a describe minutes later in the signing job gave
// 1.2.0 for the same revision. The UI stamp then no longer matched the manifest
// and the publish failed.
//
// desktop-source.yml therefore resolves the version once in its gate job and
// hands it (SOURCE_VERSION) to both the UI build and this signer. The signer
// still resolves the version itself and accepts a different pinned value only
// when it is exactly what the resolver (android/ci/resolve-version-name.py)
// returned for this revision before a newer tag landed on it:
// <tag>-dev.<first-parent distance>+g<abbreviated revision>.
const DEV = /^([0-9]+\.[0-9]+\.[0-9]+(?:[.-][A-Za-z0-9.-]+)?)-dev\.([1-9][0-9]*)\+g([0-9a-f]{7,40})$/;

export function nightlyVersion({ resolved, pinned, revision, git }) {
 if (!pinned || pinned === resolved) return resolved;
 const match = DEV.exec(pinned);
 if (!match || !revision.startsWith(match[3])) throw Error(`Pinned version ${pinned} does not name ${revision}`);
 const [, base, distance] = match;
 const tag = `refs/tags/v${base}`;
 let tagged;
 try { tagged = git('rev-parse', '--verify', '--quiet', `${tag}^{commit}`); } catch { tagged = ''; }
 if (!tagged || !git('rev-list', '--first-parent', revision).split('\n').includes(tagged) ||
     git('rev-list', '--first-parent', '--count', `${tagged}..${revision}`) !== distance) {
  throw Error(`Pinned version ${pinned} is not ${revision}'s distance from v${base}`);
 }
 return pinned;
}
