<script lang="ts">
	/**
	 * ToolIcon — the per-tool glyph, shared by the main feed's tool cards and the
	 * subagent card's child rows (which previously drew one hard-coded cog for
	 * every tool). The mapping lives in `$lib/utils/tool-icons`.
	 */
	import { getToolIconType } from '$lib/utils/tool-icons';

	interface Props {
		/** Raw tool name (MCP-prefixed names resolve on their suffix). */
		name: string;
		/** Extra classes appended to the base sizing/colour classes. */
		class?: string;
	}

	let { name, class: extraClass = '' }: Props = $props();

	const icon = $derived(getToolIconType(name));
	const cls = $derived(`w-4 h-4 text-primary shrink-0 ${extraClass}`.trim());
</script>

{#if icon === 'image'}
	<svg class={cls} fill="none" stroke="currentColor" viewBox="0 0 24 24" stroke-width="2">
		<rect x="3" y="3" width="18" height="18" rx="2" ry="2"/>
		<circle cx="8.5" cy="8.5" r="1.5"/>
		<polyline points="21 15 16 10 5 21"/>
	</svg>
{:else if icon === 'video'}
	<svg class={cls} fill="none" stroke="currentColor" viewBox="0 0 24 24" stroke-width="2">
		<polygon points="23 7 16 12 23 17 23 7"/>
		<rect x="1" y="5" width="15" height="14" rx="2" ry="2"/>
	</svg>
{:else if icon === 'cube'}
	<svg class={cls} fill="none" stroke="currentColor" viewBox="0 0 24 24" stroke-width="2">
		<path d="M21 16V8a2 2 0 0 0-1-1.73l-7-4a2 2 0 0 0-2 0l-7 4A2 2 0 0 0 3 8v8a2 2 0 0 0 1 1.73l7 4a2 2 0 0 0 2 0l7-4A2 2 0 0 0 21 16z"/>
		<polyline points="3.27 6.96 12 12.01 20.73 6.96"/>
		<line x1="12" y1="22.08" x2="12" y2="12"/>
	</svg>
{:else if icon === 'file'}
	<svg class={cls} fill="none" stroke="currentColor" viewBox="0 0 24 24" stroke-width="2">
		<path d="M14 2H6a2 2 0 0 0-2 2v16a2 2 0 0 0 2 2h12a2 2 0 0 0 2-2V8z"/>
		<polyline points="14 2 14 8 20 8"/>
		<line x1="16" y1="13" x2="8" y2="13"/>
		<line x1="16" y1="17" x2="8" y2="17"/>
	</svg>
{:else if icon === 'pencil'}
	<svg class={cls} fill="none" stroke="currentColor" viewBox="0 0 24 24" stroke-width="2">
		<path d="M17 3a2.828 2.828 0 1 1 4 4L7.5 20.5 2 22l1.5-5.5L17 3z"/>
	</svg>
{:else if icon === 'search'}
	<svg class={cls} fill="none" stroke="currentColor" viewBox="0 0 24 24" stroke-width="2">
		<circle cx="11" cy="11" r="8"/>
		<line x1="21" y1="21" x2="16.65" y2="16.65"/>
	</svg>
{:else if icon === 'terminal'}
	<svg class={cls} fill="none" stroke="currentColor" viewBox="0 0 24 24" stroke-width="2">
		<polyline points="4 17 10 11 4 5"/>
		<line x1="12" y1="19" x2="20" y2="19"/>
	</svg>
{:else if icon === 'globe'}
	<svg class={cls} fill="none" stroke="currentColor" viewBox="0 0 24 24" stroke-width="2">
		<circle cx="12" cy="12" r="10"/>
		<line x1="2" y1="12" x2="22" y2="12"/>
		<path d="M12 2a15.3 15.3 0 0 1 4 10 15.3 15.3 0 0 1-4 10 15.3 15.3 0 0 1-4-10 15.3 15.3 0 0 1 4-10z"/>
	</svg>
{:else if icon === 'bot'}
	<svg class={cls} fill="none" stroke="currentColor" viewBox="0 0 24 24" stroke-width="2">
		<rect x="3" y="11" width="18" height="10" rx="2"/>
		<circle cx="12" cy="5" r="2"/>
		<path d="M12 7v4"/>
		<line x1="8" y1="16" x2="8" y2="16"/>
		<line x1="16" y1="16" x2="16" y2="16"/>
	</svg>
{:else if icon === 'list'}
	<svg class={cls} fill="none" stroke="currentColor" viewBox="0 0 24 24" stroke-width="2">
		<line x1="8" y1="6" x2="21" y2="6"/>
		<line x1="8" y1="12" x2="21" y2="12"/>
		<line x1="8" y1="18" x2="21" y2="18"/>
		<line x1="3" y1="6" x2="3.01" y2="6"/>
		<line x1="3" y1="12" x2="3.01" y2="12"/>
		<line x1="3" y1="18" x2="3.01" y2="18"/>
	</svg>
{:else if icon === 'clock'}
	<svg class={cls} fill="none" stroke="currentColor" viewBox="0 0 24 24" stroke-width="2">
		<circle cx="12" cy="12" r="10"/>
		<polyline points="12 6 12 12 16 14"/>
	</svg>
{:else if icon === 'bell'}
	<svg class={cls} fill="none" stroke="currentColor" viewBox="0 0 24 24" stroke-width="2">
		<path d="M18 8A6 6 0 0 0 6 8c0 7-3 9-3 9h18s-3-2-3-9"/>
		<path d="M13.73 21a2 2 0 0 1-3.46 0"/>
	</svg>
{:else if icon === 'eye'}
	<svg class={cls} fill="none" stroke="currentColor" viewBox="0 0 24 24" stroke-width="2">
		<path d="M1 12s4-8 11-8 11 8 11 8-4 8-11 8-11-8-11-8z"/>
		<circle cx="12" cy="12" r="3"/>
	</svg>
{:else if icon === 'branch'}
	<svg class={cls} fill="none" stroke="currentColor" viewBox="0 0 24 24" stroke-width="2">
		<line x1="6" y1="3" x2="6" y2="15"/>
		<circle cx="18" cy="6" r="3"/>
		<circle cx="6" cy="18" r="3"/>
		<path d="M18 9a9 9 0 0 1-9 9"/>
	</svg>
{:else if icon === 'stop'}
	<svg class={cls} fill="none" stroke="currentColor" viewBox="0 0 24 24" stroke-width="2">
		<circle cx="12" cy="12" r="10"/>
		<rect x="9" y="9" width="6" height="6"/>
	</svg>
{:else if icon === 'send'}
	<svg class={cls} fill="none" stroke="currentColor" viewBox="0 0 24 24" stroke-width="2">
		<line x1="22" y1="2" x2="11" y2="13"/>
		<polygon points="22 2 15 22 11 13 2 9 22 2"/>
	</svg>
{:else}
	<svg class={cls} fill="none" stroke="currentColor" viewBox="0 0 24 24" stroke-width="2">
		<circle cx="12" cy="12" r="3"/>
		<path d="M19.4 15a1.65 1.65 0 0 0 .33 1.82l.06.06a2 2 0 0 1 0 2.83 2 2 0 0 1-2.83 0l-.06-.06a1.65 1.65 0 0 0-1.82-.33 1.65 1.65 0 0 0-1 1.51V21a2 2 0 0 1-2 2 2 2 0 0 1-2-2v-.09A1.65 1.65 0 0 0 9 19.4a1.65 1.65 0 0 0-1.82.33l-.06.06a2 2 0 0 1-2.83 0 2 2 0 0 1 0-2.83l.06-.06a1.65 1.65 0 0 0 .33-1.82 1.65 1.65 0 0 0-1.51-1H3a2 2 0 0 1-2-2 2 2 0 0 1 2-2h.09A1.65 1.65 0 0 0 4.6 9a1.65 1.65 0 0 0-.33-1.82l-.06-.06a2 2 0 0 1 0-2.83 2 2 0 0 1 2.83 0l.06.06a1.65 1.65 0 0 0 1.82.33H9a1.65 1.65 0 0 0 1-1.51V3a2 2 0 0 1 2-2 2 2 0 0 1 2 2v.09a1.65 1.65 0 0 0 1 1.51 1.65 1.65 0 0 0 1.82-.33l.06-.06a2 2 0 0 1 2.83 0 2 2 0 0 1 0 2.83l-.06.06a1.65 1.65 0 0 0-.33 1.82V9a1.65 1.65 0 0 0 1.51 1H21a2 2 0 0 1 2 2 2 2 0 0 1-2 2h-.09a1.65 1.65 0 0 0-1.51 1z"/>
	</svg>
{/if}
