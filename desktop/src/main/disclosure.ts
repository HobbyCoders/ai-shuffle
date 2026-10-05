/**
 * "Agents run as you": the plain disclosure the desktop app shows on first
 * launch and keeps available afterwards (Help > Agent Access, and the web
 * UI's Settings through the bridge).
 *
 * On the desktop there is no container between an agent and the computer.
 * Agents run as the signed-in OS user, with that user's files, programs,
 * network and CLI logins, including the user's own `claude` login.
 *
 * The acknowledgement is a plain file in the app's profile. Bumping
 * AGENT_ACCESS_VERSION shows the disclosure again after an upgrade.
 */
import { promises as fs } from 'node:fs';
import path from 'node:path';

export const AGENT_ACCESS_VERSION = 1;

/** Set by automated acceptance runs that drive the real app without a person. */
export const DISCLOSURE_ACCEPTED_ENV = 'SHUFFLE_DESKTOP_ACCEPT_AGENT_ACCESS';

export const AGENT_ACCESS = {
	title: 'Agents run as you',
	message: 'Agents in AI Shuffle Desktop run as you, on this computer.',
	points: [
		'They use your own accounts: the provider sign-ins, API keys and GitHub login you set up.',
		'They can read, change and delete any file your user account can, run programs, and reach your network, not only the workspace folder.',
		'Computer use, when you turn it on, controls your real screen, mouse and keyboard.',
		'Nothing isolates an agent from the rest of your account. Review tool approvals, and do not leave Bypass on for work you would not run yourself.'
	],
	footer: 'Want a boundary? Run the worker in Docker or on another machine and connect to it instead. You can read this again under Help > Agent Access.'
} as const;

export function agentAccessDetail(): string {
	return [...AGENT_ACCESS.points.map((point) => `• ${point}`), '', AGENT_ACCESS.footer].join('\n');
}

export interface DisclosureStoreOptions {
	file: string;
	env?: NodeJS.ProcessEnv;
}

export class DisclosureStore {
	constructor(private readonly opts: DisclosureStoreOptions) {}

	static forProfile(userData: string, env: NodeJS.ProcessEnv = process.env): DisclosureStore {
		return new DisclosureStore({ file: path.join(userData, 'agent-access.json'), env });
	}

	async acknowledged(): Promise<boolean> {
		if ((this.opts.env ?? process.env)[DISCLOSURE_ACCEPTED_ENV] === '1') return true;
		try {
			const saved = JSON.parse(await fs.readFile(this.opts.file, 'utf8')) as { version?: unknown };
			return typeof saved.version === 'number' && saved.version >= AGENT_ACCESS_VERSION;
		} catch {
			return false;
		}
	}

	async acknowledge(now: Date = new Date()): Promise<void> {
		await fs.mkdir(path.dirname(this.opts.file), { recursive: true });
		const tmp = `${this.opts.file}.tmp`;
		await fs.writeFile(tmp, JSON.stringify({ version: AGENT_ACCESS_VERSION, acknowledgedAt: now.toISOString() }));
		await fs.rename(tmp, this.opts.file);
	}
}

export type Choice = 'continue' | 'quit';

/**
 * First launch: ask once, before any worker starts. "Quit" leaves nothing
 * running and asks again next time.
 */
export async function ensureAgentAccessAcknowledged(store: DisclosureStore, ask: () => Promise<Choice>): Promise<boolean> {
	if (await store.acknowledged()) return true;
	if ((await ask()) !== 'continue') return false;
	await store.acknowledge();
	return true;
}
