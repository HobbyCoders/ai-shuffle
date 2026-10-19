import assert from 'node:assert/strict';
import { spawnSync } from 'node:child_process';
import { promises as fs, readFileSync } from 'node:fs';
import { createRequire } from 'node:module';
import os from 'node:os';
import path from 'node:path';
import { fileURLToPath } from 'node:url';
import { test } from 'node:test';

const require = createRequire(import.meta.url);
const desktop = fileURLToPath(new URL('..', import.meta.url));
// Git may check source fixtures out with CRLF on Windows; assertions compare code, not checkout EOLs.
const channelScript = readFileSync(path.join(desktop, 'build', 'installer-channel.nsh'), 'utf8').replace(/\r\n/g, '\n');
// The script without its # comments, so assertions see only NSIS code.
const channelCode = channelScript.split('\n').map(line => line.replace(/^\s*#.*$/, '')).join('\n');
const macro = name => {
  const match = channelCode.match(new RegExp(`!macro ${name}\\n([\\s\\S]*?)\\n\\s*!macroend`));
  assert.ok(match, `installer-channel.nsh defines ${name}`);
  return match[1];
};
// Index of the ${endif} that closes the ${if}/${ifNot} at lines[start], counting nesting.
const matchingEndif = (lines, start) => {
  let depth = 0;
  for (let i = start; i < lines.length; i++) {
    if (/^\$\{if(not)?\}/i.test(lines[i])) depth++;
    else if (/^\$\{endif\}/i.test(lines[i]) && --depth === 0) return i;
  }
  assert.fail(`no \${endif} closes ${lines[start]}`);
};

test('real builder config loading keeps diagnostics off the runner event pipe', () => {
  // A plain process exposes ONLY the dependency's output, never TAP or test
  // events. This fails without the sink redirect regardless of pipe timing.
  const result = spawnSync(process.execPath, ['--input-type=module', '-e', `
    import assert from 'node:assert/strict';
    import { getConfig, validateConfiguration } from './test/helpers/builder-config.mjs';
    for (const filename of ['electron-builder.yml', 'electron-builder.preview.yml']) {
      const config = await getConfig(process.cwd(), filename, null);
      await validateConfiguration(config, { isEnabled: false });
      assert.equal(config.afterPack, 'scripts/verify-packaged-runtime.cjs');
    }
    // Only the stable installer offers the channel page; previews never update.
    assert.equal((await getConfig(process.cwd(), 'electron-builder.yml', null)).nsis.include, 'build/installer-channel.nsh');
    assert.equal((await getConfig(process.cwd(), 'electron-builder.preview.yml', null)).nsis.include, undefined);
    await assert.rejects(() => validateConfiguration({ unknownConfigurationKey: true }, { isEnabled: false }));
  `], { cwd: desktop, encoding: 'utf8', timeout: 30000 });
  assert.ifError(result.error);
  assert.equal(result.status, 0, result.stderr);
  assert.equal(result.stdout, '', 'builder logs must not share stdout with serialized test events');
  assert.match(result.stderr, /loaded configuration.*electron-builder\.yml/);
  assert.match(result.stderr, /loaded configuration.*electron-builder\.preview\.yml/);
});

test('the channel page writes the file the installed app reads, under the name Electron uses', async () => {
  // electron-builder defines APP_PACKAGE_NAME as package.json's name. Electron
  // names userData after productName, else name; neither the package nor the
  // stable config sets productName in the packaged metadata, and the stable
  // app never calls app.setName (src/main/index.ts renames previews only).
  const pkg = JSON.parse(readFileSync(path.join(desktop, 'package.json'), 'utf8'));
  assert.equal(pkg.name, 'ai-shuffle-desktop');
  assert.equal(pkg.productName, undefined);
  assert.doesNotMatch(readFileSync(path.join(desktop, 'electron-builder.yml'), 'utf8'), /^extraMetadata:/m);
  // installed-entry.ts: the update root is <userData>/updates; SourceUpdates reads CHANNEL_FILE there.
  assert.match(readFileSync(path.join(desktop, 'src', 'main', 'installed-entry.ts'), 'utf8').replace(/\r\n/g, '\n'),
    /const userData = app\.getPath\('userData'\);\n\s*const root = path\.join\(userData, 'updates'\);/);
  const { SourceUpdates, CHANNEL_FILE } = require('../dist/main/source-updates.js');
  assert.match(channelCode, /!define SHUFFLE_CHANNEL_DIR "\$APPDATA\\\$\{APP_PACKAGE_NAME\}\\updates"/);
  assert.match(channelCode, new RegExp(`!define SHUFFLE_CHANNEL_FILE "\\$\\{SHUFFLE_CHANNEL_DIR\\}\\\\${CHANNEL_FILE.replace('.', '\\.')}"`));

  // The one payload the installer writes is the one the app reads as nightly.
  const writes = [...channelCode.matchAll(/FileWrite \$0 '([^']*)'/g)].map(m => m[1]);
  assert.deepEqual(writes, ['{"channel":"nightly"}']);
  const root = await fs.mkdtemp(path.join(os.tmpdir(), 'installer-channel-'));
  try {
    const updates = new SourceUpdates({ root, publicKey: '', bootstrapNode: process.execPath });
    assert.equal(await updates.channel(), 'stable', 'a Stable install writes nothing, which reads as stable');
    await fs.writeFile(path.join(root, CHANNEL_FILE), writes[0]);
    assert.equal(await updates.channel(), 'nightly');
  } finally { await fs.rm(root, { recursive: true, force: true, maxRetries: 10, retryDelay: 100 }); }
});

test('the channel page never overwrites an existing choice and writes nothing for Stable', () => {
  // The uninstaller build gets none of it (makensis -WX fails on unused code).
  assert.match(channelCode, /^!ifndef BUILD_UNINSTALLER\n[\s\S]*\n!endif\s*$/m);
  assert.equal(channelCode.match(/^!endif/gm).length, 1);

  // Default stable; only /CHANNEL=nightly selects nightly for a silent install.
  const init = macro('customInit');
  assert.match(init, /StrCpy \$shuffleChannel "stable"[\s\S]*\$\{GetOptions\} \$R0 "\/CHANNEL=" \$R1/);
  assert.match(init, /\$\{andIf\} \$R1 == "nightly"\n\s*StrCpy \$shuffleChannel "nightly"/);

  // The page is skipped for installer updates and when a choice already exists.
  const page = macro('customPageAfterChangeDir');
  assert.match(page, /Page custom shuffleChannelPageCreate shuffleChannelPageLeave/);
  assert.match(page, /\$\{if\} \$\{isUpdated\}\n\s*Abort/);
  assert.match(page, /\$\{if\} \$\{FileExists\} "\$\{SHUFFLE_CHANNEL_FILE\}"\n\s*!insertmacro shuffleRestoreContext\n\s*Abort/);
  assert.ok(page.indexOf('FileExists') < page.indexOf('nsDialogs::Create'));
  assert.match(page, /\$\{else\}\n\s*\$\{NSD_Check\} \$shuffleChannelStable/, 'Stable is selected unless /CHANNEL=nightly was given');
  assert.match(page, /Nightly again in Settings > System > Updates once a stable release catches up/);

  // Writing: nightly only, only when the file is absent, via a temporary file
  // and Rename (which never replaces an existing file); never in the page code.
  const install = macro('customInstall');
  const lines = install.split('\n').map(line => line.trim()).filter(Boolean);
  assert.equal(lines[0], '${if} $shuffleChannel == "nightly"');
  assert.equal(matchingEndif(lines, 0), lines.length - 1, 'the whole write is inside the nightly branch');
  const guard = lines.indexOf('${ifNot} ${FileExists} "${SHUFFLE_CHANNEL_FILE}"');
  assert.ok(guard > 0, 'the write is guarded by the file being absent');
  const open = lines.indexOf('FileOpen $0 "${SHUFFLE_CHANNEL_FILE}.tmp" w');
  const rename = lines.indexOf('Rename "${SHUFFLE_CHANNEL_FILE}.tmp" "${SHUFFLE_CHANNEL_FILE}"');
  assert.ok(guard < open && open < rename, 'guard, then temporary file, then rename');
  assert.ok(matchingEndif(lines, guard) > rename, 'the rename is inside the file-absent guard, not after it');
  assert.deepEqual([...channelCode.matchAll(/^\s*(FileOpen|Rename|Delete|CopyFiles)\b.*$/gm)].map(m => m[0].trim()), [
    'FileOpen $0 "${SHUFFLE_CHANNEL_FILE}.tmp" w',
    'Rename "${SHUFFLE_CHANNEL_FILE}.tmp" "${SHUFFLE_CHANNEL_FILE}"',
    'Delete "${SHUFFLE_CHANNEL_FILE}.tmp"'
  ], 'nothing else opens, replaces or deletes a channel file');
  // userData is per user even for an all-users install.
  assert.ok(lines.indexOf('!insertmacro shuffleUserContext') > 0 && lines.indexOf('!insertmacro shuffleUserContext') < guard);
  assert.ok(lines.indexOf('!insertmacro shuffleRestoreContext') > rename);
});
