/**
 * Saved workers.
 *
 * A "worker" is an AI Shuffle server the desktop app can open: the local
 * install on this computer, a box on the LAN, or a remote managed worker
 * reached through its tunnel hostname. Only the origin and a display name are
 * stored here. Sign-in state lives in Chromium's cookie jar for the
 * `persist:shuffle` partition, exactly as it would in a browser profile, so
 * this file never holds a credential.
 */
import { promises as fs } from 'node:fs';
import { randomUUID } from 'node:crypto';
import path from 'node:path';

export interface SavedServer {
	id: string;
	name: string;
	/** Normalised origin, e.g. `http://localhost:8000` or `https://studio.example.com`. */
	url: string;
	addedAt: string;
	lastUsedAt: string | null;
}

interface StoreFile {
	version: 1;
	servers: SavedServer[];
	lastServerId: string | null;
}

const EMPTY: StoreFile = { version: 1, servers: [], lastServerId: null };

/**
 * Accepts what a person would paste — a bare host, an origin, or a pairing
 * link such as `https://host/pair#token=CODE` — and returns the origin plus
 * the pairing code when one was included. Throws a readable message
 * otherwise.
 */
export function parseServerInput(raw: string): { origin: string; pairingCode: string | null } {
	let text = raw.trim();
	if (!text) throw new Error('Enter the worker address.');
	if (!/^[a-z]+:\/\//i.test(text)) text = `http://${text}`;
	let url: URL;
	try {
		url = new URL(text);
	} catch {
		throw new Error('That is not a valid address.');
	}
	if (url.protocol !== 'http:' && url.protocol !== 'https:') {
		throw new Error('Worker addresses must start with http:// or https://.');
	}
	if (url.username || url.password) throw new Error('Leave credentials out of the address.');
	if (!url.hostname) throw new Error('The address needs a host name.');

	let pairingCode: string | null = null;
	const fragment = new URLSearchParams(url.hash.slice(1));
	const token = fragment.get('token');
	if (url.pathname === '/pair' && token) {
		const code = token.replace(/[\s-]/g, '').toUpperCase();
		if (!/^[A-Z0-9]{12}$/.test(code)) throw new Error('The pairing code in that link is not 12 characters.');
		pairingCode = code;
	} else if (url.pathname !== '/' && url.pathname !== '') {
		// People paste deep links from their browser bar; the origin is all we keep.
		pairingCode = null;
	}
	return { origin: url.origin, pairingCode };
}

export function defaultServerName(origin: string): string {
	const url = new URL(origin);
	if (url.hostname === 'localhost' || url.hostname === '127.0.0.1' || url.hostname === '::1') {
		return 'This computer';
	}
	return url.hostname;
}

export class ServerStore {
	private data: StoreFile = { ...EMPTY, servers: [] };
	private loaded = false;

	constructor(private readonly file: string) {}

	async load(): Promise<void> {
		try {
			const text = await fs.readFile(this.file, 'utf8');
			const parsed = JSON.parse(text) as Partial<StoreFile>;
			const servers = Array.isArray(parsed.servers) ? parsed.servers : [];
			this.data = {
				version: 1,
				servers: servers.filter(isSavedServer),
				lastServerId: typeof parsed.lastServerId === 'string' ? parsed.lastServerId : null
			};
		} catch {
			this.data = { ...EMPTY, servers: [] };
		}
		this.loaded = true;
	}

	list(): SavedServer[] {
		return this.data.servers.map((s) => ({ ...s }));
	}

	get(id: string): SavedServer | null {
		const found = this.data.servers.find((s) => s.id === id);
		return found ? { ...found } : null;
	}

	byOrigin(origin: string): SavedServer | null {
		const found = this.data.servers.find((s) => s.url === origin);
		return found ? { ...found } : null;
	}

	last(): SavedServer | null {
		return this.data.lastServerId ? this.get(this.data.lastServerId) : null;
	}

	async add(input: { name?: string; url: string }): Promise<SavedServer> {
		const { origin } = parseServerInput(input.url);
		const existing = this.byOrigin(origin);
		if (existing) return existing;
		const server: SavedServer = {
			id: randomUUID(),
			name: (input.name ?? '').trim() || defaultServerName(origin),
			url: origin,
			addedAt: new Date().toISOString(),
			lastUsedAt: null
		};
		this.data.servers.push(server);
		await this.save();
		return { ...server };
	}

	async rename(id: string, name: string): Promise<void> {
		const server = this.data.servers.find((s) => s.id === id);
		if (!server) return;
		const trimmed = name.trim();
		if (!trimmed) return;
		server.name = trimmed.slice(0, 80);
		await this.save();
	}

	async remove(id: string): Promise<void> {
		this.data.servers = this.data.servers.filter((s) => s.id !== id);
		if (this.data.lastServerId === id) this.data.lastServerId = null;
		await this.save();
	}

	async markUsed(id: string): Promise<void> {
		const server = this.data.servers.find((s) => s.id === id);
		if (!server) return;
		server.lastUsedAt = new Date().toISOString();
		this.data.lastServerId = id;
		await this.save();
	}

	async clearLast(): Promise<void> {
		this.data.lastServerId = null;
		await this.save();
	}

	private async save(): Promise<void> {
		if (!this.loaded) return;
		await fs.mkdir(path.dirname(this.file), { recursive: true });
		const tmp = `${this.file}.${process.pid}.tmp`;
		await fs.writeFile(tmp, JSON.stringify(this.data, null, 2), 'utf8');
		await fs.rename(tmp, this.file);
	}
}

function isSavedServer(value: unknown): value is SavedServer {
	if (!value || typeof value !== 'object') return false;
	const v = value as Record<string, unknown>;
	return (
		typeof v.id === 'string' &&
		typeof v.name === 'string' &&
		typeof v.url === 'string' &&
		/^https?:\/\//.test(v.url)
	);
}
