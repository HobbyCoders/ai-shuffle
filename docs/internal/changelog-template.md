# Changelog template

Buyers read the changelog in the app (`docs/content/getting-started/changelog.md`,
mirrored in `frontend/src/lib/docs/content/getting-started/changelog.md`) and,
shortened, next to the update button. The update notes come from the annotated
release tag (`git tag -a vX.Y.Z -F notes.md`, under 4,000 characters; see
`docs/ci/release-channels.md`).

## Writing an entry

1. List what changed since the last tag: `git log --no-merges --oneline vA.B.C..HEAD`.
2. Keep only what a buyer notices or should know. Drop CI, tests, refactors,
   website and hub-only work unless it changes what a buyer sees or sends.
3. Group by area, in this order, and leave out empty groups: Install and
   packaging, Updates, License, Security, Agents and providers, Sessions and
   interface, Connectors, Desktop app, Android app, Remote access and accounts.
4. One line per change, in plain language, saying what is different for the
   buyer. No commit hashes, issue numbers, internal plan IDs or model names.
   Name Claude Code only as the program AI Shuffle runs, never as a feature.
5. Put anything the buyer must do under **Action needed**, first.
6. Put the new entry at the top of the changelog, below the introduction.
   Replace a "Next release" entry with the numbered one when the tag is cut.
7. Copy the entry, trimmed to the lines that matter most, into the tag message.

## Entry

```markdown
## X.Y.Z (YYYY-MM-DD)

One sentence on what this release is about.

### Action needed

- Update the image: this release needs a new container. Set the version to X.Y.Z in `.env` (Compose).

### Install and packaging

- ...

### Updates

- ...

### License

- ...

### Security

- ...

### Agents and providers

- ...

### Sessions and interface

- ...

### Connectors

- ...

### Desktop app

- ...

### Android app

- ...

### Remote access and accounts

- ...

### Fixed

- ...
```
