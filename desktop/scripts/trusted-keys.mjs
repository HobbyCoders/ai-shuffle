import { createPublicKey } from 'node:crypto';

/** Public-only Ed25519 trust bundle; ignore invalid blocks, never accept an empty bundle. */
export function trustedKeys(pem) {
  const keys = (pem.match(/-----BEGIN PUBLIC KEY-----[\s\S]*?-----END PUBLIC KEY-----/g) ?? []).filter(block => {
    try { return createPublicKey(block).asymmetricKeyType === 'ed25519'; }
    catch { return false; }
  });
  if (!keys.length) throw new Error('No valid Ed25519 update trust keys');
  return keys;
}
