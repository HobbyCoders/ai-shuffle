<script lang="ts">
	/**
	 * AvatarPicker: an agent's shape and colour, inside the Identity card.
	 *
	 * The shape saves on its own through the narrow identity PATCH, never
	 * through the agent's full-replace PUT. The colour is an agent field
	 * (`avatar_color`) that rides the agent's autosave, so it is only offered
	 * when the agent API returns that field. Built-in avatars are locked.
	 */
	import { tick } from 'svelte';
	import { Check, RefreshCw } from 'lucide-svelte';
	import { AVATARS, updateAgentIdentity, assistantError, type AvatarId } from '$lib/api/assistant';
	import { assistantState, agentIdentities } from '$lib/stores/assistant';
	import { sessionOwnerEpoch } from '$lib/stores/session-owner';
	import AgentAvatar from '$lib/components/assistant/AgentAvatar.svelte';
	import { AVATAR_COLORS, AVATAR_COLOR_LABELS, avatarColorToken, type AvatarColor } from '$lib/components/assistant/avatar-colors';

	interface Props {
		agentId: string;
		builtin?: boolean;
		/** Current colour name; null for the default. */
		color?: string | null;
		/** False hides the colour choice (the agent API has no avatar_color yet). */
		colorSupported?: boolean;
		onColor?: (color: AvatarColor) => void;
		/** Identities failed to load; the parent offers a retry. */
		loadError?: string;
		onRetry?: () => void;
	}

	let {
		agentId,
		builtin = false,
		color = null,
		colorSupported = false,
		onColor,
		loadError = '',
		onRetry
	}: Props = $props();

	const current = $derived(($agentIdentities.find((a) => a.id === agentId)?.avatar_id ?? 'robot') as AvatarId);
	let saving = $state(false);
	let error = $state('');
	let saved = $state(false);
	/** The shape whose save failed, for Retry. */
	let failed = $state<AvatarId | null>(null);
	let colors: HTMLDivElement | undefined = $state();
	const owner = sessionOwnerEpoch();

	async function choose(avatar_id: AvatarId) {
		if (saving || builtin || avatar_id === current) return;
		const target = agentId;
		saving = true;
		error = '';
		saved = false;
		failed = null;
		try {
			const result = await updateAgentIdentity(target, { avatar_id });
			if (owner !== sessionOwnerEpoch() || target !== agentId) return;
			agentIdentities.update((rows) =>
				rows.some((row) => row.id === target) ? rows.map((row) => (row.id === target ? result : row)) : [...rows, result]
			);
			assistantState.update((state) => (state?.agent_id === target ? { ...state, avatar_id, agent: result } : state));
			saved = true;
		} catch (e) {
			if (owner === sessionOwnerEpoch()) {
				error = assistantError(e);
				failed = avatar_id;
			}
		} finally {
			saving = false;
		}
	}

	/** Arrow keys move the colour choice, as in any radio group. */
	async function colorKey(event: KeyboardEvent) {
		const keys = ['ArrowRight', 'ArrowDown', 'ArrowLeft', 'ArrowUp'];
		if (!keys.includes(event.key) || builtin) return;
		event.preventDefault();
		const i = Math.max(0, AVATAR_COLORS.indexOf(color as AvatarColor));
		const step = event.key === 'ArrowRight' || event.key === 'ArrowDown' ? 1 : -1;
		const next = AVATAR_COLORS[(i + step + AVATAR_COLORS.length) % AVATAR_COLORS.length];
		onColor?.(next);
		await tick();
		colors?.querySelector<HTMLElement>(`[data-color="${next}"]`)?.focus();
	}
</script>

<div class="pick">
	{#if loadError}
		<div role="alert" class="pick-err">
			{loadError}
			{#if onRetry}<button type="button" class="st-btn sm" onclick={onRetry}><RefreshCw />Retry</button>{/if}
		</div>
	{:else}
		<div>
			<div class="pick-l" id="{agentId}-shape-l">
				Shape
				{#if saved}<span class="pick-saved" role="status"><Check />Saved</span>{/if}
			</div>
			<div class="shapes" role="group" aria-labelledby="{agentId}-shape-l">
				{#each AVATARS as avatar (avatar)}
					<button
						type="button"
						aria-label={`${avatar} avatar`}
						aria-pressed={current === avatar}
						title={avatar}
						disabled={builtin || saving}
						onclick={() => choose(avatar)}
					>
						<AgentAvatar {avatar} size={40} color={builtin ? null : color} neutral={builtin} />
					</button>
				{/each}
			</div>
			{#if error}
				<div role="alert" class="pick-err">
					{error}
					{#if failed}{@const retry = failed}<button type="button" class="st-btn sm" onclick={() => choose(retry)}><RefreshCw />Retry</button>{/if}
				</div>
			{/if}
		</div>
		{#if colorSupported && !builtin}
			<div>
				<div class="pick-l" id="{agentId}-color-l">Colour</div>
				<div class="swatches" role="radiogroup" aria-labelledby="{agentId}-color-l" bind:this={colors}>
					{#each AVATAR_COLORS as c, i (c)}
						{@const on = color === c}
						<button
							type="button"
							role="radio"
							data-color={c}
							aria-checked={on}
							aria-label={AVATAR_COLOR_LABELS[c]}
							title={AVATAR_COLOR_LABELS[c]}
							tabindex={on || (!AVATAR_COLORS.includes(color as AvatarColor) && i === 0) ? 0 : -1}
							onclick={() => onColor?.(c)}
							onkeydown={colorKey}
						><span class="sw" style:background={avatarColorToken(c)}></span></button>
					{/each}
				</div>
			</div>
		{/if}
	{/if}
</div>

<style>
	.pick {
		display: grid;
		gap: 14px;
	}
	.pick-l {
		display: flex;
		align-items: center;
		gap: 8px;
		font-size: 12px;
		color: var(--st-mfg);
		margin-bottom: 6px;
	}
	.pick-saved {
		display: inline-flex;
		align-items: center;
		gap: 3px;
		color: var(--st-primary);
		font-size: 11.5px;
	}
	.pick-saved :global(svg.lucide) {
		width: 12px;
		height: 12px;
	}
	.shapes {
		display: grid;
		grid-template-columns: repeat(8, minmax(0, 1fr));
		gap: 6px;
	}
	.shapes button {
		display: grid;
		place-items: center;
		padding: 4px;
		aspect-ratio: 1;
		border: 1.5px solid transparent;
		border-radius: 12px;
		background: none;
		transition:
			border-color 0.12s,
			background 0.12s,
			transform 0.12s;
	}
	.shapes button:hover:not(:disabled) {
		background: var(--st-hover);
		transform: translateY(-1px);
	}
	.shapes button[aria-pressed='true'] {
		border-color: var(--st-fg);
	}
	.shapes button:disabled {
		opacity: 0.55;
	}
	.shapes button[aria-pressed='true']:disabled {
		opacity: 1;
	}
	.swatches {
		display: flex;
		gap: 8px;
		flex-wrap: wrap;
	}
	.swatches button {
		width: 28px;
		height: 28px;
		border-radius: 50%;
		border: 0;
		padding: 0;
		background: none;
		display: grid;
		place-items: center;
	}
	/* The swatch takes the avatar's own colour token, so it matches exactly. */
	.sw {
		display: block;
		width: 26px;
		height: 26px;
		border-radius: 50%;
		box-shadow:
			0 0 0 2px var(--st-card),
			0 0 0 3px transparent;
		transition: box-shadow 0.12s;
	}
	.swatches button[aria-checked='true'] .sw {
		box-shadow:
			0 0 0 2px var(--st-card),
			0 0 0 4px var(--st-fg);
	}
	.swatches button:focus-visible {
		outline-offset: 3px;
		border-radius: 50%;
	}
	.pick-err {
		display: flex;
		align-items: center;
		gap: 10px;
		margin-top: 8px;
		font-size: 12.5px;
		color: var(--st-destructive);
	}
	@container (max-width: 560px) {
		.shapes {
			grid-template-columns: repeat(4, minmax(0, 1fr));
			gap: 8px;
		}
	}
	@media (prefers-reduced-motion: reduce) {
		.shapes button,
		.sw {
			transition: none;
		}
	}
</style>
