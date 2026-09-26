/** Generated credentials for the embedded server; never a database setup form. */
import { promises as fs } from 'node:fs';
import { writePrivateFile } from './private-file';

export interface PostgresConfig {
	host: string;
	port: number;
	user: string;
	password: string;
	database: string;
	/** Retained on disk solely to distinguish older external connection records. */
	managed?: boolean;
}

interface StoredFile {
	version: 1;
	postgres: (Omit<PostgresConfig, 'password'> & { password: { encrypted: string } | { plain: string } }) | null;
}

export interface Cipher {
	available(): boolean;
	encrypt(text: string): Buffer;
	decrypt(data: Buffer): string;
}

/** Only the supervisor's generated loopback endpoint is accepted. */
export function isManagedConnection(value: unknown): value is PostgresConfig {
	if (!value || typeof value !== 'object') return false;
	const v = value as Record<string, unknown>;
	const identifier = /^[A-Za-z_][A-Za-z0-9_]{0,62}$/;
	return v.host === '127.0.0.1' && typeof v.port === 'number' && Number.isInteger(v.port) && v.port > 0 && v.port <= 65535
		&& typeof v.user === 'string' && identifier.test(v.user)
		&& typeof v.database === 'string' && identifier.test(v.database)
		&& typeof v.password === 'string' && v.password.length > 0 && !/[\x00-\x1f\x7f]/.test(v.password);
}

export class SettingsStore {
	private data: StoredFile = { version: 1, postgres: null };
	private loaded = false;
	constructor(private readonly file: string, private readonly cipher: Cipher) {}

	async load(): Promise<void> {
		try {
			const parsed = JSON.parse(await fs.readFile(this.file, 'utf8')) as StoredFile;
			const stored = parsed?.postgres;
			// Ignore legacy external records without rewriting or importing them.
			this.data = { version: 1, postgres: parsed?.version === 1 && stored?.managed === true && stored.password
				&& (('encrypted' in stored.password && typeof stored.password.encrypted === 'string') || ('plain' in stored.password && typeof stored.password.plain === 'string')) ? stored : null };
		} catch { this.data = { version: 1, postgres: null }; }
		this.loaded = true;
	}

	postgres(): PostgresConfig | null {
		const stored = this.data.postgres;
		if (!stored) return null;
		try {
			const password = 'encrypted' in stored.password ? this.cipher.decrypt(Buffer.from(stored.password.encrypted, 'base64')) : stored.password.plain;
			const config = { ...stored, password, managed: true };
			return isManagedConnection(config) ? config : null;
		} catch { return null; }
	}

	async savePostgres(config: PostgresConfig): Promise<void> {
		if (config.managed !== true || !isManagedConnection(config)) throw new Error('Only generated embedded PostgreSQL credentials can be stored.');
		if (!this.loaded) throw new Error('The credential store is not loaded.');
		const password = this.cipher.available() ? { encrypted: this.cipher.encrypt(config.password).toString('base64') } : { plain: config.password };
		const next: StoredFile = { version: 1, postgres: { ...config, managed: true, password } };
		await writePrivateFile(this.file, JSON.stringify(next, null, 2));
		this.data = next;
	}
}

export const plainCipher: Cipher = {
	available: () => false,
	encrypt: (text) => Buffer.from(text, 'utf8'),
	decrypt: (data) => data.toString('utf8')
};
