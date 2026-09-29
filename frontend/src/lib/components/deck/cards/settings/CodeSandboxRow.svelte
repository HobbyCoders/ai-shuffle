<script lang="ts">
	/**
	 * CodeSandboxRow — whether agents' Python cells run inside the bubblewrap
	 * sandbox on this install (GET /deployment → code_sandbox). Without it the
	 * kernel's only boundary is its in-process guard, so Ask mode prompts before
	 * each cell; the row says so and names the reason the self-test recorded.
	 */
	import { ShieldCheck, ShieldOff } from 'lucide-svelte';
	import { layout } from '$lib/stores/layout.svelte';
	import type { CodeSandboxStatus } from '$lib/api/client';

	interface Props {
		// null while loading (or when the worker predates the field).
		status: CodeSandboxStatus | null;
	}

	let { status }: Props = $props();

	// The install guide's section "The code-mode sandbox".
	const DOCS_ITEM = 'getting-started/install-docker-compose';

	const checking = $derived(!status || status.state === 'checking');
	const label = $derived(
		checking
			? 'Code sandbox: checking…'
			: status!.active
				? 'Code sandbox: active'
				: status!.state === 'off'
					? 'Code sandbox: off'
					: 'Code sandbox: unavailable'
	);
</script>

<div class="set-group">
	<div class="set-grouphead">
		<h3>{#if status?.active}<ShieldCheck />{:else}<ShieldOff />{/if}Code sandbox</h3>
	</div>
	<div class="set-row stack sandbox-row">
		<div class="lab">
			<h4>Agents' Python code</h4>
		</div>
		<div class="ctl">
			<p class="state" class:on={status?.active} class:off={status && !status.active && !checking} data-testid="code-sandbox-state">{label}</p>
			{#if status && !status.active && !checking && status.reason}
				<p class="reason" data-testid="code-sandbox-reason">{status.reason}</p>
			{/if}
			<p class="hint">
				The OS sandbox isolates agents' Python code from this worker's files and network.
				Tool calls still follow the session's permission mode.
			</p>
			<p class="hint" data-testid="code-sandbox-permissions">
				When the sandbox is unavailable or off, Ask mode prompts before each Python cell.
				Bypass skips ordinary tool approvals; private access and other explicit consent requests still require your approval.
			</p>
			<p class="hint">
				The current OS sandbox uses bubblewrap on Linux; it is not available on Windows or macOS.
				<button type="button" class="doclink" onclick={() => layout.openFeaturePage('docs', DOCS_ITEM)}>The code-mode sandbox in the install guide</button>
			</p>
		</div>
	</div>
</div>

<style>
	.sandbox-row .ctl {
		min-width: 0;
		max-width: 100%;
	}
	.state,
	.reason,
	.hint {
		line-height: 1.5;
		overflow-wrap: anywhere;
	}
	.state {
		margin: 0;
		font-size: 13px;
		font-weight: 600;
		color: var(--foreground);
	}
	.state.on {
		color: var(--success, #16a34a);
	}
	.state.off {
		color: var(--warning, #b45309);
	}
	.reason {
		margin: 6px 0 0;
		font-size: 12.5px;
		font-family: var(--font-mono, monospace);
		color: var(--muted-foreground);
		overflow-wrap: anywhere;
	}
	.hint {
		margin: 8px 0 0;
		font-size: 12.5px;
		color: var(--muted-foreground);
	}
	.doclink {
		padding: 0;
		border: 0;
		background: none;
		font: inherit;
		color: var(--primary);
		text-decoration: underline;
		cursor: pointer;
	}
</style>
