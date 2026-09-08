/**
 * Desktop settings: the PostgreSQL connection the local worker uses.
 *
 * Stored as JSON in userData. The password is encrypted with Electron's
 * safeStorage (DPAPI on Windows, Keychain on macOS, the keyring on Linux) and
 * kept as base64; where no keyring is available it is stored as plain text
 * and the file says so, so the setup screen can tell the person.
 */
import { promises as fs } from 'node:fs';
import path from 'node:path';

export interface PostgresConfig {
	host: string;
	port: number;
	user: string;
	password: string;
	database: string;
}

interface StoredFile {
	version: 1;
	postgres: {
		host: string;
		port: number;
		user: string;
		database: string;
		password: { encrypted: string } | { plain: string } | null;
	} | null;
}

export interface Cipher {
	available(): boolean;
	encrypt(text: string): Buffer;
	decrypt(data: Buffer): string;
}

export const DEFAULT_POSTGRES: Omit<PostgresConfig, 'password'> = {
	host: 'localhost',
	port: 5432,
	user: 'ai_shuffle',
	database: 'ai_shuffle'
};

const IDENTIFIER = /^[A-Za-z_][A-Za-z0-9_]*$/;

/** Validates a form submission and returns a clean config or a message. */
export function validatePostgres(input: Partial<Record<keyof PostgresConfig, unknown>>): { ok: true; config: PostgresConfig } | { ok: false; error: string } {
	const host = String(input.host ?? '').trim();
	const user = String(input.user ?? '').trim();
	const database = String(input.database ?? '').trim();
	const password = String(input.password ?? '');
	const port = Number(input.port ?? DEFAULT_POSTGRES.port);
	if (!host) return { ok: false, error: 'Enter the PostgreSQL host.' };
	if (/[\s/@]/.test(host)) return { ok: false, error: 'The host is a name or address only, without a scheme, path or credentials.' };
	if (!Number.isInteger(port) || port < 1 || port > 65535) return { ok: false, error: 'The port must be a number between 1 and 65535.' };
	if (!user) return { ok: false, error: 'Enter the database user.' };
	if (!database) return { ok: false, error: 'Enter the database name.' };
	if (!IDENTIFIER.test(database)) return { ok: false, error: 'The database name may contain letters, digits and underscores, and must not start with a digit.' };
	if (!password) return { ok: false, error: 'Enter the database password.' };
	return { ok: true, config: { host, port, user, password, database } };
}

export class SettingsStore {
	private data: StoredFile = { version: 1, postgres: null };
	private loaded = false;

	constructor(private readonly file: string, private readonly cipher: Cipher) {}

	async load(): Promise<void> {
		try {
			const parsed = JSON.parse(await fs.readFile(this.file, 'utf8')) as Partial<StoredFile>;
			this.data = { version: 1, postgres: isStoredPostgres(parsed.postgres) ? parsed.postgres : null };
		} catch {
			this.data = { version: 1, postgres: null };
		}
		this.loaded = true;
	}

	get configured(): boolean {
		return this.data.postgres !== null && this.data.postgres.password !== null;
	}

	/** True when the stored password could not be protected by the OS keyring. */
	get passwordStoredPlain(): boolean {
		const p = this.data.postgres?.password;
		return !!p && 'plain' in p;
	}

	postgres(): PostgresConfig | null {
		const stored = this.data.postgres;
		if (!stored || !stored.password) return null;
		let password: string;
		try {
			password = 'encrypted' in stored.password
				? this.cipher.decrypt(Buffer.from(stored.password.encrypted, 'base64'))
				: stored.password.plain;
		} catch {
			return null;
		}
		return { host: stored.host, port: stored.port, user: stored.user, database: stored.database, password };
	}

	/** The connection without its password, for prefilling the form. */
	postgresPublic(): Omit<PostgresConfig, 'password'> {
		const stored = this.data.postgres;
		if (!stored) return { ...DEFAULT_POSTGRES };
		return { host: stored.host, port: stored.port, user: stored.user, database: stored.database };
	}

	async savePostgres(config: PostgresConfig): Promise<void> {
		const password = this.cipher.available()
			? { encrypted: this.cipher.encrypt(config.password).toString('base64') }
			: { plain: config.password };
		this.data = {
			version: 1,
			postgres: { host: config.host, port: config.port, user: config.user, database: config.database, password }
		};
		await this.save();
	}

	private async save(): Promise<void> {
		if (!this.loaded) return;
		await fs.mkdir(path.dirname(this.file), { recursive: true });
		const tmp = `${this.file}.${process.pid}.tmp`;
		await fs.writeFile(tmp, JSON.stringify(this.data, null, 2), { encoding: 'utf8', mode: 0o600 });
		await fs.rename(tmp, this.file);
	}
}

function isStoredPostgres(value: unknown): value is NonNullable<StoredFile['postgres']> {
	if (!value || typeof value !== 'object') return false;
	const v = value as Record<string, unknown>;
	if (typeof v.host !== 'string' || typeof v.user !== 'string' || typeof v.database !== 'string') return false;
	if (typeof v.port !== 'number') return false;
	const p = v.password;
	if (p === null) return true;
	if (!p || typeof p !== 'object') return false;
	const pw = p as Record<string, unknown>;
	return typeof pw.encrypted === 'string' || typeof pw.plain === 'string';
}

/** A cipher that stores nothing secret; used when safeStorage is unavailable and in tests. */
export const plainCipher: Cipher = {
	available: () => false,
	encrypt: (text) => Buffer.from(text, 'utf8'),
	decrypt: (data) => data.toString('utf8')
};
