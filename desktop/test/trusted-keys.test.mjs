import assert from 'node:assert/strict';
import { generateKeyPairSync, sign, verify } from 'node:crypto';
import { readFile } from 'node:fs/promises';
import { createRequire } from 'node:module';
import { test } from 'node:test';
import { trustedKeys as publisherKeys } from '../scripts/trusted-keys.mjs';
const require = createRequire(import.meta.url);
const { trustedKeys } = require('../dist/main/trusted-keys.js');
const { verifySourceManifest } = require('../dist/main/source-updates.js');
const { verifyInstallerEntry } = require('../dist/main/installer-updates.js');

const pair = () => {
  const { privateKey, publicKey } = generateKeyPairSync('ed25519');
  return { privateKey, pem: publicKey.export({ type: 'spki', format: 'pem' }) };
};
const source = { format: 2, revision: 'a'.repeat(40), sha256: 'b'.repeat(64), size: 1, sequence: 1 };
const installer = { ...source, type: 'desktop-installer', format: 1, platform: 'mac', arch: 'arm64', version: '1.0.0', file: 'installer.dmg' };
const garbage = '-----BEGIN PUBLIC KEY-----\ninvalid\n-----END PUBLIC KEY-----\n';

for (const [name, manifest, check] of [
  ['source', source, (entry, pem) => verifySourceManifest(JSON.stringify(entry), pem)],
  ['installer', installer, (entry, pem) => verifyInstallerEntry(entry, pem, 'mac')],
]) {
  test(`${name} accepts either rotation key, but never an untrusted signer or empty trust bundle`, () => {
    const old = pair(), next = pair(), stranger = pair();
    const payload = Buffer.from(JSON.stringify(manifest));
    for (const signer of [old, next, stranger]) {
      const signature = sign(null, payload, signer.privateKey);
      const entry = { payload: payload.toString('base64'), signature: signature.toString('base64') };
      for (const keys of [[old], [next], [old, next], [next, old]]) {
        const pem = keys.map(key => key.pem).join('');
        if (keys.includes(signer)) assert.deepEqual(check(entry, pem), manifest);
        else assert.throws(() => check(entry, pem), /signature/);
        // Publisher preflight and desktop use the same any-key policy.
        for (const parse of [trustedKeys, publisherKeys]) {
          assert.equal(parse(pem).some(key => verify(null, payload, key, signature)), keys.includes(signer));
        }
      }
      for (const pem of ['', 'garbage', garbage]) assert.throws(() => check(entry, pem), /No valid Ed25519/);
    }
    const payloadEntry = { payload: payload.toString('base64'), signature: sign(null, payload, next.privateKey).toString('base64') };
    assert.deepEqual(check(payloadEntry, garbage + next.pem), manifest);
    const wrongType = generateKeyPairSync('ec', { namedCurve: 'prime256v1' }).publicKey.export({ type: 'spki', format: 'pem' });
    assert.throws(() => check(payloadEntry, wrongType), /No valid Ed25519/);
    assert.deepEqual(check(payloadEntry, wrongType + next.pem), manifest);
  });
}

test('shipped trust bundle contains the original key first and the replacement second', async () => {
  const pem = await readFile(new URL('../build/application-update-key.pem', import.meta.url), 'utf8');
  const keys = trustedKeys(pem);
  assert.deepEqual(keys, publisherKeys(pem));
  assert.equal(keys.length, 2);
  assert.ok(keys[0].includes('MCowBQYDK2VwAyEAE1mkIwKYaoGbwdakUcU1ud2e+hdttD//aCUSCPVg4rI='));
  assert.ok(keys[1].includes('MCowBQYDK2VwAyEAhvyeThCGOfInuEhVZEMzGOJXSmOrDd8v4nooUkej8Tg='));
});
