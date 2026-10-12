import assert from 'node:assert/strict';
import { generateKeyPairSync } from 'node:crypto';
import { readFileSync } from 'node:fs';
import { test } from 'node:test';
import { checkSigningKey, checkReleaseSigning } from '../scripts/signing-preflight.mjs';

const pair = () => {
  const { privateKey, publicKey } = generateKeyPairSync('ed25519');
  return { privateKey, pem: publicKey.export({ type: 'spki', format: 'pem' }) };
};
test('stable release preflight accepts the second rotation key and fails closed', () => {
  const old = pair(), next = pair(), stranger = pair();
  const bundle = old.pem + next.pem;
  for (const signer of [old, next]) assert.doesNotThrow(() => checkSigningKey(signer.privateKey, bundle));
  assert.throws(() => checkSigningKey(stranger.privateKey, bundle), /does not match/);
  assert.throws(() => checkSigningKey(next.privateKey, old.pem), /does not match/);
  for (const invalid of ['', 'invalid', '-----BEGIN PUBLIC KEY-----\ninvalid\n-----END PUBLIC KEY-----']) {
    assert.throws(() => checkSigningKey(next.privateKey, invalid), /No valid Ed25519/);
  }
});
test('release workflow uses the tested preflight and still requires release settings', () => {
  assert.match(readFileSync(new URL('../../.github/workflows/release.yml', import.meta.url), 'utf8'), /run: node desktop\/scripts\/signing-preflight.mjs/);
  assert.throws(() => checkReleaseSigning({}), /DESKTOP_APPLICATION_SIGNING_KEY is missing/);
});
