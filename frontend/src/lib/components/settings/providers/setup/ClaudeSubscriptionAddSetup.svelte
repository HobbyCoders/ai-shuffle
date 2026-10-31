<script lang="ts">
	/**
	 * "Another Subscription" for Claude: a Claude Code CLI sign-in into a fresh
	 * folder on this worker (`POST …/subscription/add-another`). Docker and
	 * Linux workers run `claude auth login` in the in-app terminal; the desktop
	 * app and Windows show the command to run in a terminal on that worker.
	 * Either way the saved-accounts list is polled; once the CLI reports the
	 * sign-in, the worker saves it as the active subscription and clears
	 * `login`. Cancel stops it and removes the unfinished folder; when the CLI
	 * had already finished signing in, the worker keeps that sign-in instead
	 * and this view opens it, as a finished poll would.
	 *
	 * Before the first extra sign-in, the CLI's own sign-in is saved as the
	 * `default` entry so it stays listed (and can be switched back to) once
	 * the new one becomes active. If that save fails for any reason other than
	 * "not signed in", the sign-in does not start.
	 */
	import { onDestroy, onMount } from 'svelte';
	import { SquareTerminal } from 'lucide-svelte';
	import { getErrorMessage } from '$lib/api/client';
	import {
		addAnotherClaudeSubscription,
		cancelClaudeSubscriptionLogin,
		claudeAccountError,
		getClaudeAccounts,
		saveCurrentClaudeSubscription,
		type ClaudeSubscriptionLogin
	} from '$lib/api/claude-auth';
	import { providers } from '$lib/stores/providers.svelte';
	import { getSettingsAccess } from '$lib/settings/access.svelte';
	import RowGroup from '../../rows/RowGroup.svelte';
	import SettingRow from '../../rows/SettingRow.svelte';
	import Segmented from '../../rows/Segmented.svelte';
	import CodeBlock from '../../rows/CodeBlock.svelte';
	import { ClaudeCliLogin, type TerminalChunk } from '../claude-cli-login.svelte';
	import { claudeSubItem } from '../providers-model';
	import ClaudeCliTerminal from './ClaudeCliTerminal.svelte';

	interface Props {
		onFinish: (item: string) => void;
		onCancel: () => void;
		/** Poll interval for the saved-accounts list (tests shorten it). */
		pollMs?: number;
	}

	let { onFinish, onCancel, pollMs = 3000 }: Props = $props();

	const access = getSettingsAccess();
	type Shell = 'powershell' | 'cmd' | 'posix';
	const SHELLS: { value: Shell; label: string }[] = [
		{ value: 'powershell', label: 'PowerShell' },
		{ value: 'cmd', label: 'Command Prompt' },
		{ value: 'posix', label: 'macOS / Linux' }
	];

	let login = $state<ClaudeSubscriptionLogin | null>(null);
	let starting = $state(false);
	let cancelling = $state(false);
	let error = $state('');
	let shell = $state<Shell>(
		typeof navigator !== 'undefined' && /Windows/i.test(navigator.userAgent) ? 'powershell' : 'posix'
	);
	let pollTimer: ReturnType<typeof setTimeout> | undefined;
	let disposed = false;
	/** The entry the worker kept when a cancel found the sign-in already finished. */
	let kept: string | null = null;

	function errorText(e: unknown, fallback: string): string {
		return claudeAccountError(e).message ?? getErrorMessage(e, fallback);
	}

	/** The add-another run's terminal chunk, in the shape the shared relay reads. */
	function chunk(next: ClaudeSubscriptionLogin | null | undefined): TerminalChunk {
		const t = next?.terminal_state;
		return t
			? { output: t.output, offset: t.offset, running: t.running, url: t.url, reset: true }
			: { output: '', offset: 0, running: true, url: null, reset: true };
	}

	const cli = new ClaudeCliLogin({
		start: async () => {
			const result = await addAnotherClaudeSubscription();
			absorb(result.login ?? result.accounts.login ?? null, result.accounts);
			return chunk(login);
		},
		cancel: async () => {
			kept = await cancelPending();
		}
	});

	/** Cancel the pending sign-in; resolves with the kept entry's id, if the CLI had finished. */
	async function cancelPending(): Promise<string | null> {
		const result = await cancelClaudeSubscriptionLogin();
		providers.setClaudeAccounts(result.accounts);
		return result.account_id ?? null;
	}

	function absorb(next: ClaudeSubscriptionLogin | null, accounts?: Parameters<typeof providers.setClaudeAccounts>[0]) {
		if (accounts) providers.setClaudeAccounts(accounts);
		login = next;
	}

	/** Save the CLI's own sign-in first, so it is not hidden once the new one is active. */
	async function keepDefault() {
		const accounts = providers.current.claude?.accounts;
		const signedIn = !!providers.current.claude?.cli.signed_in;
		if (!accounts?.default_in_use || !signedIn) return;
		if (accounts.subscription.some((a) => a.kind === 'default')) return;
		try {
			const result = await saveCurrentClaudeSubscription('');
			providers.setClaudeAccounts(result.accounts);
		} catch (e) {
			// Not signed in after all: nothing to keep. Anything else stops the
			// start, or the original sign-in would become unreachable.
			if (claudeAccountError(e).code !== 'not_signed_in') throw e;
		}
	}

	async function start() {
		if (starting) return;
		starting = true;
		error = '';
		try {
			await keepDefault();
			const result = await addAnotherClaudeSubscription();
			absorb(result.login ?? result.accounts.login ?? null, result.accounts);
			// The run already started; the shared terminal relay attaches to it.
			if (login?.terminal) cli.attach(chunk(login));
			schedulePoll();
		} catch (e) {
			error = errorText(e, 'Could not start the sign-in');
		} finally {
			starting = false;
		}
	}

	async function cancel() {
		if (cancelling) return;
		cancelling = true;
		clearTimeout(pollTimer);
		kept = null;
		try {
			// A running terminal's Stop already cancels the add-another sign-in.
			const stopped = cli.terminalOpen && cli.running;
			if (cli.terminalOpen) await cli.closeTerminal();
			if (login && !stopped) kept = await cancelPending().catch(() => null);
			login = null;
			const id = kept;
			kept = null;
			if (id) await finish(id);
			else onCancel();
		} finally {
			cancelling = false;
		}
	}

	/** The terminal's Stop or Close: end this sign-in and offer Start again. */
	async function terminalClosed() {
		clearTimeout(pollTimer);
		// Stop on a running terminal already cancelled it (and set `kept`).
		let id = kept;
		kept = null;
		if (!id && login) id = await cancelPending().catch(() => null);
		login = null;
		if (id) await finish(id);
	}

	function schedulePoll() {
		clearTimeout(pollTimer);
		if (!disposed) pollTimer = setTimeout(() => void poll(), pollMs);
	}

	async function poll() {
		try {
			const result = await getClaudeAccounts();
			if (disposed) return;
			const id = login?.id;
			const next = result.accounts.login ?? null;
			absorb(next, result.accounts);
			const saved = id ? result.accounts.subscription.find((a) => a.id === id) : undefined;
			if (!next && saved) {
				await finish(saved.id);
				return;
			}
			if (!next) return; // Cancelled elsewhere.
		} catch (e) {
			if (!disposed) error = errorText(e, 'Could not check the sign-in');
		}
		schedulePoll();
	}

	async function finish(id: string) {
		if (cli.terminalOpen) cli.dispose();
		await providers.loadClaude(true).catch(() => {});
		onFinish(claudeSubItem(id));
	}

	onMount(async () => {
		// Resume a sign-in started before a reload or from another view.
		try {
			const result = await getClaudeAccounts();
			if (disposed) return;
			const pending = result.accounts.login ?? null;
			absorb(pending, result.accounts);
			if (pending) {
				if (pending.terminal && pending.terminal_state) cli.attach(chunk(pending));
				schedulePoll();
			}
		} catch {
			// The first Start reports any error.
		}
	});

	onDestroy(() => {
		disposed = true;
		clearTimeout(pollTimer);
		cli.dispose();
	});

	const failed = $derived(login?.status === 'failed');
	// Only the shells this path can be quoted for (Command Prompt may be missing).
	const shells = $derived(login?.commands ? SHELLS.filter((s) => !!login?.commands?.[s.value]) : SHELLS);
	const shownShell = $derived(shells.some((s) => s.value === shell) ? shell : (shells[0]?.value ?? 'posix'));
</script>

{#if login && cli.terminalOpen}
	<RowGroup title="Sign in" bare>
		<ClaudeCliTerminal {cli} onClose={terminalClosed} />
	</RowGroup>
	<p class="pv-note">Becomes active after sign-in; the other subscription turns off. The Claude Code mode does not change.</p>
	{#if error}<p class="pv-msg err" role="alert">{error}</p>{/if}
	<div class="pv-acts">
		<button type="button" class="st-btn ghost" disabled={cancelling} onclick={cancel}>Cancel</button>
	</div>
{:else if login}
	<div class="st-card pv-card">
		<div class="pv-flow" data-testid="claude-add-subscription">
			{#if login.commands}
				<div class="pv-flow-head">Run this in a terminal on this worker, then sign in on Anthropic’s page</div>
				<Segmented options={shells} value={shownShell} label="Terminal" size="sm" onchange={(v) => (shell = v)} />
				<CodeBlock code={login.commands[shownShell] ?? ''} prompt />
			{:else}
				<div class="pv-flow-head">Signing in with <span class="st-mono">{login.command}</span> on this worker</div>
			{/if}
			{#if failed}
				<p class="pv-msg err" role="alert">{login.error || 'The sign-in did not finish. Try again or cancel it.'}</p>
				<div class="pv-row-inline">
					<button type="button" class="st-btn primary" disabled={starting} onclick={start}>Try again</button>
				</div>
			{:else}
				<div class="pv-wait" role="status"><span class="pv-spin" aria-hidden="true"></span>Waiting for the sign-in</div>
			{/if}
		</div>
	</div>
	<p class="pv-note">Becomes active after sign-in; the other subscription turns off. The Claude Code mode does not change.</p>
	{#if error}<p class="pv-msg err" role="alert">{error}</p>{/if}
	<div class="pv-acts">
		<button type="button" class="st-btn ghost" disabled={cancelling} onclick={cancel}>Cancel</button>
	</div>
{:else}
	<RowGroup title="Sign in">
		<SettingRow label="Sign in with another Claude account" hint="Becomes active after sign-in; the other subscription turns off">
			<button type="button" class="st-btn primary" disabled={starting || access.readOnly} onclick={start}
				><SquareTerminal />{starting ? 'Starting…' : 'Start sign-in'}</button
			>
		</SettingRow>
	</RowGroup>
	{#if error}<p class="pv-msg err" role="alert">{error}</p>{/if}
	<div class="pv-acts">
		<button type="button" class="st-btn ghost" disabled={cancelling} onclick={cancel}>Cancel</button>
	</div>
{/if}
