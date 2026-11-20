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
// Execute just the installer hooks against in-memory files and HKCU. The
// tests below separately pin the order in which electron-builder invokes them.
function runStartupHook(name, { old = 'C:\\User app', target = old, mode = 'CurrentUser',
  owner = 'C:\\Local profile', stamp = owner, stampAt = old, run = `"${old}\\AI Shuffle.exe"`, approved = 'blocked',
  updated = false, silentMachineUpgrade = false, replacement = '', guardError = '0',
  retire = '', exeExists = true } = {}) {
  const v = { $INSTDIR: target, $installMode: mode, $LOCALAPPDATA: owner, $hasPerMachineInstallation: silentMachineUpgrade ? '1' : '0', $shuffleRetireStartup: retire };
  const stampPath = '\\resources\\login-item-uninstall-v3';
  const resolve = token => token.replace(/^(['"])(.*)\1$/, '$2')
    .replace(/\$\{SHUFFLE_LOGIN_ITEM_STAMP\}/g, stampPath.slice(1))
    .replace(/\$\{APP_EXECUTABLE_FILENAME\}/g, 'AI Shuffle.exe')
    .replace(/\$\w+/g, key => v[key] ?? '').toLowerCase();
  const condition = expr => {
    if (expr === '${Silent}') return silentMachineUpgrade;
    if (expr === '${isUpdated}') return updated;
    if (expr === '${Errors}') return false;
    if (expr.startsWith('${FileExists} ')) {
      const file = resolve(expr.slice(14));
      return (exeExists && file === resolve(target + '\\AI Shuffle.exe')) || (!!stamp && file === resolve(stampAt + stampPath));
    }
    const comparison = expr.match(/^(.*) (==|!=) (.*)$/);
    assert.ok(comparison, expr);
    return (resolve(comparison[1]) === resolve(comparison[3])) === (comparison[2] === '==');
  };
  const body = name === 'success' ? channelCode.match(/Function un\.onUninstSuccess\n([\s\S]*?)\n\s*FunctionEnd/)[1] : macro(name);
  const lines = body.split('\n').map(line => line.trim()).filter(Boolean);
  const active = [];
  const result = (proceeds, reason) => ({ proceeds, run, approved, replacement, retire: v.$shuffleRetireStartup, ...(reason && { reason }) });
  for (const line of lines) {
    const logic = line.match(/^\$\{(if|ifNot|andIf|orIf|elseif|endif)\}(?: (.*))?$/);
    if (logic) {
      const [, op, expr] = logic;
      if (op === 'endif') active.pop();
      else if (op === 'if' || op === 'ifNot') active.push({ passing: condition(expr) === (op === 'if'), matched: false });
      else if (op === 'elseif') {
        const state = active.at(-1);
        state.matched ||= state.passing;
        state.passing = !state.matched && condition(expr);
      } else {
        const state = active.at(-1);
        if (!state.matched) state.passing = op === 'andIf' ? state.passing && condition(expr) : state.passing || condition(expr);
      }
      continue;
    }
    if (active.some(state => !state.passing)) continue;
    if (/^Section(?:In|End| )/.test(line) || /^!insertmacro shuffle(?:User|Restore)Context$/.test(line)) continue;
    if (line.startsWith('!insertmacro shuffleStartupRefuse ')) return result(false, line.split('"')[1]);
    if (line === '!insertmacro setInstallModePerAllUsers') v.$installMode = 'all';
    else if (line.startsWith('!insertmacro shuffleReadStartupOwner ')) v.$1 = stamp;
    else if (line.startsWith('StrCpy ')) {
      const [, dest, source] = line.match(/^StrCpy (\$\w+) (.*)$/);
      v[dest] = source === '"$INSTDIR"' ? target : source === '"$LOCALAPPDATA"' ? owner : source.slice(1, -1);
    }
    else if (line.startsWith("System::Call 'kernel32::SetEnvironmentVariableW(")) { replacement = v.$0; v.$1 = '1'; }
    else if (line === 'ReadEnvStr $2 "${SHUFFLE_STARTUP_REPLACEMENT}"') v.$2 = replacement;
    else if (line.startsWith("System::Call 'kernel32::CreateFileW(")) v.$0 = guardError === '0' ? '123' : '-1';
    else if (line === 'Pop $4') v.$4 = guardError;
    else if (line === 'ClearErrors') continue;
    else if (line === 'ReadRegStr $3 HKCU "${SHUFFLE_RUN}" "${SHUFFLE_LOGIN_ITEM}"') v.$3 = run;
    else if (line === 'DeleteRegValue HKCU "${SHUFFLE_RUN}" "${SHUFFLE_LOGIN_ITEM}"') run = '';
    else if (line === 'DeleteRegValue HKCU "${SHUFFLE_APPROVAL}" "${SHUFFLE_LOGIN_ITEM}"') approved = '';
    else assert.fail(`Unexpected operation in ${name}: ${line}`);
  }
  return result(true);
}

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
  const channelEnd = matchingEndif(lines, 0);
  assert.ok(channelEnd < lines.indexOf('FileOpen $0 "$INSTDIR\\${SHUFFLE_LOGIN_ITEM_STAMP}" w'), 'the installer stamp is independent of channel');
  const guard = lines.indexOf('${ifNot} ${FileExists} "${SHUFFLE_CHANNEL_FILE}"');
  assert.ok(guard > 0, 'the write is guarded by the file being absent');
  const open = lines.indexOf('FileOpen $0 "${SHUFFLE_CHANNEL_FILE}.tmp" w');
  const rename = lines.indexOf('Rename "${SHUFFLE_CHANNEL_FILE}.tmp" "${SHUFFLE_CHANNEL_FILE}"');
  assert.ok(guard < open && open < rename, 'guard, then temporary file, then rename');
  assert.ok(matchingEndif(lines, guard) > rename, 'the rename is inside the file-absent guard, not after it');
  assert.deepEqual([...channelCode.matchAll(/^\s*(FileOpen|Rename|Delete|CopyFiles)\b.*SHUFFLE_CHANNEL_.*$/gm)].map(m => m[0].trim()), [
    'FileOpen $0 "${SHUFFLE_CHANNEL_FILE}.tmp" w',
    'Rename "${SHUFFLE_CHANNEL_FILE}.tmp" "${SHUFFLE_CHANNEL_FILE}"',
    'Delete "${SHUFFLE_CHANNEL_FILE}.tmp"'
  ], 'nothing else opens, replaces or deletes a channel file');
  // userData is per user even for an all-users install.
  assert.ok(lines.indexOf('!insertmacro shuffleUserContext') > 0 && lines.indexOf('!insertmacro shuffleUserContext') < guard);
  assert.ok(lines.indexOf('!insertmacro shuffleRestoreContext') > rename);
});

test('per-user installers provision the guard in app-owned LocalAppData before stamping', () => {
  assert.match(channelCode, /!define SHUFFLE_LOGIN_ITEM_STAMP "resources\\login-item-uninstall-v3"/);
  assert.match(channelCode, /!define SHUFFLE_STARTUP_GUARD_DIR "\$LOCALAPPDATA\\\$\{APP_PACKAGE_NAME\}"/);
  assert.equal(require('../package.json').name, 'ai-shuffle-desktop');
  const install = macro('customInstall').split('\n').map(line => line.trim()).filter(Boolean);
  const user = install.indexOf('${if} $installMode == "CurrentUser"');
  const granted = install.indexOf('${if} $0 != -1');
  const write = install.indexOf('FileWriteUTF16LE $0 "$LOCALAPPDATA"');
  assert.ok(user > 0 && user < granted && granted < write && write < matchingEndif(install, granted));
  const uninstall = macro('customUnInstall');
  assert.match(uninstall, /CreateFileW\(w "\$\{SHUFFLE_STARTUP_GUARD\}", i 0x80000000, i 0, p 0, i 3, i 0x80, p 0\)/);
  assert.doesNotMatch(uninstall, /DeleteRegValue/);
  assert.deepEqual(channelCode.split('\n').map(l => l.trim()).filter(l => l.startsWith('DeleteRegValue')), [
    'DeleteRegValue HKCU "${SHUFFLE_RUN}" "${SHUFFLE_LOGIN_ITEM}"',
    'DeleteRegValue HKCU "${SHUFFLE_APPROVAL}" "${SHUFFLE_LOGIN_ITEM}"'
  ]);
});

test('pinned NSIS selects the replacement before either ExecWait; old removal fences after the close check', () => {
  const template = file => readFileSync(path.join(require.resolve('app-builder-lib/package.json'), '..', 'templates/nsis', file), 'utf8');
  const section = template('installSection.nsh');
  assert.match(template('installer.nsi'), /!insertmacro customHeader[\s\S]+Section "install"[\s\S]+!include "installSection.nsh"/);
  assert.match(section, /CHECK_APP_RUNNING[\s\S]+!insertmacro uninstallOldVersion SHELL_CONTEXT[\s\S]+!insertmacro uninstallOldVersion HKEY_CURRENT_USER[\s\S]+!insertmacro installApplicationFiles/);
  assert.match(template('include/installUtil.nsh'), /StrCpy \$0 "\$0 --updated"[\s\S]+ExecWait .*\$uninstallerFileNameTemp[\s\S]+ExecWait .*\$uninstallerFileName/);
  const uninstaller = template('uninstaller.nsh');
  assert.match(uninstaller, /\$\{If\} \$\{Silent\}\s+call un\.checkAppRunning/);
  assert.match(uninstaller, /Section "un\.[\s\S]+call un\.checkAppRunning[\s\S]+!insertmacro customUnInstall[\s\S]+Call un\.atomicRMDir[\s\S]+Call un\.restoreFiles\s+Pop \$R0\s+Abort/);
  assert.doesNotMatch(uninstaller, /Function un\.onUninstSuccess/, 'builder must not already define our post-success hook');
});

const oldRun = '"C:\\User app\\AI Shuffle.exe"';
const atPrompt = { run: oldRun, approved: 'blocked' };
const uninstall = options => runStartupHook('customUnInstall', options);
const state = ({ run, approved }) => ({ run, approved });
const success = (result, exeExists = false) => state(runStartupHook('success', { ...result, exeExists }));
const absent = { run: '', approved: '' };

test('opt-in during the close prompt is carried only for a same-folder per-user upgrade', () => {
  for (const [destination, preserve] of [
    [{ target: 'c:\\user APP' }, true], [{ target: 'C:\\New app' }, false],
    [{ mode: 'all' }, false], [{ silentMachineUpgrade: true }, false]
  ]) {
    const wait = runStartupHook('customHeader', { ...destination, ...absent });
    assert.equal(wait.proceeds, true);
    // Cancel never invokes the child uninstaller, so the opt-in stands.
    assert.deepEqual(runStartupHook('customHeader', { ...destination, ...atPrompt }), { ...wait, ...atPrompt });
    if (!preserve) {
      const busy = uninstall({ ...atPrompt, updated: true, replacement: wait.replacement, guardError: '32' });
      assert.equal(busy.proceeds, false);
      assert.match(busy.reason, /changing/);
      assert.deepEqual(state(busy), atPrompt);
    }
    const removal = uninstall({ ...atPrompt, updated: true, replacement: wait.replacement });
    assert.equal(removal.proceeds, true);
    assert.deepEqual(state(removal), atPrompt, 'registry remains untouched until removal succeeds');
    // A locked payload file triggers restoreFiles and Abort, not onUninstSuccess.
    assert.deepEqual(state(removal), atPrompt, 'rollback keeps Run AND StartupApproved');
    assert.deepEqual(success(removal, true), atPrompt, 'incomplete RMDir cannot retire either value');
    assert.deepEqual(success(removal), preserve ? atPrompt : absent);
  }
});

test('unprovisioned/redirected profiles do not block removal; missing guards cannot admit a writer', () => {
  const roaming = '\\\\profile-server\\read-only\\User';
  const local = 'C:\\Users\\User\\AppData\\Local';
  const dest = { target: 'C:\\New app', updated: true, owner: local, stamp: local, roaming };
  const removal = uninstall({ ...dest, target: 'C:\\User app' });
  assert.equal(removal.proceeds, true);
  assert.deepEqual(success(removal), absent);
  for (const guardError of ['2', '3']) {
    const missing = uninstall({ ...dest, target: 'C:\\User app', guardError });
    assert.equal(missing.proceeds, true);
    assert.deepEqual(success(missing), absent);
  }
  const denied = uninstall({ guardError: '5' });
  assert.equal(denied.proceeds, false);
  assert.match(denied.reason, /permissions/);
  assert.deepEqual(state(denied), atPrompt);
  assert.equal(uninstall({ stamp: '', guardError: '5' }).proceeds, true, 'failed provisioning creates no uninstall dependency');
});

test('only the owning user can replace or uninstall a stamped copy; true uninstall ignores the replacement hint', () => {
  assert.equal(runStartupHook('customHeader', { old: '', target: 'C:\\Machine app', mode: 'all' }).proceeds, true);
  assert.equal(runStartupHook('customHeader', { stamp: 'C:\\Another user', mode: 'all' }).proceeds, false);
  assert.equal(uninstall({ stamp: 'C:\\Another user' }).proceeds, false);
  assert.equal(uninstall({ mode: 'all' }).proceeds, false);
  assert.deepEqual(success(uninstall({ stamp: '' })), atPrompt, 'legacy unstamped copies have no authority');
  assert.deepEqual(success(uninstall({ replacement: 'C:\\User app' })), absent, 'true uninstall ignores a replacement hint');
  assert.deepEqual(success(uninstall({ updated: true })), absent, 'unknown destination does not justify carrying the registration');
  const unrelated = { run: '"C:\\Other app\\AI Shuffle.exe"', approved: 'blocked' };
  assert.deepEqual(success(uninstall(unrelated)), unrelated);
});
