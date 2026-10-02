/**
 * The Claude Code CLI: what `claude auth status` reports, and the in-app
 * terminal that runs `claude auth login` on this worker.
 *
 * AI Shuffle never handles a Claude sign-in itself. The owner signs in to the
 * CLI on Anthropic's page and pastes the code it shows into the terminal; this
 * state machine only relays the terminal and re-reads the CLI's answer.
 *
 * Shared by the setup wizard's `ClaudeCliPanel` and the Claude subscription
 * credential in Settings > Providers. Create one per mounted view, call
 * `init()` on mount and `dispose()` on destroy.
 */
import { getErrorMessage } from '$lib/api/client';
import {
	cancelClaudeCliLogin,
	getClaudeCliStatus,
	logoutClaudeCli,
	readClaudeCliLogin,
	sendClaudeCliLogin,
	startClaudeCliLogin,
	type ClaudeCliLoginState,
	type ClaudeCliStatus
} from '$lib/api/claude-cli';
import { auth } from '$lib/stores/auth.svelte';
import { hydrateModelRegistry } from '$lib/constants/models.svelte';

export interface ClaudeCliLoginOptions {
	/** Called with every fresh status (the wizard gates Continue on it). */
	onStatus?: (status: ClaudeCliStatus) => void;
	/** Called after the terminal output grows (scroll it to the end). */
	onOutput?: () => void;
}

type TerminalChunk = Pick<ClaudeCliLoginState, 'output' | 'offset' | 'reset' | 'url' | 'running' | 'error'>;

export class ClaudeCliLogin {
	status = $state<ClaudeCliStatus | null>(null);
	loadError = $state('');
	checking = $state(false);

	/** The sign-in terminal is shown. */
	terminalOpen = $state(false);
	/** `claude auth login` is still running. */
	running = $state(false);
	output = $state('');
	/** The sign-in link the CLI printed. */
	url = $state<string | null>(null);
	/** The code typed into the terminal's input. */
	code = $state('');
	sending = $state(false);
	loginError = $state('');
	loginResult = $state<'ok' | 'failed' | null>(null);
	signingOut = $state(false);

	#offset = 0;
	#pollTimer: ReturnType<typeof setTimeout> | undefined;
	#disposed = false;
	#options: ClaudeCliLoginOptions;

	constructor(options: ClaudeCliLoginOptions = {}) {
		this.#options = options;
	}

	/** The command to run on the host when the in-app terminal is not offered. */
	get hostCommand(): string {
		return this.status?.login.docker_command ?? this.status?.login.command ?? 'claude auth login';
	}

	/** Whether this worker offers the terminal to this viewer (owner, not the phone). */
	terminalAvailable(isAdmin: boolean, isMobile: boolean): boolean {
		return !!this.status?.login.terminal && isAdmin && !isMobile;
	}

	/** First load, then pick up a login still running from before a reload. */
	async init(canUseTerminal: () => boolean): Promise<void> {
		await this.load(false);
		if (this.#disposed || !canUseTerminal()) return;
		try {
			const state = await readClaudeCliLogin(0);
			if (!this.#disposed && state.active && state.running) {
				this.terminalOpen = true;
				this.#absorb(state);
				this.#schedulePoll();
			}
		} catch {
			// No terminal to resume.
		}
	}

	/** Ask the CLI for its status; `refresh` re-runs `claude auth status`. */
	async load(refresh: boolean): Promise<void> {
		this.checking = true;
		try {
			const next = await getClaudeCliStatus(refresh);
			if (this.#disposed) return;
			const prev = this.status;
			const changed =
				prev !== null && (prev.signed_in !== next.signed_in || !!prev.relogin_notice !== !!next.relogin_notice);
			const accountChanged =
				prev !== null && (prev.signed_in !== next.signed_in || (prev.plan ?? null) !== (next.plan ?? null));
			this.status = next;
			// The app-wide gates and the upgrade notice read /auth/status.
			if (changed) auth.checkAuth().catch(() => {});
			// The pickers list Claude models only while the registry reports a
			// Claude sign-in, so a sign-in, sign-out or plan change re-reads it.
			if (accountChanged) void hydrateModelRegistry();
			this.loadError = '';
			this.#options.onStatus?.(next);
		} catch (e) {
			if (!this.#disposed) this.loadError = getErrorMessage(e, 'Could not ask the Claude Code CLI for its status.');
		} finally {
			this.checking = false;
		}
	}

	async startLogin(): Promise<void> {
		this.loginError = '';
		this.loginResult = null;
		this.output = '';
		this.#offset = 0;
		this.url = null;
		this.terminalOpen = true;
		try {
			this.#absorb(await startClaudeCliLogin());
			this.#schedulePoll(300);
		} catch (e) {
			this.loginError = getErrorMessage(e, 'Could not start `claude auth login`.');
			this.running = false;
		}
	}

	async sendCode(): Promise<void> {
		const text = this.code.trim();
		if (!text || this.sending) return;
		this.sending = true;
		try {
			this.#absorb(await sendClaudeCliLogin(text, this.#offset));
			this.code = '';
			this.#schedulePoll(300);
		} catch (e) {
			this.loginError = getErrorMessage(e, 'Could not pass the code to the CLI.');
		} finally {
			this.sending = false;
		}
	}

	/** Stop a running login and hide the terminal. */
	async closeTerminal(): Promise<void> {
		clearTimeout(this.#pollTimer);
		if (this.running) {
			try {
				await cancelClaudeCliLogin();
			} catch {
				// The server stops an abandoned login on its own.
			}
		}
		this.running = false;
		this.terminalOpen = false;
		this.output = '';
		this.url = null;
		this.code = '';
	}

	/** `claude auth logout`, then ask again. */
	async signOut(): Promise<void> {
		if (this.signingOut) return;
		this.signingOut = true;
		this.loginError = '';
		try {
			const result = await logoutClaudeCli();
			if (!result.success) this.loginError = result.error || 'The CLI could not sign out.';
		} catch (e) {
			this.loginError = getErrorMessage(e, 'The CLI could not sign out.');
		} finally {
			this.signingOut = false;
			await this.load(true);
		}
	}

	dispose(): void {
		this.#disposed = true;
		clearTimeout(this.#pollTimer);
	}

	#schedulePoll(delay = 800): void {
		clearTimeout(this.#pollTimer);
		if (!this.#disposed) this.#pollTimer = setTimeout(() => void this.#poll(), delay);
	}

	#absorb(state: TerminalChunk): void {
		this.output = state.reset ? state.output : this.output + state.output;
		this.#offset = state.offset;
		this.url = state.url ?? this.url;
		this.running = state.running;
		if (state.error) this.loginError = state.error;
		const onOutput = this.#options.onOutput;
		if (onOutput) queueMicrotask(onOutput);
	}

	async #poll(): Promise<void> {
		try {
			const state = await readClaudeCliLogin(this.#offset);
			if (this.#disposed) return;
			this.#absorb(state);
			if (state.running) {
				this.#schedulePoll();
				return;
			}
			this.loginResult = state.succeeded ? 'ok' : state.active ? 'failed' : null;
			await this.load(true);
		} catch (e) {
			if (!this.#disposed) {
				this.loginError = getErrorMessage(e, 'Lost contact with the sign-in terminal.');
				this.running = false;
			}
		}
	}
}
