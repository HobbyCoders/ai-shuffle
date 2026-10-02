<script lang="ts">
	import { watchResources } from '$lib/services/watch-resources';
	import { apiBase, withAuth } from '$lib/api/runtime';
	/**
	 * MediaToolsSection — the "Media & tools" settings group (Chunk 8, plan §1.5).
	 *
	 * Folds the two residual Connections groups (Media & tools / fal.ai and
	 * Integrations / GitHub) into a single group, and adds a "Manage media models"
	 * keycard row that opens `ManageMediaModelsDrawer` — the new home for the old
	 * AI-models tab (ruling D5). Replaces the residual `ConnectionsSection`.
	 *
	 * Card order: fal.ai API key, GitHub, AI media models — the credentials
	 * first, then the model catalog the fal key unlocks.
	 *
	 * The fal.ai keycard mechanics are lifted verbatim from ConnectionsSection
	 * (self-contained local state + raw fetch to /settings/integrations/fal-ai).
	 * GitHub uses the same self-contained connection UI as the setup wizard.
	 *
	 * Rendered inside `.ed-content` like ProvidersSection's other groups, so the
	 * shell `.set-group` / `.ed-note` frame classes still apply; the keycard CSS is
	 * copied here so the component is self-contained.
	 */
	import { onMount } from 'svelte';
	import { Image } from 'lucide-svelte';
	import GitHubConnection from '$lib/components/GitHubConnection.svelte';
	import ManageMediaModelsDrawer from './ManageMediaModelsDrawer.svelte';



	// ----- fal.ai key (self-contained, lifted verbatim from ConnectionsSection) -----
	let falKeySet = $state(false);
	let falKeyMasked = $state('');
	let falKeyInput = $state('');
	let falSaving = $state(false);
	let falError = $state('');
	let falSuccess = $state('');

	// The fal key comes from the /settings/integrations payload.
	async function loadKeyStatus() {
		try {
			const res = await fetch(`${apiBase()}/settings/integrations`, withAuth({ }));
			if (!res.ok) return;
			const data = await res.json();
			falKeySet = !!data.fal_api_key_set;
			falKeyMasked = data.fal_api_key_masked ?? '';
		} catch (e) {
			// Silent — non-critical, user can retry by reopening settings
			console.warn('Failed to load credential status', e);
		}
	}

	async function saveFalKey() {
		if (!falKeyInput.trim()) return;
		falSaving = true;
		falError = '';
		falSuccess = '';
		try {
			const res = await fetch(`${apiBase()}/settings/integrations/fal-ai`, withAuth({
				method: 'POST',
				headers: { 'content-type': 'application/json' },
				body: JSON.stringify({ api_key: falKeyInput.trim() }),
			}));
			if (!res.ok) {
				const body = await res.json().catch(() => ({ }));
				falError = body.detail ?? `Save failed (${res.status})`;
				return;
			}
			const data = await res.json();
			falKeySet = true;
			falKeyMasked = data.masked_key ?? '';
			falKeyInput = '';
			falSuccess = data.valid ? 'Saved & verified' : 'Saved (could not verify)';
			setTimeout(() => (falSuccess = ''), 2500);
		} catch (e) {
			falError = e instanceof Error ? e.message : 'Network error';
		} finally {
			falSaving = false;
		}
	}

	async function removeFalKey() {
		falSaving = true;
		falError = '';
		falSuccess = '';
		try {
			const res = await fetch(`${apiBase()}/settings/integrations/fal-ai`, withAuth({
				method: 'DELETE',
			}));
			if (!res.ok) {
				falError = `Remove failed (${res.status})`;
				return;
			}
			falKeySet = false;
			falKeyMasked = '';
			falSuccess = 'Removed';
			setTimeout(() => (falSuccess = ''), 2500);
		} catch (e) {
			falError = e instanceof Error ? e.message : 'Network error';
		} finally {
			falSaving = false;
		}
	}

	// Fetch on mount. No reactivity needed — key status only changes via this
	// component's own save/remove actions.
	onMount(() => {
		loadKeyStatus();
	});

	// ----- Manage media models drawer (fold of the old AI-models tab, ruling D5) -----
	let manageMediaOpen = $state(false);
	watchResources(['settings'], () => loadKeyStatus());
</script>

<!-- Media & tools -->
<div class="set-group">
	<div class="set-grouphead">
		<h3><Image />Media &amp; tools</h3>
	</div>

	<!-- fal.ai key -->
	<div class="keycard">
		<div class="kc-ic">
			<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" aria-hidden="true">
				<rect x="3" y="3" width="18" height="18" rx="2"/>
				<circle cx="9" cy="9" r="2"/>
				<path d="m21 15-3.6-3.6a2 2 0 0 0-2.8 0L6 20"/>
			</svg>
		</div>
		<div class="kc-body">
			<h4>fal.ai API key
				{#if falKeySet}<span class="sb ok">Set</span>{:else}<span class="sb off">Optional</span>{/if}
				{#if falKeySet}<code class="kc-val">{falKeyMasked}</code>{/if}
			</h4>
		</div>
		<div class="kc-acts">
			{#if falKeySet}
				<button type="button" class="ed-btn danger" onclick={removeFalKey} disabled={falSaving}>Remove</button>
			{:else}
				<input
					type="password"
					aria-label="fal.ai API key"
					autocomplete="off"
					value={falKeyInput}
					oninput={(e) => (falKeyInput = e.currentTarget.value)}
					placeholder="Enter your fal.ai API key"
					class="inp kc-in"
				/>
				<button type="button" class="ed-btn primary" onclick={saveFalKey} disabled={falSaving || !falKeyInput.trim()}>
					{falSaving ? '…' : 'Save'}
				</button>
			{/if}
		</div>
	</div>

	{#if falError}<div class="kc-foot err" role="alert">{falError}</div>{/if}
	{#if falSuccess}<div class="kc-foot ok">{falSuccess}</div>{/if}

	<GitHubConnection embedded />

	<!-- AI media models (opens the manage-media-models drawer) -->
	<div class="keycard">
		<div class="kc-ic">
			<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true">
				<rect x="3" y="3" width="7" height="7" rx="1"/>
				<rect x="14" y="3" width="7" height="7" rx="1"/>
				<rect x="3" y="14" width="7" height="7" rx="1"/>
				<rect x="14" y="14" width="7" height="7" rx="1"/>
			</svg>
		</div>
		<div class="kc-body">
			<h4>AI media models</h4>
		</div>
		<div class="kc-acts">
			<button type="button" class="ed-btn" onclick={() => (manageMediaOpen = true)}>Manage</button>
		</div>
	</div>

</div>

{#if manageMediaOpen}
	<ManageMediaModelsDrawer onClose={() => (manageMediaOpen = false)} />
{/if}

<style>
	.keycard {
		display: flex;
		gap: 10px;
		align-items: center;
		padding: 10px 14px;
		border-bottom: 1px solid var(--border);
	}
	.keycard:last-child {
		border-bottom: none;
	}
	.kc-ic {
		flex-shrink: 0;
		width: 20px;
		height: 24px;
		display: grid;
		place-items: center;
		color: var(--muted-foreground);
	}
	.kc-ic :global(svg) {
		width: 20px;
		height: 20px;
	}
	.kc-body {
		flex: 1;
		min-width: 0;
	}
	.kc-body h4 {
		margin: 0;
		font-size: 13px;
		font-weight: 600;
		color: var(--foreground);
		display: flex;
		align-items: center;
		gap: 8px;
		flex-wrap: wrap;
	}
	.kc-val {
		font-family: var(--font-mono, monospace);
		font-size: 11.5px;
		color: var(--muted-foreground);
	}
	.kc-acts {
		flex-shrink: 0;
		display: flex;
		gap: 8px;
		align-items: center;
		align-self: center;
		flex-wrap: wrap;
		justify-content: flex-end;
	}
	/* Every action button reads the same width so the column lines up down the
	   group, whatever the label says. */
	.kc-acts .ed-btn {
		display: inline-flex;
		align-items: center;
		justify-content: center;
		min-width: 104px;
		text-align: center;
	}
	.kc-acts .kc-in {
		width: 200px;
		min-width: 0;
	}
	.sb {
		font-family: var(--font-mono, monospace);
		font-size: 10px;
		font-weight: 600;
		letter-spacing: 0.04em;
		text-transform: uppercase;
		padding: 2px 7px;
		border-radius: 999px;
		line-height: 1.5;
	}
	.sb.ok {
		color: var(--success, #16a34a);
		background: color-mix(in oklch, var(--success, #16a34a) 14%, transparent);
	}
	.sb.off {
		color: var(--muted-foreground);
		background: var(--muted);
	}
	.kc-foot {
		padding: 6px 14px 10px;
		font-size: 12.5px;
	}
	.kc-foot.ok {
		color: var(--success, #16a34a);
	}
	.kc-foot.err {
		color: var(--destructive);
	}

	@media (max-width: 560px) {
		/* Let the card wrap so the action area drops onto its own full-width
		   row. Without this the flex-shrink:0 actions column demands 100% width
		   in the same row and crushes the body text to one word per line. */
		.keycard {
			flex-wrap: wrap;
			padding-left: 12px;
			padding-right: 12px;
		}
		.kc-acts:has(input) {
			width: 100%;
			justify-content: flex-start;
		}
		.kc-acts .kc-in {
			width: 100%;
		}
	}
</style>
