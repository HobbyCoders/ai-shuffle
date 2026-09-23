// The release tag is the version source. CI passes it as APP_VERSION (with or
// without the leading "v"); local and dev builds fall back to package.json,
// which tracks the latest tag.
import { readFileSync } from 'node:fs';

const pkg = JSON.parse(readFileSync(new URL('./package.json', import.meta.url), 'utf8'));
const fromEnv = (process.env.APP_VERSION || '').trim().replace(/^v(?=\d)/i, '');

export const appVersion = fromEnv || pkg.version;
