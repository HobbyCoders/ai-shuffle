<script lang="ts">
	import { untrack, onDestroy } from 'svelte';
	import type { BrowserEditor } from '$lib/browser-panel/workspace';
	let { field, scale, left, top, viewWidth, viewHeight, onEdit, onActive, onEnter }: {
		field: BrowserEditor; scale: number; left: number; top: number; viewWidth: number; viewHeight: number;
		onEdit: (args: Record<string, unknown>) => void;
		onActive: (active: boolean) => void;
		onEnter: () => void;
	} = $props();
	let value = $state('');
	let focused = $state(false);
	let composing = false;
	let lastSent: string | undefined;
	let el = $state<HTMLInputElement | HTMLTextAreaElement>();
	$effect(() => {
		const remote = field.value;
		if (!untrack(() => focused)) value = remote;
	});
	function edit() {
		if (composing || lastSent === value) return;
		lastSent = value;
		onEdit({ field: field.id, action: 'replace', value, start: el?.selectionStart ?? value.length, end: el?.selectionEnd ?? value.length });
	}
	function focus() {
		focused = true;
		lastSent = undefined;
		onActive(true);
		onEdit({ field: field.id, action: 'focus' });
	}
	function blur() {
		focused = false;
		onActive(false);
		onEdit({ field: field.id, action: 'blur' });
	}
	onDestroy(() => { if (focused) onActive(false); });
	const style = $derived.by(() => {
		// A desktop page can be thumbnail-sized on a phone. Keep the hit target
		// aligned when idle, but make the focused native text readable/selectable.
		const width = focused ? Math.min(Math.max(field.width * scale, 240), viewWidth - 16) : field.width * scale;
		const height = focused ? Math.min(Math.max(field.height * scale, field.type === 'textarea' ? 80 : 36), viewHeight - 16) : field.height * scale;
		const x = focused ? Math.max(8, Math.min(left + field.x * scale, viewWidth - width - 8)) : left + field.x * scale;
		const y = focused ? Math.max(8, Math.min(top + field.y * scale, viewHeight - height - 8)) : top + field.y * scale;
		return `left:${x}px;top:${y}px;width:${width}px;height:${height}px`;
	});
	function key(e: KeyboardEvent) {
		e.stopPropagation();
		if (e.key === 'Escape') el?.blur();
		if (e.key === 'Enter' && field.type !== 'textarea' && !e.isComposing) { e.preventDefault(); onEnter(); }
	}
</script>

<!-- Real native controls, present BEFORE the first tap, preserve the trusted
     focus gesture on iOS and leave selection/copy/paste/IME to the browser. -->
{#if field.type === 'textarea'}
	<textarea bind:this={el} bind:value {style} class:focused aria-label={field.label}
		onfocus={focus} onblur={blur} oninput={(e) => { value = e.currentTarget.value; edit(); }} onkeydown={key} onkeyup={(e) => e.stopPropagation()}
		oncompositionstart={() => composing = true} oncompositionend={() => { composing = false; edit(); }}
		oncontextmenu={(e) => e.stopPropagation()} maxlength="16384" spellcheck="false"></textarea>
{:else}
	<input bind:this={el} bind:value type={field.type} {style} class:focused aria-label={field.label}
		onfocus={focus} onblur={blur} oninput={(e) => { value = e.currentTarget.value; edit(); }} onkeydown={key} onkeyup={(e) => e.stopPropagation()}
		oncompositionstart={() => composing = true} oncompositionend={() => { composing = false; edit(); }}
		oncontextmenu={(e) => e.stopPropagation()} maxlength="16384" autocomplete="off" spellcheck="false" />
{/if}

<style>
	input, textarea {
		position: absolute; box-sizing: border-box; margin: 0; min-width: 0;
		border: 1px solid transparent; border-radius: 3px; padding: 2px 4px;
		font: 16px/1.3 system-ui, sans-serif; resize: none;
		background: transparent; color: transparent; caret-color: transparent;
		touch-action: auto; user-select: text; -webkit-user-select: text;
		-webkit-touch-callout: default;
	}
	.focused { z-index: 2; background: var(--background, white); color: var(--foreground, #111); caret-color: auto; border-color: var(--primary, #6750a4); outline: 2px solid var(--primary, #6750a4); }
</style>
