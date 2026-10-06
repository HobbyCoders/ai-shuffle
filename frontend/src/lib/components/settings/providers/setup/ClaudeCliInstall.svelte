<script lang="ts">
	import { onMount, onDestroy } from 'svelte';
	import { desktop } from '$lib/services/desktop';
	import '../../shell/settings.css';
	import '../providers.css';
	import { ClaudeCliInstallState } from '../claude-cli-install.svelte';
	import RowGroup from '../../rows/RowGroup.svelte';
	import SettingRow from '../../rows/SettingRow.svelte';
	import Switch from '../../rows/Switch.svelte';
	import ValueText from '../../rows/ValueText.svelte';
	import CodeBlock from '../../rows/CodeBlock.svelte';
	import ConfirmDialog from '$lib/components/ui/ConfirmDialog.svelte';

	let { manage = false, isAdmin = true, onFinished = () => {}, onDismiss = () => {} }: {
		manage?: boolean; isAdmin?: boolean; onFinished?: () => void | Promise<void>;
		/** The owner dismissed the "removed" confirmation. */
		onDismiss?: () => void;
	} = $props();
	const installer = new ClaudeCliInstallState(() => onFinished());
	const status = $derived(installer.status);
	const disabled = $derived(!isAdmin || installer.busy || installer.running);
	let confirming = $state(false);
	// A removal started from this view keeps its confirmation on screen after
	// the CLI is gone, until the owner dismisses it or leaves the page (#1890).
	let removedHere = $state(false);
	const removedNotice = $derived(manage && removedHere && !!status && !status.installed
		&& status.job?.kind === 'remove' && status.job.state === 'succeeded');
	function dismissRemoved() { removedHere = false; onDismiss(); }
	function remove() { confirming = false; removedHere = true; void installer.start('remove'); }
	const manual = $derived(status?.platform !== 'docker' && (desktop?.platform === 'win32' || (!desktop && typeof navigator !== 'undefined' && /Win/i.test(navigator.platform)))
		? 'irm https://claude.ai/install.ps1 | iex' : 'curl -fsSL https://claude.ai/install.sh | bash');
	onMount(() => { if (isAdmin) void installer.load(); });
	onDestroy(() => installer.dispose());
</script>

<div class="settings-shell cli-install-host">
{#if !isAdmin}
	<p class="pv-msg">The worker owner can install Claude Code here.</p>
{:else if !status}
	<p class="pv-msg" role="status">{installer.error || 'Checking Claude Code installation'}</p>
	{#if installer.error}<button type="button" class="st-btn" onclick={() => installer.load()}>Try again</button>{/if}
{:else if removedNotice}
	<div class="cli-install" data-testid="claude-cli-install">
		<p class="pv-msg" role="status">{status.job?.message || 'Claude Code removed.'}</p>
		<button type="button" class="st-btn" onclick={dismissRemoved}>Dismiss</button>
	</div>
{:else if manage || !status.installed || installer.running || status.job?.state === 'failed'}
	<div class="cli-install" data-testid="claude-cli-install" aria-busy={installer.busy || installer.running}>
		{#if installer.running}
			<div aria-live="polite" role="status" class="cli-job">
				<p>Claude Code {status.job?.kind} is running. {installer.elapsed} elapsed.</p>
				{#if status.job?.output_tail}
					<!-- svelte-ignore a11y_no_noninteractive_tabindex (Keyboard users need to scroll the capped output.) -->
					<pre tabindex="0" role="region" aria-label="Installer output">{status.job.output_tail}</pre>
				{/if}
			</div>
			<button type="button" class="st-btn" disabled>Working</button>
		{:else if status.job?.state === 'failed'}
			<p class="pv-msg err" role="alert">{status.job.message || 'The Claude Code job failed.'}</p>
			{#if status.job.output_tail}
				<!-- svelte-ignore a11y_no_noninteractive_tabindex (Keyboard users need to scroll the capped output.) -->
				<pre class="cli-output" tabindex="0" role="region" aria-label="Installer output">{status.job.output_tail}</pre>
			{/if}
			{#if status.job.kind === 'remove' ? status.can_remove : status.can_install}<button type="button" class="st-btn primary" disabled={disabled} onclick={() => { if (status.job!.kind === 'remove') confirming = true; else void installer.start(status.job!.kind); }}>Try again</button>{/if}
			<p class="pv-msg">Or run Anthropic's installer on this worker{manual.startsWith('irm') ? ' in PowerShell' : ''}.</p>
			<CodeBlock code={manual} label="Copy install command" />
		{:else if !status.installed}
			{#if status.can_install}
				<p>Claude's subscription sign-in uses Anthropic's Claude Code CLI. It is not installed on this worker.</p>
				<button type="button" class="st-btn primary" disabled={disabled} onclick={() => installer.start('install')}>Install Claude Code</button>
				<p class="cli-helper">AI Shuffle downloads it from Anthropic with Anthropic's own installer. Nothing is bundled.</p>
			{:else}
				<p>This {status.platform} worker cannot run the installer from AI Shuffle. Install Claude Code manually on the worker{manual.startsWith('irm') ? ' in PowerShell' : ''}.</p>
				<CodeBlock code={manual} label="Copy install command" />
			{/if}
		{/if}
		{#if manage && status.installed}
			<RowGroup title="Claude Code CLI">
				<SettingRow label="Version"><ValueText mono value={status.version ?? 'Not reported'} /></SettingRow>
				<SettingRow label="Path"><ValueText mono value={status.path ?? 'Not reported'} /></SettingRow>
				<SettingRow id="providers.cliKeepInstalled">
					<Switch checked={status.keep_installed} disabled={disabled} onchange={(v) => installer.keep(v)} />
				</SettingRow>
				<p class="cli-helper keep-helper">When on, this worker reinstalls the CLI at start if it is missing. When off, starting the worker never downloads it.</p>
				<SettingRow id="providers.cliUpdate">
					<button type="button" class="st-btn" disabled={disabled || !status.can_install} onclick={() => installer.start('update')}>Check for update</button>
				</SettingRow>
				{#if status.can_remove}<SettingRow id="providers.cliRemove">
					<button type="button" class="st-btn" disabled={disabled} onclick={() => confirming = true}>Remove</button>
				</SettingRow>{/if}
			</RowGroup>
			{#if status.job?.state === 'succeeded' && status.job.message && !installer.error}<p class="pv-msg" role="status">{status.job.message}</p>{/if}
		{/if}
		{#if installer.error}<p class="pv-msg err" role="alert">{installer.error}</p><button type="button" class="st-btn" onclick={() => installer.load()}>Check again</button>{/if}
	</div>
{/if}
</div>
<ConfirmDialog open={confirming} title="Remove Claude Code CLI?" confirmLabel="Remove" danger
	message="Your sign-in and settings in the Claude folder are kept. Claude models stop working until Claude Code is installed again."
	onCancel={() => confirming = false} onConfirm={remove} />

<style>
	.cli-install-host { display: block; height: auto; grid-template-columns: none; }
	.cli-install { min-width: 0; padding: 16px; display: grid; gap: 12px; font-size: 13px; }
	.cli-install p { margin: 0; line-height: 1.6; }
	.cli-helper { color: var(--st-mfg, var(--muted-foreground)); font-size: 12px; }
	.cli-install > button { justify-self: start; min-height: 44px; }
	.cli-install .keep-helper { padding: 0 16px 12px; }
	/* Phone widths and touch screens: 44px targets, not the shell's 30px. */
	@media (max-width: 600px), (pointer: coarse) {
		.cli-install :global(.st-btn) { height: auto; min-height: 44px; }
	}
	.cli-output, .cli-job pre { margin: 8px 0 0; padding: 12px; max-height: 160px; overflow: auto; white-space: pre-wrap; overflow-wrap: anywhere; font: 12px/1.6 var(--st-mono, monospace); background: var(--st-code-bg, var(--muted)); border-radius: var(--st-radius-md, 8px); }
</style>
