<script lang="ts">
	import { untrack, onDestroy } from 'svelte';
	import type { BrowserEditor } from '$lib/browser-panel/workspace';
	type FieldSurface = { background: string; color: string };
	let { field, scale, left, top, viewWidth, viewHeight, onEdit, onActive, onEnter, onText, onKey, onNavKey, surface }: {
		field: BrowserEditor; scale: number; left: number; top: number; viewWidth: number; viewHeight: number;
		onEdit: (args: Record<string, unknown>) => void;
		onActive: (active: boolean) => void;
		onEnter: () => void;
		/** Keystroke fields (split one-time-code boxes): typed text and Backspace go to the page's focused element. */
		onText?: (text: string) => void;
		onKey?: (key: 'Backspace' | 'Delete') => void;
		/**
		 * Navigation keys (arrows, PageUp/PageDown, Tab, Escape) also go to the
		 * page so its suggestion lists, focus order and popups keep working.
		 * `modifiers` is the CDP bitmask (Shift+Tab walks backwards).
		 */
		onNavKey?: (key: string, code: string, keyCode: number, modifiers: number) => void;
		/** The page field's own fill as drawn in the frame, so the focused editor blends in. */
		surface?: () => FieldSurface | null;
	} = $props();
	// Split code boxes move focus to the next box on every digit. Replacing one
	// box's value fights that, so these fields send real keystrokes instead and
	// keep no local copy (the page itself shows the digits).
	const keys = $derived(!!field.keys && !!onText && !!onKey);
	let value = $state('');
	let focused = $state(false);
	let composing = false;
	let lastSent: string | undefined;
	let el = $state<HTMLInputElement | HTMLTextAreaElement>();
	// Mouse/trackpad: the focused editor sits exactly in the page field's own
	// box, so nothing nearby is covered. It shows its own text and caret (sized
	// from the field) so a click lands where the text it edits really is.
	// Touch: a desktop page can be thumbnail-sized, so focus enlarges a readable
	// native editor instead.
	const finePointerQuery = typeof matchMedia === 'function' ? matchMedia('(pointer: fine)') : null;
	let finePointer = $state(finePointerQuery?.matches ?? false);
	$effect(() => {
		if (!finePointerQuery) return;
		const update = () => { finePointer = finePointerQuery.matches; };
		finePointerQuery.addEventListener?.('change', update);
		return () => finePointerQuery.removeEventListener?.('change', update);
	});
	const enlarged = $derived(focused && !keys && !finePointer);
	const inPlace = $derived(focused && !keys && finePointer);
	let fill = $state<FieldSurface | null>(null);
	const NAV_KEYS = new Map([['ArrowLeft', 37], ['ArrowUp', 38], ['ArrowRight', 39], ['ArrowDown', 40], ['PageUp', 33], ['PageDown', 34], ['Tab', 9], ['Escape', 27]]);
	$effect(() => {
		const remote = field.value;
		if (untrack(() => keys)) value = '';
		else if (!untrack(() => focused)) value = remote;
	});
	function edit() {
		if (keys) { if (!composing && value) onText?.(value); value = ''; if (el) el.value = ''; return; }
		if (composing || lastSent === value) return;
		lastSent = value;
		onEdit({ field: field.id, action: 'replace', value, start: el?.selectionStart ?? value.length, end: el?.selectionEnd ?? value.length });
	}
	function focus() {
		if (!keys) fill = surface?.() ?? null;
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
	const round = (n: number) => Math.round(n * 10) / 10;
	const style = $derived.by(() => {
		const box = `left:${left + field.x * scale}px;top:${top + field.y * scale}px;width:${field.width * scale}px;height:${field.height * scale}px`;
		if (finePointer && !keys) {
			// Match the page's own text so the click that focuses the field puts
			// the caret where that text really is (applied while idle too, so the
			// text never moves on focus). Text starts at (page border + padding) ×
			// scale; the overlay's own 1px border comes off that. Runtimes that do
			// not report border widths get the common 1px.
			const m = field.style;
			const textarea = field.type === 'textarea';
			let font: number, padX: number, padY: number, lineHeight: string, family = '';
			if (m) {
				font = m.fontSize * scale;
				padX = Math.max(0, (m.paddingLeft + (m.borderLeft ?? 1)) * scale - 1);
				padY = textarea ? Math.max(0, (m.paddingTop + (m.borderTop ?? 1)) * scale - 1) : 0;
				lineHeight = m.lineHeight ? `${round(m.lineHeight * scale)}px` : 'normal';
				// A page-supplied value goes into a style attribute: keep it to a
				// plain font list so it can never add other declarations.
				if (/^[\w\s,"'.-]{1,256}$/.test(m.fontFamily)) family = `;font-family:${m.fontFamily}, system-ui, sans-serif`;
			} else {
				// Old runtimes report no metrics. A single-line field's text is about
				// 0.62 of its height (13.33px in a 21px default input), capped at
				// 16px; a textarea's rows are unknown, so use the default 13.5px.
				font = (textarea ? 13.5 : Math.min(field.height * 0.62, 16)) * scale;
				padX = Math.max(1, 3 * scale);
				padY = textarea ? padX : 0;
				lineHeight = '1.2';
			}
			const colors = inPlace && fill ? `;--field-bg:${fill.background};--field-fg:${fill.color}` : '';
			return `${box};font-size:${Math.max(6, round(font))}px;line-height:${lineHeight};padding:${round(padY)}px ${round(padX)}px${family}${colors}`;
		}
		if (!enlarged) return box;
		// A desktop page can be thumbnail-sized on a phone. Keep the hit target
		// aligned when idle, but make the focused native text readable/selectable.
		const width = Math.min(Math.max(field.width * scale, 240), viewWidth - 16);
		const height = Math.min(Math.max(field.height * scale, field.type === 'textarea' ? 80 : 36), viewHeight - 16);
		const x = Math.max(8, Math.min(left + field.x * scale, viewWidth - width - 8));
		const y = Math.max(8, Math.min(top + field.y * scale, viewHeight - height - 8));
		const colors = fill ? `;--field-bg:${fill.background};--field-fg:${fill.color}` : '';
		return `left:${x}px;top:${y}px;width:${width}px;height:${height}px${colors}`;
	});
	function beforeInput(e: InputEvent) {
		if (!keys || composing || e.isComposing || !e.inputType.startsWith('delete')) return;
		// The local box is always empty, so Backspace must be forwarded by hand.
		e.preventDefault();
		onKey?.(e.inputType.endsWith('Forward') ? 'Delete' : 'Backspace');
	}
	function key(e: KeyboardEvent) {
		e.stopPropagation();
		if (keys && (e.key === 'Backspace' || e.key === 'Delete') && !e.isComposing) { e.preventDefault(); onKey?.(e.key); return; }
		const navCode = NAV_KEYS.get(e.key);
		if (onNavKey && navCode !== undefined && !e.isComposing && !composing) {
			// Arrows also move the local caret (no preventDefault) so the next
			// replacement still carries the right selection. Tab moves the page's
			// focus, so it must not also move this app's focus.
			if (e.key === 'Tab') e.preventDefault();
			onNavKey(e.key, e.code || e.key, navCode, (e.altKey ? 1 : 0) | (e.ctrlKey || e.metaKey ? 2 : 0) | (e.shiftKey ? 8 : 0));
		}
		if (e.key === 'Escape') el?.blur();
		if (e.key === 'Enter' && field.type !== 'textarea' && !e.isComposing) { e.preventDefault(); onEnter(); }
	}
</script>

<!-- Real native controls, present BEFORE the first tap, preserve the trusted
     focus gesture on iOS and leave selection/copy/paste/IME to the browser. -->
{#if field.type === 'textarea'}
	<textarea bind:this={el} bind:value {style} class:focused={focused && !keys} class:enlarged class:in-place={inPlace} class:keys aria-label={field.label}
		onfocus={focus} onblur={blur} onbeforeinput={beforeInput} oninput={(e) => { value = e.currentTarget.value; edit(); }} onkeydown={key} onkeyup={(e) => e.stopPropagation()}
		oncompositionstart={() => composing = true} oncompositionend={() => { composing = false; edit(); }}
		oncontextmenu={(e) => e.stopPropagation()} maxlength="16384" spellcheck="false"></textarea>
{:else}
	<input bind:this={el} bind:value type={field.type} {style} class:focused={focused && !keys} class:enlarged class:in-place={inPlace} class:keys aria-label={field.label}
		onfocus={focus} onblur={blur} onbeforeinput={beforeInput} oninput={(e) => { value = e.currentTarget.value; edit(); }} onkeydown={key} onkeyup={(e) => e.stopPropagation()}
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
	/* Keystroke boxes stay see-through: the page draws the digits and its own focus. */
	.keys { outline: 1px dashed var(--primary, #6750a4); outline-offset: 1px; }
	.keys:not(:focus) { outline: none; }
	/* Mouse/trackpad focus: the page field's own box, its own fill (sampled
	   from the frame; theme card colours if that fails) and real, visible text
	   and caret, so what you see is where the edit lands. The page under it can
	   be any colour (and a theme's ring can be white), so the ring is two
	   bands: app background, then the theme's focus colour. */
	.focused {
		z-index: 2; outline: none;
		box-shadow: 0 0 0 2px var(--background, #fff), 0 0 0 4px var(--ring, #6750a4), 0 0 10px 4px color-mix(in oklch, var(--ring, #6750a4) 30%, transparent);
	}
	.in-place {
		border-radius: 2px;
		background: var(--field-bg, var(--card, #fff)); color: var(--field-fg, var(--card-foreground, #111));
		caret-color: currentColor;
	}
	.in-place::selection { background: Highlight; color: HighlightText; }
	/* Touch focus: an enlarged, opaque native editor that is readable over a
	   thumbnail page, in the page field's colours when the frame can be read. */
	.enlarged { background: var(--field-bg, var(--background, white)); color: var(--field-fg, var(--foreground, #111)); caret-color: auto; border-color: var(--primary, #6750a4); outline: 2px solid var(--primary, #6750a4); box-shadow: none; }
	.enlarged::selection { background: color-mix(in oklch, var(--primary, #6750a4) 35%, transparent); }
</style>
