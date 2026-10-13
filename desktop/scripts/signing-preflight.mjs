import { sign, verify } from 'node:crypto';
import { readFileSync } from 'node:fs';
import { pathToFileURL } from 'node:url';
import { trustedKeys } from './trusted-keys.mjs';

export function checkSigningKey(privateKey, pem) {
  const probe = Buffer.from('AI Shuffle release signing preflight');
  const signature = sign(null, probe, privateKey);
  if (!trustedKeys(pem).some(key => verify(null, probe, key, signature))) {
    throw new Error('Signing key does not match the shipped update public key');
  }
}

export function checkReleaseSigning(env = process.env) {
  for (const name of ['DESKTOP_APPLICATION_SIGNING_KEY', 'CLOUDFLARE_API_TOKEN', 'CLOUDFLARE_ACCOUNT_ID', 'ACCOUNT_HUB_DATABASE_ID', 'RELEASE_GITHUB_REPO', 'RELEASE_GITHUB_REPO_ID']) {
    if (!env[name]) throw new Error(`${name} is missing in release-signing`);
  }
  if (!/^[a-f0-9-]{36}$/.test(env.ACCOUNT_HUB_DATABASE_ID)) throw new Error('Invalid ACCOUNT_HUB_DATABASE_ID');
  checkSigningKey(env.DESKTOP_APPLICATION_SIGNING_KEY, readFileSync(new URL('../build/application-update-key.pem', import.meta.url), 'utf8'));
}

if (process.argv[1] && import.meta.url === pathToFileURL(process.argv[1]).href) {
  checkReleaseSigning();
  console.log('Signing inputs are present and the key matches; deployment access still needs owner verification.');
}
