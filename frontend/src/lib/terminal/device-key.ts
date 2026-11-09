/**
 * This device's terminal key: the proof a worker accepts for opening a shell
 * from another worker. Contract: docs/internal/terminals.md.
 *
 * A non-exportable P-256 signing key made by WebCrypto and kept in IndexedDB.
 * The private half never reaches JavaScript as bytes, so neither a page script
 * nor the worker that serves this page can copy it out. A browser that is not
 * on a secure origin has no WebCrypto, and that is final: there is no
 * exportable key, localStorage copy or software fallback here on purpose.
 */

const DB_NAME = 'ai-shuffle-terminal-device';
const STORE = 'keys';
const RECORD = 'device';
const LINK_CODE_DOMAIN = 'ai-shuffle-terminal-link-code-v1:';

export const NO_DEVICE_KEY = "This browser can't hold a terminal key at this address, because it needs a secure connection. Use the desktop app, the Android app or an HTTPS address.";

export interface DeviceKey {
	keyId: string;
	/** The raw public point, base64url. Sent to the receiver when the device is linked. */
	publicKey: string;
	/** The code the owner enters on the receiving worker. Only ever worked out here. */
	code: string;
	sign(text: string): Promise<string>;
}

/** Why this browser can't hold a key, or null when it can. */
export function deviceKeyProblem(): string | null {
	const hasSubtle = typeof crypto !== 'undefined' && !!crypto.subtle;
	const secure = typeof isSecureContext === 'boolean' ? isSecureContext : false;
	return secure && hasSubtle && typeof indexedDB !== 'undefined' ? null : NO_DEVICE_KEY;
}

const base64url = (bytes: Uint8Array) =>
	btoa(String.fromCharCode(...bytes)).replace(/\+/g, '-').replace(/\//g, '_').replace(/=+$/, '');

const sha256 = async (bytes: Uint8Array<ArrayBuffer>) =>
	new Uint8Array(await crypto.subtle.digest('SHA-256', bytes));

const BASE32 = 'ABCDEFGHIJKLMNOPQRSTUVWXYZ234567';

function base32(bytes: Uint8Array): string {
	let bits = 0;
	let value = 0;
	let out = '';
	for (const byte of bytes) {
		value = (value << 8) | byte;
		bits += 8;
		while (bits >= 5) {
			out += BASE32[(value >>> (bits - 5)) & 31];
			bits -= 5;
		}
		value &= (1 << bits) - 1;
	}
	return bits ? out + BASE32[(value << (5 - bits)) & 31] : out;
}

/** The twelve-character code for a public key, matching the receiving worker's own working. */
export async function linkCode(rawPublicKey: Uint8Array<ArrayBuffer>): Promise<string> {
	const domain = new TextEncoder().encode(LINK_CODE_DOMAIN);
	const message = new Uint8Array(domain.length + rawPublicKey.length);
	message.set(domain);
	message.set(rawPublicKey, domain.length);
	return base32((await sha256(message)).slice(0, 8)).slice(0, 12);
}

function openDatabase(): Promise<IDBDatabase> {
	return new Promise((resolve, reject) => {
		const request = indexedDB.open(DB_NAME, 1);
		request.onupgradeneeded = () => request.result.createObjectStore(STORE);
		request.onsuccess = () => resolve(request.result);
		request.onerror = () => reject(request.error ?? new Error('Could not open the key store'));
	});
}

function readKey(db: IDBDatabase): Promise<CryptoKeyPair | undefined> {
	return new Promise((resolve, reject) => {
		const request = db.transaction(STORE).objectStore(STORE).get(RECORD);
		request.onsuccess = () => resolve(request.result as CryptoKeyPair | undefined);
		request.onerror = () => reject(request.error ?? new Error('Could not read the key store'));
	});
}

/**
 * Store `candidate` unless another tab got there first, and return whichever
 * key is stored. One readwrite transaction, so two tabs agree on one key.
 */
function keepFirst(db: IDBDatabase, candidate: CryptoKeyPair): Promise<CryptoKeyPair> {
	return new Promise((resolve, reject) => {
		const transaction = db.transaction(STORE, 'readwrite');
		const store = transaction.objectStore(STORE);
		let kept = candidate;
		const lookup = store.get(RECORD);
		lookup.onsuccess = () => {
			if (lookup.result) kept = lookup.result as CryptoKeyPair;
			else store.put(candidate, RECORD);
		};
		transaction.oncomplete = () => resolve(kept);
		transaction.onerror = () => reject(transaction.error ?? new Error('Could not save the key'));
		transaction.onabort = () => reject(transaction.error ?? new Error('Could not save the key'));
	});
}

async function loadOrCreate(): Promise<CryptoKeyPair> {
	const db = await openDatabase();
	try {
		const stored = await readKey(db);
		if (stored) return stored;
		const created = await crypto.subtle.generateKey({ name: 'ECDSA', namedCurve: 'P-256' }, false, ['sign', 'verify']);
		return await keepFirst(db, created);
	} finally {
		db.close();
	}
}

async function build(): Promise<DeviceKey> {
	const pair = await loadOrCreate();
	const raw = new Uint8Array(await crypto.subtle.exportKey('raw', pair.publicKey));
	return {
		keyId: base64url(await sha256(raw)),
		publicKey: base64url(raw),
		code: await linkCode(raw),
		sign: async (text) => {
			const signature = await crypto.subtle.sign({ name: 'ECDSA', hash: 'SHA-256' }, pair.privateKey, new TextEncoder().encode(text));
			return base64url(new Uint8Array(signature));
		},
	};
}

let cached: Promise<DeviceKey> | null = null;

/** This device's key, made the first time it is needed. Rejects where the browser can't hold one. */
export function deviceKey(): Promise<DeviceKey> {
	const problem = deviceKeyProblem();
	if (problem) return Promise.reject(new Error(problem));
	cached ??= build().catch((error) => {
		cached = null;
		throw error;
	});
	return cached;
}

/** A plain name for this device, shown to the owner on the receiving worker. */
export function deviceLabel(): string {
	const agent = typeof navigator === 'undefined' ? '' : navigator.userAgent;
	const browser = /Electron/.test(agent) ? 'AI Shuffle desktop' : /Edg\//.test(agent) ? 'Edge' : /Firefox\//.test(agent) ? 'Firefox'
		: /Chrome\//.test(agent) ? 'Chrome' : /Safari\//.test(agent) ? 'Safari' : 'Browser';
	const system = /Android/.test(agent) ? 'Android' : /iPhone|iPad/.test(agent) ? 'iOS' : /Windows/.test(agent) ? 'Windows'
		: /Mac OS X/.test(agent) ? 'macOS' : /Linux/.test(agent) ? 'Linux' : '';
	return system ? `${browser} on ${system}` : browser;
}
