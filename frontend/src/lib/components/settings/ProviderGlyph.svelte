<script lang="ts" module>
	/**
	 * Brand glyph kinds the model pickers know how to draw. Anything else falls
	 * back to a generic "custom endpoint" mark.
	 */
	export type ProviderGlyphKind = 'claude' | 'openai' | 'grok' | 'openrouter' | 'custom';

	/**
	 * Map a provider row's `provider_type` (or the native sentinel) onto a glyph.
	 * ChatGPT/Codex rows share the OpenAI mark; xAI API rows share Grok's.
	 */
	export function providerGlyphKind(providerType: string | null | undefined): ProviderGlyphKind {
		switch (providerType) {
			case 'claude':
			case '__native__':
			case '__claude__':
				return 'claude';
			case 'openai':
			case 'chatgpt':
				return 'openai';
			case 'grok':
			case 'xai':
				return 'grok';
			case 'openrouter':
				return 'openrouter';
			default:
				return 'custom';
		}
	}
</script>

<script lang="ts">
	interface Props {
		kind: ProviderGlyphKind;
		size?: number;
		class?: string;
	}
	let { kind, size = 16, class: className = '' }: Props = $props();
</script>

<svg
	class="provider-glyph {className}"
	width={size}
	height={size}
	viewBox="0 0 24 24"
	fill="none"
	stroke="currentColor"
	stroke-width="1.9"
	stroke-linecap="round"
	stroke-linejoin="round"
	aria-hidden="true"
>
	{#if kind === 'claude'}
		<!-- Sunburst mark -->
		<path d="M12 3.5v4M12 16.5v4M3.5 12h4M16.5 12h4M6 6l2.8 2.8M15.2 15.2 18 18M18 6l-2.8 2.8M8.8 15.2 6 18" />
		<circle cx="12" cy="12" r="2.2" fill="currentColor" stroke="none" />
	{:else if kind === 'openai'}
		<!-- Hexagonal knot approximation -->
		<path d="M12 3.2 19.6 7.6v8.8L12 20.8 4.4 16.4V7.6Z" />
		<path d="M12 8 15.5 10v4L12 16l-3.5-2v-4Z" />
		<path d="M12 3.2V8M19.6 16.4 15.5 14M4.4 16.4 8.5 14" />
	{:else if kind === 'grok'}
		<!-- Stylised X -->
		<path d="M5 4.5 19 19.5M19 4.5 5 19.5" stroke-width="2.4" />
	{:else if kind === 'openrouter'}
		<!-- Two routed paths -->
		<path d="M3 8h4.5c2.2 0 3.4 1 4.8 3 1.4 2 2.6 3 4.8 3H20" />
		<path d="M3 16h4.5c2.2 0 3.4-1 4.8-3 1.4-2 2.6-3 4.8-3H20" />
		<path d="m17.5 5.5 3 2.5-3 2.5M17.5 13.5l3 2.5-3 2.5" />
	{:else}
		<!-- Generic endpoint cube -->
		<path d="m12 3 8 4.5v9L12 21l-8-4.5v-9Z" />
		<path d="M4 7.5 12 12l8-4.5M12 12v9" />
	{/if}
</svg>

<style>
	.provider-glyph {
		display: block;
		flex-shrink: 0;
	}
</style>
