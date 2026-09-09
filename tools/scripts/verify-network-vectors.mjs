#!/usr/bin/env node
// Checks tests/core/network/vectors.json with WebCrypto, the API the account
// hub (Cloudflare Workers) and a phone client use. Every primitive the Python
// side produces must verify or decrypt here byte for byte.
//
//   node tools/scripts/verify-network-vectors.mjs
import { readFileSync } from 'node:fs';
import { fileURLToPath } from 'node:url';
import { dirname, join } from 'node:path';
import { webcrypto } from 'node:crypto';

const subtle = webcrypto.subtle;
const here = dirname(fileURLToPath(import.meta.url));
const vectors = JSON.parse(readFileSync(join(here, '..', '..', 'tests', 'core', 'network', 'vectors.json'), 'utf8'));
const enc = new TextEncoder();

const b64u = {
  decode(text) {
    const padded = text.replace(/-/g, '+').replace(/_/g, '/') + '='.repeat((4 - (text.length % 4)) % 4);
    return Uint8Array.from(Buffer.from(padded, 'base64'));
  },
  encode(bytes) {
    return Buffer.from(bytes).toString('base64').replace(/\+/g, '-').replace(/\//g, '_').replace(/=+$/, '');
  },
};

function canonical(value) {
  if (value === null || typeof value !== 'object') return JSON.stringify(value);
  if (Array.isArray(value)) return '[' + value.map(canonical).join(',') + ']';
  return '{' + Object.keys(value).sort().map((k) => JSON.stringify(k) + ':' + canonical(value[k])).join(',') + '}';
}

function concat(...parts) {
  const total = parts.reduce((n, p) => n + p.length, 0);
  const out = new Uint8Array(total);
  let offset = 0;
  for (const p of parts) { out.set(p, offset); offset += p.length; }
  return out;
}

async function hkdf(secret, info, salt) {
  const key = await subtle.importKey('raw', secret, 'HKDF', false, ['deriveBits']);
  const bits = await subtle.deriveBits({ name: 'HKDF', hash: 'SHA-256', salt: salt ?? new Uint8Array(0), info }, key, 256);
  return new Uint8Array(bits);
}

async function aesGcmDecrypt(rawKey, nonce, ciphertext, aad) {
  const key = await subtle.importKey('raw', rawKey, 'AES-GCM', false, ['decrypt']);
  return new Uint8Array(await subtle.decrypt({ name: 'AES-GCM', iv: nonce, additionalData: aad }, key, ciphertext));
}

async function sha256Hex(bytes) {
  return Buffer.from(await subtle.digest('SHA-256', bytes)).toString('hex');
}

let failures = 0;
function check(name, ok) {
  console.log((ok ? 'ok   ' : 'FAIL ') + name);
  if (!ok) failures += 1;
}

// --- Ed25519 -------------------------------------------------------------
{
  const pub = b64u.decode(vectors.signing.public);
  const key = await subtle.importKey('raw', pub, { name: 'Ed25519' }, false, ['verify']);
  const digest = await sha256Hex(pub);
  const fingerprint = digest.slice(0, 32).match(/.{4}/g).join('-');
  check('fingerprint matches', fingerprint === vectors.signing.fingerprint);
  for (const [i, c] of vectors.signing.cases.entries()) {
    const message = enc.encode(canonical(c.message));
    check(`canonical json #${i}`, Buffer.from(message).equals(Buffer.from(c.canonical, 'utf8')));
    const ok = await subtle.verify({ name: 'Ed25519' }, key, b64u.decode(c.signature), message);
    check(`ed25519 signature #${i}`, ok);
    const bad = await subtle.verify({ name: 'Ed25519' }, key, b64u.decode(c.signature), concat(message, enc.encode('x')));
    check(`ed25519 rejects altered message #${i}`, !bad);
  }
}

// --- AES-256-GCM payloads --------------------------------------------------
{
  const networkKey = b64u.decode(vectors.payload.network_key);
  for (const [i, c] of vectors.payload.cases.entries()) {
    const blob = b64u.decode(c.blob);
    const plain = await aesGcmDecrypt(networkKey, blob.slice(0, 12), blob.slice(12), enc.encode(c.aad));
    check(`payload #${i} decrypts`, Buffer.from(plain).equals(Buffer.from(c.plaintext, 'utf8')));
    let rejected = false;
    try { await aesGcmDecrypt(networkKey, blob.slice(0, 12), blob.slice(12), enc.encode(c.aad + '!')); } catch { rejected = true; }
    check(`payload #${i} rejects wrong aad`, rejected);
  }
}

// --- X25519 key wrap ---------------------------------------------------------
{
  const w = vectors.wrap;
  const recipientPrivate = b64u.decode(w.recipient_private);
  const recipientPublic = b64u.decode(w.recipient_public);
  const pkcs8 = concat(Uint8Array.from([0x30, 0x2e, 0x02, 0x01, 0x00, 0x30, 0x05, 0x06, 0x03, 0x2b, 0x65, 0x6e, 0x04, 0x22, 0x04, 0x20]), recipientPrivate);
  const priv = await subtle.importKey('pkcs8', pkcs8, { name: 'X25519' }, false, ['deriveBits']);
  const ephemeral = b64u.decode(w.wrapped.epk);
  const ephemeralKey = await subtle.importKey('raw', ephemeral, { name: 'X25519' }, false, []);
  const shared = new Uint8Array(await subtle.deriveBits({ name: 'X25519', public: ephemeralKey }, priv, 256));
  const kek = await hkdf(shared, concat(enc.encode('shuffle-network-key-wrap-v1'), ephemeral, recipientPublic));
  const key = await aesGcmDecrypt(kek, b64u.decode(w.wrapped.nonce), b64u.decode(w.wrapped.ct), ephemeral);
  check('x25519 wrap unwraps to the network key', b64u.encode(key) === w.key);
}

// --- Recovery key ---------------------------------------------------------------
{
  const r = vectors.recovery;
  const alphabet = '0123456789ABCDEFGHJKMNPQRSTVWXYZ';
  const cleaned = r.text.toUpperCase().replace(/[^A-Z0-9]/g, '');
  check('recovery key version prefix', cleaned.startsWith('A1'));
  let bits = 0n;
  for (const ch of cleaned.slice(2)) bits = (bits << 5n) | BigInt(alphabet.indexOf(ch));
  const raw = new Uint8Array(16);
  for (let i = 15; i >= 0; i--) { raw[i] = Number(bits & 0xffn); bits >>= 8n; }
  check('recovery text decodes to raw', b64u.encode(raw) === r.raw);
  const salt = enc.encode(r.network_id);
  const kek = await hkdf(raw, enc.encode('shuffle-network-recovery-v1'), salt);
  const key = await aesGcmDecrypt(kek, b64u.decode(r.wrapped.nonce), b64u.decode(r.wrapped.ct), salt);
  check('recovery wrap unwraps to the network key', b64u.encode(key) === r.key);
  const checkKey = await hkdf(raw, enc.encode('shuffle-network-recovery-v1-check'), salt);
  check('recovery check value matches', (await sha256Hex(checkKey)) === r.check);
}

if (failures) {
  console.error(`${failures} vector check(s) failed`);
  process.exit(1);
}
console.log('all network vectors verified with WebCrypto');
