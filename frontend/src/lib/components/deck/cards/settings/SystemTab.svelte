<script lang="ts">
	import { Gauge, Users, FileText } from 'lucide-svelte';
	import RateLimitManager from '$lib/components/RateLimitManager.svelte';
	import ApiUserManager from '$lib/components/ApiUserManager.svelte';
	import DevicesSection from './DevicesSection.svelte';

	interface Props {
		// Security Instructions
		securityInstructions: string;
		onSecurityInstructionsChange: (value: string) => void;
		defaultSecurityInstructions: string;
		isSecurityInstructionsCustomized: boolean;
		securityInstructionsChanged: boolean;
		loadingSecurityInstructions: boolean;
		savingSecurityInstructions: boolean;
		securityInstructionsSuccess: string;
		securityInstructionsError: string;
		onSaveSecurityInstructions: () => void;
		onRevertSecurityInstructions: () => void;
	}

	let {
		securityInstructions,
		onSecurityInstructionsChange,
		isSecurityInstructionsCustomized,
		securityInstructionsChanged,
		loadingSecurityInstructions,
		savingSecurityInstructions,
		securityInstructionsSuccess,
		securityInstructionsError,
		onSaveSecurityInstructions,
		onRevertSecurityInstructions,
	}: Props = $props();
</script>

<!-- Rate limits -->
<div class="set-group">
	<div class="set-grouphead">
		<h3><Gauge />Rate limits</h3>
		<p>Throttle how often sessions can call the model.</p>
	</div>
	<div class="sg-body"><RateLimitManager /></div>
</div>

<!-- Devices (cluster registry) — the primary cross-install / offload UI -->
<DevicesSection />

<!-- API users -->
<div class="set-group">
	<div class="set-grouphead">
		<h3><Users />API users</h3>
		<p>Create and revoke API keys for programmatic access.</p>
	</div>
	<div class="sg-body"><ApiUserManager /></div>
</div>

<!-- System prompt -->
<div class="set-group">
	<div class="set-grouphead">
		<h3><FileText />System prompt</h3>
		<p>Prepended to every system prompt sent to Claude, across all profiles.</p>
	</div>
	{#if loadingSecurityInstructions}
		<div class="sg-body"><p class="muted">Loading…</p></div>
	{:else}
		<div class="set-row stack">
			<div class="lab">
				<h4>Custom instructions</h4>
				<p>Applies globally. Leave empty to use the built-in default.</p>
			</div>
			<div class="ctl">
				<textarea
					value={securityInstructions}
					oninput={(e) => onSecurityInstructionsChange(e.currentTarget.value)}
					class="fld"
					placeholder="Enter system prompt instructions…"
					rows="12"
				></textarea>
				<div class="sp-foot">
					<div class="sp-status">
						{#if isSecurityInstructionsCustomized}<span class="b b-on">Customized</span>{:else}<span class="b b-muted">Using default</span>{/if}
						{#if securityInstructionsChanged}<span class="b b-warn">Unsaved changes</span>{/if}
					</div>
					<div class="sp-acts">
						<button
							type="button"
							class="ed-btn"
							onclick={onRevertSecurityInstructions}
							disabled={!isSecurityInstructionsCustomized || savingSecurityInstructions}
							title="Revert to default instructions"
						>Revert to default</button>
						<button
							type="button"
							class="ed-btn primary"
							onclick={onSaveSecurityInstructions}
							disabled={!securityInstructionsChanged || savingSecurityInstructions}
						>{savingSecurityInstructions ? 'Saving…' : 'Save changes'}</button>
					</div>
				</div>
				{#if securityInstructionsError}<p class="msg err">{securityInstructionsError}</p>{/if}
				{#if securityInstructionsSuccess}<p class="msg ok">{securityInstructionsSuccess}</p>{/if}
			</div>
		</div>
	{/if}
</div>

<style>
	.sg-body {
		padding: 14px 18px 16px;
	}
	.muted {
		margin: 0;
		font-size: 13px;
		color: var(--muted-foreground);
	}
	.sp-foot {
		display: flex;
		align-items: center;
		justify-content: space-between;
		gap: 12px;
		flex-wrap: wrap;
		margin-top: 10px;
	}
	.sp-status {
		display: flex;
		gap: 6px;
		flex-wrap: wrap;
	}
	.sp-acts {
		display: flex;
		gap: 8px;
	}
	.b {
		font-family: var(--font-mono, monospace);
		font-size: 10px;
		font-weight: 600;
		letter-spacing: 0.04em;
		text-transform: uppercase;
		padding: 2px 7px;
		border-radius: 999px;
	}
	.b-on {
		color: var(--primary-foreground);
		background: var(--primary);
	}
	.b-muted {
		color: var(--muted-foreground);
		background: var(--muted);
	}
	.b-warn {
		color: var(--warning, #b45309);
		background: color-mix(in oklch, var(--warning, #b45309) 16%, transparent);
	}
	.msg {
		margin: 8px 0 0;
		font-size: 12.5px;
	}
	.msg.ok {
		color: var(--success, #16a34a);
	}
	.msg.err {
		color: var(--destructive);
	}
</style>
