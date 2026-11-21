/**
 * Vault: the data and form state behind Settings > Vault.
 *
 * - The list is the owner's saved sign-ins on this worker. The worker never
 *   sends a stored secret, so secret fields always start empty: on an existing
 *   sign-in a blank secret keeps the stored one and a typed one replaces it.
 * - Saving is explicit. Only changed fields are sent, and typed secrets are
 *   dropped from the form as soon as a save is sent, whether or not it works.
 * - "Add sign-in" makes one local draft (never two).
 * - When the list reloads, a clean form follows the new values and a dirty
 *   form keeps the owner's edits and is flagged "changed elsewhere". A sign-in
 *   removed elsewhere loses its form, typed secrets included.
 */
import { Globe, SquareTerminal } from 'lucide-svelte';
import { api, getErrorMessage } from '$lib/api/client';
import { auth } from '$lib/stores/auth.svelte';
import { isForbidden, settingsAccess } from '$lib/settings/access.svelte';
import type { SettingsIcon } from '$lib/settings/categories';

export const DRAFT_ID = 'new-vault-sign-in';
const ENDPOINT = '/vault/entries';

export type VaultKind = 'machine' | 'service';

/** A saved sign-in as the worker lists it. Never carries a secret. */
export interface VaultEntry {
	id: string;
	kind: VaultKind;
	label: string;
	/** `username@host:port`, or the service address. */
	target: string;
	/** Pinned SSH host key or HTTPS certificate; blank when none is pinned. */
	fingerprint: string;
	host?: string;
	port?: number;
	username?: string;
	uses_key?: boolean;
	writable?: boolean;
	auth_header?: string;
	auth_scheme?: string;
}

export interface VaultForm {
	kind: VaultKind;
	label: string;
	host: string;
	port: number;
	/** The SSH account, or a web service's account when it signs in with a password. */
	username: string;
	useKey: boolean;
	/** A web service that signs in with a username and password (HTTP Basic), not a token. */
	basic: boolean;
	url: string;
	authHeader: string;
	authScheme: string;
	writable: boolean;
	/** A replacement pin; blank keeps the stored one. */
	fingerprint: string;
	// Secrets: typed here, never loaded from the worker.
	password: string;
	privateKey: string;
	passphrase: string;
	secret: string;
}

export interface EditResult {
	updated: boolean;
	revoked: number;
	host_key_cleared: boolean;
}

export const KIND_OPTIONS: { value: VaultKind; label: string }[] = [
	{ value: 'machine', label: 'SSH machine' },
	{ value: 'service', label: 'Web service' }
];

const SECRETS = ['password', 'privateKey', 'passphrase', 'secret'] as const;

export function emptyForm(kind: VaultKind = 'machine'): VaultForm {
	return {
		kind,
		label: '',
		host: '',
		port: 22,
		username: '',
		useKey: false,
		basic: false,
		url: '',
		authHeader: 'Authorization',
		authScheme: 'Bearer',
		writable: false,
		fingerprint: '',
		password: '',
		privateKey: '',
		passphrase: '',
		secret: ''
	};
}

export function formFromEntry(entry: VaultEntry): VaultForm {
	// Older entries list only `username@host:port`.
	const parsed = /^([^@]+)@(.+):([0-9]+)$/.exec(entry.target);
	const service = entry.kind === 'service';
	return {
		...emptyForm(entry.kind),
		label: entry.label,
		host: service ? '' : (entry.host ?? parsed?.[2] ?? ''),
		port: service ? 22 : (entry.port ?? Number(parsed?.[3] ?? 22)),
		username: service ? (entry.username ?? '') : (entry.username ?? parsed?.[1] ?? ''),
		useKey: !!entry.uses_key,
		basic: service && !!entry.username,
		url: service ? entry.target : '',
		authHeader: entry.auth_header ?? 'Authorization',
		authScheme: entry.auth_scheme ?? 'Bearer',
		writable: !!entry.writable
	};
}

export function kindLabel(kind: VaultKind): string {
	return kind === 'service' ? 'Web service' : 'SSH machine';
}

export function kindIcon(kind: VaultKind): SettingsIcon {
	return kind === 'service' ? Globe : SquareTerminal;
}

/** What the stored secret is, without the secret. */
export function secretLabel(entry: VaultEntry): string {
	if (entry.kind === 'service') return entry.username ? 'Username and password' : entry.auth_header || 'Authorization';
	return entry.uses_key ? 'Private key' : 'Password';
}

/** The body that adds a sign-in. Only the fields of its own kind are sent. */
export function createBody(f: VaultForm): Record<string, unknown> {
	const body: Record<string, unknown> = { kind: f.kind, label: f.label.trim() };
	if (f.fingerprint.trim()) body.fingerprint = f.fingerprint.trim();
	if (f.kind === 'service') {
		body.url = f.url.trim();
		// The worker encodes a username and password itself; the header is fixed.
		body.auth_header = f.basic ? 'Authorization' : f.authHeader.trim();
		body.auth_scheme = f.basic ? 'Basic' : f.authScheme.trim();
		if (f.basic) body.username = f.username.trim();
		body.writable = f.writable;
		body.secret = f.secret;
		return body;
	}
	body.host = f.host.trim();
	body.port = Number(f.port);
	body.username = f.username.trim();
	if (f.useKey) {
		body.private_key = f.privateKey;
		if (f.passphrase) body.passphrase = f.passphrase;
	} else {
		body.password = f.password;
	}
	return body;
}

/** The body that changes a sign-in: only what differs from where the form started. */
export function editBody(f: VaultForm, initial: VaultForm): Record<string, unknown> {
	const body: Record<string, unknown> = {};
	if (f.label.trim() !== initial.label) body.label = f.label.trim();
	if (f.fingerprint.trim()) body.fingerprint = f.fingerprint.trim();
	if (f.kind === 'service') {
		if (f.url.trim() !== initial.url) body.url = f.url.trim();
		if (f.basic !== initial.basic) {
			// Another way to sign in: say all of it, so nothing stored is reused as the other kind.
			body.auth_header = f.basic ? 'Authorization' : f.authHeader.trim();
			body.auth_scheme = f.basic ? 'Basic' : f.authScheme.trim();
			body.username = f.basic ? f.username.trim() : '';
		} else if (f.basic) {
			if (f.username.trim() !== initial.username) body.username = f.username.trim();
		} else {
			if (f.authHeader.trim() !== initial.authHeader) body.auth_header = f.authHeader.trim();
			if (f.authScheme.trim() !== initial.authScheme) body.auth_scheme = f.authScheme.trim();
		}
		if (f.writable !== initial.writable) body.writable = f.writable;
		if (f.secret) body.secret = f.secret;
		return body;
	}
	if (f.host.trim() !== initial.host) body.host = f.host.trim();
	if (Number(f.port) !== initial.port) body.port = Number(f.port);
	if (f.username.trim() !== initial.username) body.username = f.username.trim();
	if (f.useKey) {
		if (f.privateKey) body.private_key = f.privateKey;
		if (f.passphrase) body.passphrase = f.passphrase;
	} else if (f.password) {
		body.password = f.password;
	}
	return body;
}

/** Problems the owner can fix before anything is sent. The worker validates again. */
export function problems(f: VaultForm, initial?: VaultForm): string[] {
	const found: string[] = [];
	if (!f.label.trim()) found.push('Give the sign-in a name');
	if (f.kind === 'service') {
		if (!/^https:\/\/\S+$/i.test(f.url.trim())) found.push('Enter an address that starts with https://');
		if (f.basic) {
			const name = f.username.trim();
			if (!name) found.push('Enter the username');
			else if (name.includes(':')) found.push('A username cannot contain a colon');
		}
		// A stored token is never reused as a password, nor a password as a token.
		if ((!initial || initial.basic !== f.basic) && !f.secret) {
			found.push(f.basic ? 'Enter the password' : 'Enter the secret the service expects');
		}
		return found;
	}
	if (!f.host.trim()) found.push('Enter the machine address');
	if (!f.username.trim()) found.push('Enter the username');
	const port = Number(f.port);
	if (!Number.isInteger(port) || port < 1 || port > 65535) found.push('Enter a port between 1 and 65535');
	if (f.useKey) {
		// Switching from a password needs the key; keeping a key may leave it blank.
		if ((!initial || !initial.useKey) && !f.privateKey) found.push('Paste the private key');
	} else if ((!initial || initial.useKey) && !f.password) {
		found.push('Enter the password');
	}
	return found;
}

function clearSecrets(f: VaultForm): void {
	for (const name of SECRETS) f[name] = '';
}

function same(a: VaultForm, b: VaultForm): boolean {
	return JSON.stringify(a) === JSON.stringify(b);
}

const PREVIEW_TEXT = 'A preview sign-in cannot open the Vault.';
export const INSECURE_TEXT =
	"The Vault is not available on this connection. Open Settings in the AI Shuffle desktop app window, or through this worker's HTTPS remote access address.";

/** HTTPS or loopback, the only connections the worker accepts a secret on. */
function secureConnection(): boolean {
	return typeof window !== 'undefined' && !!window.isSecureContext;
}

export class VaultModel {
	loading = $state(false);
	loaded = $state(false);
	/** Not the owner (an unapproved device). */
	forbidden = $state(false);
	/** Why the Vault cannot be shown here at all (connection, preview). */
	blocked = $state('');
	loadError = $state('');
	/** False while the worker's encryption key is locked: nothing can be saved. */
	available = $state(true);
	entries = $state<VaultEntry[]>([]);
	/** Forms by entry id. */
	forms = $state<Record<string, VaultForm>>({});
	/** The values each form started from. */
	private initial = $state<Record<string, VaultForm>>({});
	errors = $state<Record<string, string[]>>({});
	notices = $state<Record<string, string>>({});
	changedElsewhere = $state<Record<string, boolean>>({});
	saving = $state<string | null>(null);
	draft = $state<VaultForm | null>(null);
	/** The draft whose request is under way; the owner may have left it for another since. */
	private submitted: VaultForm | null = null;

	entry(id: string): VaultEntry | undefined {
		return this.entries.find((e) => e.id === id);
	}

	/** Whether sign-ins can be added or changed from this page right now. */
	get editable(): boolean {
		return this.loaded && !this.forbidden && !this.blocked && this.available;
	}

	async load(): Promise<void> {
		// A preview sign-in is refused by the worker; do not ask.
		if (auth.current.status?.preview_access) {
			this.block(PREVIEW_TEXT);
			return;
		}
		// The worker refuses the Vault on a plain HTTP connection; never type a secret into one.
		if (!secureConnection()) {
			this.block(INSECURE_TEXT);
			return;
		}
		this.loading = true;
		try {
			const data = await api.get<{ entries: VaultEntry[]; vault_available: boolean }>(ENDPOINT);
			this.entries = [...data.entries].sort((a, b) => a.label.localeCompare(b.label));
			this.available = data.vault_available;
			this.forbidden = false;
			this.blocked = '';
			this.loadError = '';
			this.loaded = true;
			this.reconcile();
		} catch (e) {
			// An unapproved device is read-only everywhere in Settings. For the owner,
			// a refusal is about this connection: the worker says why.
			if (isForbidden(e) && settingsAccess.readOnly) this.forbidden = true;
			else if (isForbidden(e)) this.block(getErrorMessage(e, INSECURE_TEXT));
			else this.loadError = 'Could not load the Vault';
		} finally {
			this.loading = false;
		}
	}

	/** The Vault cannot be shown: forget everything, typed secrets included. */
	private block(reason: string): void {
		this.blocked = reason;
		this.entries = [];
		this.forms = {};
		this.initial = {};
		this.draft = null;
		this.errors = {};
		this.notices = {};
		this.changedElsewhere = {};
		this.loaded = true;
	}

	/** The form for a sign-in or the draft. Call outside rendering (component init). */
	ensure(id: string): VaultForm | undefined {
		if (id === DRAFT_ID) return this.draft ?? undefined;
		if (this.forms[id]) return this.forms[id];
		const e = this.entry(id);
		if (!e) return undefined;
		this.initial[id] = formFromEntry(e);
		this.forms[id] = formFromEntry(e);
		return this.forms[id];
	}

	form(id: string): VaultForm | undefined {
		return id === DRAFT_ID ? (this.draft ?? undefined) : this.forms[id];
	}

	started(id: string): VaultForm | undefined {
		return this.initial[id];
	}

	dirty(id: string): boolean {
		if (id === DRAFT_ID) return this.draft !== null;
		const f = this.forms[id];
		return !!f && !!this.initial[id] && !same(f, this.initial[id]);
	}

	addDraft(): string {
		// A draft that is being added is no longer the owner's to edit: the next one starts empty.
		if (!this.draft || this.draft === this.submitted) this.draft = emptyForm();
		return DRAFT_ID;
	}

	discardDraft(): void {
		this.draft = null;
		delete this.errors[DRAFT_ID];
	}

	/** Back to the saved values; typed secrets are dropped. */
	discard(id: string): void {
		delete this.errors[id];
		delete this.changedElsewhere[id];
		this.reset(id);
	}

	private reset(id: string): void {
		const e = this.entry(id);
		if (!e) {
			delete this.forms[id];
			delete this.initial[id];
			return;
		}
		this.initial[id] = formFromEntry(e);
		this.forms[id] = formFromEntry(e);
	}

	/**
	 * Saves the form. Returns the sign-in's id on success, or null when the
	 * owner has since left the draft it was sent from (nothing to show in its place).
	 */
	async save(id: string): Promise<string | null> {
		const f = this.form(id);
		if (!f || this.saving) return null;
		const isDraft = id === DRAFT_ID;
		const initial = isDraft ? undefined : this.initial[id];
		const found = problems(f, initial);
		if (found.length) {
			this.errors[id] = found;
			return null;
		}
		if (!secureConnection()) {
			clearSecrets(f);
			this.errors[id] = [INSECURE_TEXT];
			return null;
		}
		const body = isDraft ? createBody(f) : editBody(f, initial!);
		delete this.errors[id];
		delete this.notices[id];
		this.saving = id;
		// Typed secrets live only in this request from here on.
		clearSecrets(f);
		try {
			if (isDraft) {
				this.submitted = this.draft;
				const created = await api.post<{ id: string }>(ENDPOINT, body);
				const current = this.draft === this.submitted;
				if (current) this.draft = null;
				await this.load();
				return current ? created.id : null;
			}
			const result = Object.keys(body).length
				? await api.patch<EditResult>(`${ENDPOINT}/${encodeURIComponent(id)}`, body)
				: null;
			await this.load();
			this.discard(id);
			if (result) this.notices[id] = savedNotice(result);
			return id;
		} catch (e) {
			// A failure belongs to the draft that was sent, not to one started since.
			if (isDraft && this.draft !== this.submitted) return null;
			this.errors[id] = [
				isForbidden(e) && settingsAccess.readOnly
					? 'You need owner access to change the Vault'
					: getErrorMessage(e, 'Could not save the sign-in')
			];
			return null;
		} finally {
			for (const name of ['password', 'private_key', 'passphrase', 'secret']) if (name in body) body[name] = '';
			this.submitted = null;
			this.saving = null;
		}
	}

	async remove(id: string): Promise<boolean> {
		try {
			await api.delete(`${ENDPOINT}/${encodeURIComponent(id)}`);
			delete this.forms[id];
			delete this.initial[id];
			delete this.errors[id];
			delete this.notices[id];
			delete this.changedElsewhere[id];
			await this.load();
			return true;
		} catch (e) {
			this.errors[id] = [getErrorMessage(e, 'Could not remove the sign-in')];
			return false;
		}
	}

	/** After a reload: clean forms follow the worker; dirty forms get flagged. */
	private reconcile(): void {
		for (const id of Object.keys(this.forms)) {
			const e = this.entry(id);
			if (!e) {
				delete this.forms[id];
				delete this.initial[id];
				delete this.errors[id];
				delete this.notices[id];
				delete this.changedElsewhere[id];
				continue;
			}
			if (!this.dirty(id)) {
				this.reset(id);
				delete this.changedElsewhere[id];
			} else if (!same(formFromEntry(e), this.initial[id])) {
				this.changedElsewhere[id] = true;
			}
		}
	}
}

function savedNotice(result: EditResult): string {
	const parts: string[] = [];
	if (result.revoked > 0)
		parts.push(result.revoked === 1 ? 'Ended 1 access that used this sign-in' : `Ended ${result.revoked} accesses that used this sign-in`);
	if (result.host_key_cleared) parts.push('The next connection pins the new address');
	return parts.join('. ');
}
