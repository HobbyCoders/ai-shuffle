<script lang="ts">
	/**
	 * BrowserDesignRail: the list of changes marked on the live page.
	 *
	 * Each row carries the same number as its pin on the page, so the list
	 * reads as the page's to-do. One row at a time is open for editing: the
	 * user writes what should change and can try a size or colour, which the
	 * panel previews on the page. The rail owns no state of its own beyond
	 * which controls are folded; the panel holds the changes and talks to the
	 * page.
	 */
	import { Check, ChevronDown, MessageSquare, Pencil, Trash2, Undo2, X } from 'lucide-svelte';
	import {
		changeSummary,
		elementKind,
		elementWords,
		hslToHex,
		isEmptyChange,
		isTransparent,
		originalValue,
		pixels,
		propLabel,
		safeValue,
		sourceLabel,
		toHex,
		toHsl,
		type DesignChange,
		type Hsl,
		type DesignProp,
		type DesignScope,
	} from '$lib/browser-panel/design';

	interface Props {
		/** `side`: a column beside the page. `sheet`: a drawer under it. */
		layout: 'side' | 'sheet';
		/** Marked changes in page order, including the one being edited. */
		rows: DesignChange[];
		/** Id of the row open in the editor, if any. */
		editingId: number | null;
		/** True when the open row is not in the list yet. */
		editingNew: boolean;
		/** The page is showing its real styles instead of the previews. */
		showingOriginal: boolean;
		/** Whether anything is being previewed on the page at all. */
		hasPreviews: boolean;
		sending: boolean;
		/** How many changes the last send carried, until the next edit. */
		sentCount: number | null;
		/** Bumped by the panel to move focus into the note field. */
		focusToken: number;
		onNote: (text: string) => void;
		onTweak: (prop: DesignProp, value: string | null) => void;
		onScope: (scope: DesignScope) => void;
		onCommit: () => void;
		onCancel: () => void;
		onEdit: (id: number) => void;
		onRemove: (id: number) => void;
		onCompare: (original: boolean) => void;
		onSend: () => void;
		onDone: () => void;
		onViewChat: () => void;
	}

	let {
		layout,
		rows,
		editingId,
		editingNew,
		showingOriginal,
		hasPreviews,
		sending,
		sentCount,
		focusToken,
		onNote,
		onTweak,
		onScope,
		onCommit,
		onCancel,
		onEdit,
		onRemove,
		onCompare,
		onSend,
		onDone,
		onViewChat,
	}: Props = $props();

	const sendable = $derived(rows.filter((r) => !isEmptyChange(r)).length);
	const editing = $derived(rows.find((r) => r.id === editingId) ?? null);

	// The try-it controls start open where there is room and folded in the drawer.
	let adjustChoice = $state<boolean | null>(null);
	const adjustOpen = $derived(adjustChoice ?? layout === 'side');

	let noteEl: HTMLTextAreaElement | undefined = $state();
	$effect(() => {
		if (focusToken > 0) noteEl?.focus({ preventScroll: false });
	});

	const WEIGHTS = [
		{ value: '400', label: 'Regular' },
		{ value: '600', label: 'Semibold' },
		{ value: '700', label: 'Bold' },
	];

	interface Slider {
		prop: DesignProp;
		min: number;
		max: number;
	}
	const SIZE: Slider = { prop: 'font-size', min: 8, max: 96 };
	const BOX_SLIDERS: Slider[] = [
		{ prop: 'padding', min: 0, max: 64 },
		{ prop: 'border-radius', min: 0, max: 48 },
		{ prop: 'margin-bottom', min: 0, max: 96 },
	];

	function title(change: DesignChange): string {
		const words = elementWords(change.element, change.detail);
		return words ? `${elementKind(change.element)} “${words}”` : elementKind(change.element);
	}

	/** Where the element lives: its source file when the page says, else its selector. */
	function location(change: DesignChange): { text: string; code: boolean; full: string } {
		const source = change.detail.source;
		if (source) {
			const [name, line] = sourceLabel(source).split(':');
			return { text: line ? `${name}, line ${line}` : name, code: false, full: source.file };
		}
		return { text: change.element.selector, code: true, full: change.element.selector };
	}

	function current(change: DesignChange, prop: DesignProp): string {
		return change.tweaks[prop] ?? originalValue(change.detail, prop);
	}

	/** The page's colour variables, one swatch per distinct colour. */
	function swatches(change: DesignChange): { name: string; value: string }[] {
		const seen = new Set<string>();
		const out: { name: string; value: string }[] = [];
		for (const token of change.detail.tokens) {
			const value = safeValue(token.value);
			if (!value || seen.has(value)) continue;
			seen.add(value);
			out.push({ name: token.name, value });
			if (out.length >= 9) break;
		}
		return out;
	}

	/**
	 * The colour an element has now as CSS the panel can paint, resolved when
	 * it is one of the page's variables. Null when it has no colour.
	 */
	function paint(change: DesignChange, prop: DesignProp): string | null {
		const value = current(change, prop);
		const name = /^var\((--[\w-]+)\)$/.exec(value)?.[1];
		const actual = name ? (change.detail.tokens.find((t) => t.name === name)?.value ?? '') : value;
		return !actual || isTransparent(actual) ? null : safeValue(actual);
	}

	let probe: CanvasRenderingContext2D | null | undefined;
	/** `#rrggbb` for any colour the browser understands (names, hsl(), …). */
	function hexOf(colour: string): string | null {
		const direct = toHex(colour);
		if (direct) return direct;
		if (probe === undefined) probe = document.createElement('canvas').getContext('2d');
		if (!probe) return null;
		// A value the canvas rejects leaves the previous fill in place.
		probe.fillStyle = '#000000';
		probe.fillStyle = colour;
		const first = probe.fillStyle;
		probe.fillStyle = '#ffffff';
		probe.fillStyle = colour;
		return first === probe.fillStyle ? toHex(first) : null;
	}

	/** The native picker needs `#rrggbb`; grey stands in when there is no colour. */
	function pickerValue(change: DesignChange, prop: DesignProp): string {
		const shown = paint(change, prop);
		return (shown && hexOf(shown)) ?? '#808080';
	}

	/** What the value field reads: the tried value, else the element's own colour. */
	function colourText(change: DesignChange, prop: DesignProp): string {
		const tried = change.tweaks[prop];
		if (tried) return tried;
		const own = originalValue(change.detail, prop);
		if (!own || isTransparent(own)) return '';
		return /^rgba\(/i.test(own) ? own : (toHex(own) ?? own);
	}

	/**
	 * Hue and saturation the brightness slider is holding, by change and
	 * property. Black and white carry neither, so the slider remembers them
	 * for as long as the colour on the page is the one it last set.
	 */
	const held: Record<string, { h: number; s: number; out: string }> = {};

	function hueOf(change: DesignChange, prop: DesignProp): Hsl | null {
		const shown = paint(change, prop);
		const hex = shown ? hexOf(shown) : null;
		const now = hex ? toHsl(hex) : null;
		if (!now) return null;
		const kept = held[`${change.id}:${prop}`];
		return kept && kept.out === change.tweaks[prop] ? { h: kept.h, s: kept.s, l: now.l } : now;
	}

	/** Lighten or darken the colour, keeping its hue. */
	function setBrightness(change: DesignChange, prop: DesignProp, light: number) {
		const base = hueOf(change, prop);
		if (!base) return;
		const out = hslToHex(base.h, base.s, light);
		held[`${change.id}:${prop}`] = { h: base.h, s: base.s, out };
		onTweak(prop, out);
	}

	function isColour(value: string): boolean {
		return typeof CSS !== 'undefined' && typeof CSS.supports === 'function'
			? CSS.supports('color', value)
			: toHex(value) !== null;
	}

	/** Value fields holding text that is not a colour (yet), by change and property. */
	let notColour = $state<Record<string, boolean>>({});

	/** Preview a typed colour as soon as it is one: hex, rgb(), hsl() or a name. */
	function typeColour(change: DesignChange, prop: DesignProp, raw: string) {
		const key = `${change.id}:${prop}`;
		const value = raw.trim() ? safeValue(raw) : null;
		const ok = value !== null && isColour(value);
		notColour = { ...notColour, [key]: !!raw.trim() && !ok };
		if (ok) onTweak(prop, value);
	}

	/** Leaving the field drops half-typed text and shows the colour in use. */
	function settleColour(change: DesignChange, prop: DesignProp, field: HTMLInputElement) {
		field.value = colourText(change, prop);
		notColour = { ...notColour, [`${change.id}:${prop}`]: false };
	}

	function onNoteKeydown(e: KeyboardEvent) {
		if (e.key === 'Enter' && (e.metaKey || e.ctrlKey)) {
			e.preventDefault();
			onCommit();
		} else if (e.key === 'Escape') {
			e.preventDefault();
			onCancel();
		}
	}
</script>

{#snippet undoButton(change: DesignChange, prop: DesignProp, back: string)}
	{@const changed = prop in change.tweaks}
	<button
		class="icon-btn undo"
		class:hidden={!changed}
		disabled={!changed}
		onclick={() => onTweak(prop, null)}
		aria-label="Put {propLabel(prop).toLowerCase()} back to {back}"
		title="Back to {back}"
	>
		<Undo2 size={14} strokeWidth={1.75} />
	</button>
{/snippet}

{#snippet sliderRow(change: DesignChange, slider: Slider)}
	{@const from = pixels(originalValue(change.detail, slider.prop))}
	{@const now = pixels(current(change, slider.prop), from)}
	{@const changed = slider.prop in change.tweaks}
	<div class="control">
		<label for="design-{slider.prop}-{change.id}">{propLabel(slider.prop)}</label>
		<input
			id="design-{slider.prop}-{change.id}"
			type="range"
			min={slider.min}
			max={Math.max(slider.max, Math.ceil(from))}
			step="1"
			value={now}
			oninput={(e) => onTweak(slider.prop, `${e.currentTarget.value}px`)}
		/>
		<span class="readout" class:changed>{now}px</span>
		{@render undoButton(change, slider.prop, `${from}px`)}
	</div>
{/snippet}

{#snippet weightRow(change: DesignChange)}
	{@const weight = current(change, 'font-weight')}
	<div class="control">
		<span class="control-label" id="design-weight-{change.id}">{propLabel('font-weight')}</span>
		<div class="segmented wide" role="group" aria-labelledby="design-weight-{change.id}">
			{#each WEIGHTS as w (w.value)}
				<button class:on={weight === w.value} aria-pressed={weight === w.value} onclick={() => onTweak('font-weight', w.value)}>
					{w.label}
				</button>
			{/each}
		</div>
		{@render undoButton(change, 'font-weight', 'the original weight')}
	</div>
{/snippet}

{#snippet colourRow(change: DesignChange, prop: DesignProp)}
	{@const tokens = swatches(change)}
	{@const shown = paint(change, prop)}
	{@const hue = hueOf(change, prop)}
	{@const name = propLabel(prop).toLowerCase()}
	<div class="control colour">
		<span class="control-label" id="design-{prop}-{change.id}">{propLabel(prop)}</span>
		{@render undoButton(change, prop, 'the original colour')}
		<div class="colour-pick">
			<label class="well" title="Open the colour picker">
				<span class="swatch-fill" class:none={shown === null} style:background-color={shown ?? 'transparent'}></span>
				<input
					type="color"
					value={pickerValue(change, prop)}
					oninput={(e) => onTweak(prop, e.currentTarget.value)}
					aria-label="Pick any {name}"
				/>
			</label>
			<input
				class="colour-value"
				type="text"
				spellcheck="false"
				autocapitalize="off"
				autocomplete="off"
				value={colourText(change, prop)}
				placeholder="No colour"
				aria-label="{propLabel(prop)} as hex, rgb(), hsl() or a colour name"
				aria-invalid={notColour[`${change.id}:${prop}`] === true}
				oninput={(e) => typeColour(change, prop, e.currentTarget.value)}
				onblur={(e) => settleColour(change, prop, e.currentTarget)}
			/>
		</div>
		{#if hue}
			<div class="colour-line">
				<label class="colour-line-label" for="design-{prop}-light-{change.id}">Brightness</label>
				<input
					id="design-{prop}-light-{change.id}"
					class="brightness"
					type="range"
					min="0"
					max="100"
					step="1"
					value={Math.round(hue.l)}
					style:--track="linear-gradient(to right, #000, {hslToHex(hue.h, hue.s, 50)}, #fff)"
					style:--thumb={shown}
					oninput={(e) => setBrightness(change, prop, Number(e.currentTarget.value))}
				/>
				<span class="readout" class:changed={prop in change.tweaks}>{Math.round(hue.l)}%</span>
			</div>
		{/if}
		{#if tokens.length > 0}
			<div class="colour-line" role="group" aria-label="This page's own colours for the {name}">
				<span class="colour-line-label">From this page</span>
				<div class="swatches">
					{#each tokens as token (token.name)}
						{@const value = `var(${token.name})`}
						<button
							class="swatch"
							class:on={change.tweaks[prop] === value}
							aria-pressed={change.tweaks[prop] === value}
							onclick={() => onTweak(prop, value)}
							aria-label="Use the page colour {token.name.replace(/^--/, '')}"
							title={token.name.replace(/^--/, '')}
						>
							<span class="swatch-fill" style:background-color={token.value}></span>
						</button>
					{/each}
				</div>
			</div>
		{/if}
	</div>
{/snippet}

<section class="rail" class:sheet={layout === 'sheet'} aria-label="Design changes">
	<header class="rail-head">
		<div class="rail-title">
			<h2>Changes</h2>
			{#if rows.length > 0}<span class="rail-count">{rows.length} marked</span>{/if}
		</div>
		<button class="quiet-btn" onclick={onDone}>Done</button>
	</header>

	<div class="rail-body">
		{#if sentCount !== null}
			<div class="sent" role="status">
				<span class="sent-mark"><Check size={14} strokeWidth={2.25} /></span>
				<div class="sent-text">
					<p class="sent-title">Sent {sentCount} {sentCount === 1 ? 'change' : 'changes'}</p>
					<p class="sent-copy">
						The page is back to the real code, so what changes next is the AI's edit.
					</p>
					<button class="link-btn" onclick={onViewChat}>
						<MessageSquare size={13} strokeWidth={1.75} />
						View chat
					</button>
				</div>
			</div>
		{/if}

		{#if rows.length === 0}
			<div class="empty">
				<div class="empty-demo" aria-hidden="true">
					<span class="chip">1</span>
					<span class="empty-box"></span>
				</div>
				<p class="empty-title">
					{sentCount !== null ? 'Mark the next change' : 'Click anything on the page'}
				</p>
				<p class="empty-copy">
					Each click marks an element. Say what should change, or try a size or colour here
					first. Send the whole list to the AI when you are ready.
				</p>
			</div>
		{:else}
			<ol class="thread">
				{#each rows as change, index (change.id)}
					{@const open = change.id === editingId}
					<li class="row" class:open>
						<span class="chip" aria-hidden="true">{index + 1}</span>
						{#if open}
							{@const where = location(change)}
							{@const hasText = !!change.element.text}
							<div class="editor">
								<div class="editor-head">
									<p class="row-title">{title(change)}</p>
									<button
										class="icon-btn"
										onclick={onCancel}
										aria-label={editingNew ? 'Discard this change' : 'Stop editing this change'}
										title={editingNew ? 'Discard' : 'Stop editing'}
									>
										<X size={15} strokeWidth={1.75} />
									</button>
								</div>
								<p class="where" class:code={where.code} title={where.full}>{where.text}</p>

								{#if change.detail.similar > 1}
									<div class="segmented scope" role="group" aria-label="What this change applies to">
										<button
											class:on={change.scope === 'one'}
											aria-pressed={change.scope === 'one'}
											onclick={() => onScope('one')}>Just this one</button
										>
										<button
											class:on={change.scope === 'all'}
											aria-pressed={change.scope === 'all'}
											onclick={() => onScope('all')}>All {change.detail.similar} like it</button
										>
									</div>
								{/if}

								<label class="field-label" for="design-note-{change.id}">What should change?</label>
								<textarea
									id="design-note-{change.id}"
									bind:this={noteEl}
									class="note"
									rows="3"
									placeholder="Make this stand out more"
									value={change.note}
									oninput={(e) => onNote(e.currentTarget.value)}
									onkeydown={onNoteKeydown}></textarea>

								<button
									class="disclosure"
									aria-expanded={adjustOpen}
									onclick={() => (adjustChoice = !adjustOpen)}
								>
									<span>Try it on the page</span>
									<ChevronDown size={15} strokeWidth={1.75} />
								</button>

								{#if adjustOpen}
									<div class="controls">
										{#if hasText}
											<div class="group">
												{@render sliderRow(change, SIZE)}
												{@render weightRow(change)}
												{@render colourRow(change, 'color')}
											</div>
										{/if}
										<div class="group">
											{@render colourRow(change, 'background-color')}
											{#each BOX_SLIDERS as slider (slider.prop)}
												{@render sliderRow(change, slider)}
											{/each}
										</div>
									</div>
								{/if}

								<div class="editor-actions">
									<button class="solid-btn" onclick={onCommit} disabled={isEmptyChange(change)}>
										{editingNew ? 'Add change' : 'Save change'}
									</button>
									<span class="editor-hint">or click the next element</span>
								</div>
							</div>
						{:else}
							<div class="summary">
								<button class="summary-main" onclick={() => onEdit(change.id)} disabled={sending} title="Edit this change">
									<span class="row-title">{title(change)}</span>
									<span class="row-note">{changeSummary(change)}</span>
								</button>
								<div class="row-actions">
									<button class="icon-btn" onclick={() => onEdit(change.id)} disabled={sending} aria-label="Edit change {index + 1}" title="Edit">
										<Pencil size={14} strokeWidth={1.75} />
									</button>
									<button class="icon-btn" onclick={() => onRemove(change.id)} disabled={sending} aria-label="Remove change {index + 1}" title="Remove">
										<Trash2 size={14} strokeWidth={1.75} />
									</button>
								</div>
							</div>
						{/if}
					</li>
				{/each}
			</ol>
		{/if}
	</div>

	{#if rows.length > 0}
		<footer class="rail-foot">
			{#if hasPreviews}
				<div class="compare">
					<span class="compare-label" id="design-compare">Page shows</span>
					<div class="segmented wide" role="group" aria-labelledby="design-compare">
						<button class:on={showingOriginal} aria-pressed={showingOriginal} onclick={() => onCompare(true)}>
							Original
						</button>
						<button class:on={!showingOriginal} aria-pressed={!showingOriginal} onclick={() => onCompare(false)}>
							With changes
						</button>
					</div>
				</div>
			{/if}
			<button class="send-btn" onclick={onSend} disabled={sendable === 0 || sending} aria-busy={sending}>
				{#if sending}
					Sending…
				{:else if sendable === 0}
					Describe a change to send it
				{:else}
					Send {sendable} {sendable === 1 ? 'change' : 'changes'} to the AI
				{/if}
			</button>
		</footer>
	{/if}
</section>

<style>
	.rail {
		--pad: 20px;
		display: flex;
		flex-direction: column;
		flex: none;
		width: 424px;
		min-height: 0;
		background: var(--background);
		border-left: 1px solid var(--border);
		color: var(--foreground);
		font-family: var(--font-sans);
		font-size: 0.8125rem;
		line-height: 1.5;
	}
	.rail.sheet {
		--pad: 16px;
		width: auto;
		max-height: 58%;
		border-left: 0;
		border-top: 1px solid var(--border);
		box-shadow: 0 -8px 24px oklch(0 0 0 / 0.12);
	}

	/* ── Head ───────────────────────────────────────────────────────────── */
	.rail-head {
		display: flex;
		align-items: center;
		justify-content: space-between;
		gap: 12px;
		padding: 16px var(--pad) 12px;
		flex: none;
	}
	.sheet .rail-head {
		padding: 10px var(--pad) 8px;
	}
	.rail-title {
		display: flex;
		align-items: baseline;
		gap: 10px;
		min-width: 0;
	}
	h2 {
		margin: 0;
		font-size: 0.9375rem;
		font-weight: 600;
		letter-spacing: -0.005em;
	}
	.rail-count {
		color: var(--muted-foreground);
		font-size: 0.8125rem;
		font-variant-numeric: tabular-nums;
	}

	.rail-body {
		flex: 1;
		min-height: 0;
		overflow-y: auto;
		overscroll-behavior: contain;
		padding: 4px var(--pad) 20px;
	}

	/* ── Shared controls ────────────────────────────────────────────────── */
	button {
		font: inherit;
		color: inherit;
		-webkit-tap-highlight-color: transparent;
	}
	button:focus-visible,
	textarea:focus-visible,
	input[type='range']:focus-visible,
	.well:focus-within {
		outline: 2px solid var(--ring);
		outline-offset: 2px;
	}

	.quiet-btn {
		height: 32px;
		padding: 0 12px;
		background: transparent;
		border: 1px solid var(--border);
		border-radius: var(--radius);
		font-weight: 500;
		cursor: pointer;
		transition: background 120ms ease;
	}
	.quiet-btn:hover {
		background: var(--hover-overlay);
	}

	.icon-btn {
		width: 30px;
		height: 30px;
		flex: none;
		display: inline-flex;
		align-items: center;
		justify-content: center;
		background: transparent;
		border: 0;
		border-radius: calc(var(--radius) - 2px);
		color: var(--muted-foreground);
		cursor: pointer;
		transition:
			background 120ms ease,
			color 120ms ease;
	}
	.icon-btn:hover:not(:disabled) {
		background: var(--hover-overlay);
		color: var(--foreground);
	}
	.icon-btn.hidden {
		visibility: hidden;
	}

	.chip {
		width: 22px;
		height: 22px;
		flex: none;
		display: inline-flex;
		align-items: center;
		justify-content: center;
		border-radius: 7px;
		background: var(--primary);
		color: var(--primary-foreground);
		font-size: 0.75rem;
		font-weight: 600;
		font-variant-numeric: tabular-nums;
		line-height: 1;
	}

	.segmented {
		display: inline-flex;
		padding: 3px;
		gap: 2px;
		background: var(--muted);
		border-radius: var(--radius);
		min-width: 0;
	}
	.segmented.wide {
		display: flex;
		flex: 1;
	}
	.segmented button {
		flex: 1;
		min-width: 0;
		height: 28px;
		padding: 0 10px;
		background: transparent;
		border: 0;
		border-radius: calc(var(--radius) - 3px);
		color: var(--muted-foreground);
		font-size: 0.78125rem;
		font-weight: 500;
		white-space: nowrap;
		cursor: pointer;
		transition:
			background 120ms ease,
			color 120ms ease;
	}
	.segmented button:hover {
		color: var(--foreground);
	}
	.segmented button.on {
		background: var(--background);
		color: var(--foreground);
		box-shadow: var(--shadow-s);
	}

	/* ── Empty and sent states ──────────────────────────────────────────── */
	.empty {
		padding: 28px 0 8px;
		max-width: 300px;
	}
	.sheet .empty {
		padding: 8px 0 4px;
		max-width: none;
	}
	.empty-demo {
		position: relative;
		width: 132px;
		height: 60px;
		margin: 0 0 20px 11px;
	}
	.sheet .empty-demo {
		display: none;
	}
	.empty-demo .chip {
		position: absolute;
		top: -11px;
		left: -11px;
	}
	.empty-box {
		position: absolute;
		inset: 0;
		border: 1.5px dashed color-mix(in oklch, var(--primary) 70%, transparent);
		border-radius: 3px;
		background: color-mix(in oklch, var(--primary) 7%, transparent);
	}
	.empty-title {
		margin: 0 0 6px;
		font-size: 0.9375rem;
		font-weight: 600;
	}
	.empty-copy {
		margin: 0;
		color: var(--muted-foreground);
		font-size: 0.8125rem;
		line-height: 1.6;
	}

	.sent {
		display: flex;
		gap: 12px;
		padding: 14px;
		margin-bottom: 8px;
		background: var(--card);
		border-radius: calc(var(--radius) + 2px);
	}
	.sent-mark {
		width: 22px;
		height: 22px;
		flex: none;
		display: inline-flex;
		align-items: center;
		justify-content: center;
		border-radius: 50%;
		background: color-mix(in oklch, var(--success) 18%, transparent);
		color: var(--success);
	}
	.sent-text {
		min-width: 0;
	}
	.sent-title {
		margin: 0;
		font-weight: 600;
	}
	.sent-copy {
		margin: 2px 0 8px;
		color: var(--muted-foreground);
	}
	.link-btn {
		display: inline-flex;
		align-items: center;
		gap: 6px;
		height: 30px;
		padding: 0 10px;
		margin-left: -10px;
		background: transparent;
		border: 0;
		border-radius: var(--radius);
		color: var(--primary);
		font-weight: 500;
		cursor: pointer;
	}
	.link-btn:hover {
		background: var(--hover-overlay);
	}

	/* ── The numbered thread ────────────────────────────────────────────── */
	.thread {
		list-style: none;
		margin: 0;
		padding: 8px 0 0;
		display: flex;
		flex-direction: column;
	}
	.row {
		position: relative;
		display: flex;
		align-items: flex-start;
		gap: 14px;
		padding-bottom: 14px;
	}
	.row:last-child {
		padding-bottom: 0;
	}
	/* The line that ties one number to the next. */
	.row:not(:last-child)::before {
		content: '';
		position: absolute;
		left: 10.5px;
		top: 28px;
		bottom: 2px;
		width: 1px;
		background: var(--border);
	}
	.row > .chip {
		margin-top: 7px;
	}
	/* The open change takes the full width; its number sits inside the card. */
	.row.open {
		display: block;
	}
	.row.open > .chip {
		position: absolute;
		top: 15px;
		left: 16px;
		margin: 0;
		z-index: 1;
	}
	.row:not(.open) > .chip {
		background: color-mix(in oklch, var(--primary) 16%, transparent);
		color: var(--primary);
	}

	.summary {
		flex: 1;
		min-width: 0;
		display: flex;
		align-items: flex-start;
		gap: 4px;
		border-radius: calc(var(--radius) + 2px);
		transition: background 120ms ease;
	}
	.summary:hover,
	.summary:focus-within {
		background: var(--hover-overlay);
	}
	.summary-main {
		flex: 1;
		min-width: 0;
		display: flex;
		flex-direction: column;
		gap: 2px;
		padding: 7px 4px 8px 10px;
		margin-left: -10px;
		background: transparent;
		border: 0;
		border-radius: inherit;
		text-align: left;
		cursor: pointer;
	}
	.row-title {
		margin: 0;
		font-size: 0.84375rem;
		font-weight: 550;
		overflow: hidden;
		text-overflow: ellipsis;
		white-space: nowrap;
	}
	.row-note {
		color: var(--muted-foreground);
		display: -webkit-box;
		-webkit-line-clamp: 2;
		line-clamp: 2;
		-webkit-box-orient: vertical;
		overflow: hidden;
	}
	.row-actions {
		display: flex;
		padding: 3px 3px 0 0;
		opacity: 0;
		transition: opacity 120ms ease;
	}
	.summary:hover .row-actions,
	.summary:focus-within .row-actions {
		opacity: 1;
	}
	@media (hover: none) {
		.row-actions {
			opacity: 1;
		}
	}

	/* ── The open change ────────────────────────────────────────────────── */
	.editor {
		position: relative;
		min-width: 0;
		padding: 14px 16px 16px;
		background: var(--card);
		border-radius: calc(var(--radius) + 4px);
		box-shadow:
			0 0 0 1px color-mix(in oklch, var(--primary) 38%, transparent),
			var(--shadow-m);
	}
	.editor-head {
		display: flex;
		align-items: center;
		gap: 8px;
		padding-left: 34px;
		min-height: 24px;
	}
	.editor-head .row-title {
		flex: 1;
		min-width: 0;
		font-size: 0.875rem;
		font-weight: 600;
	}
	.editor-head .icon-btn {
		margin: -4px -6px -4px 0;
	}
	.where {
		margin: 6px 0 0;
		color: var(--muted-foreground);
		font-size: 0.78125rem;
		overflow: hidden;
		text-overflow: ellipsis;
		white-space: nowrap;
	}
	.where.code {
		font-family: var(--font-mono, monospace);
		font-size: 0.71875rem;
	}
	.scope {
		margin-top: 12px;
		display: flex;
	}
	.field-label {
		display: block;
		margin: 16px 0 6px;
		font-weight: 550;
	}
	.note {
		display: block;
		width: 100%;
		min-height: 76px;
		padding: 9px 11px;
		resize: vertical;
		background: var(--background);
		border: 1px solid var(--input);
		border-radius: var(--radius);
		color: var(--foreground);
		font: inherit;
		line-height: 1.5;
	}
	.note::placeholder {
		color: color-mix(in oklch, var(--muted-foreground) 70%, transparent);
	}
	.note:focus {
		outline: none;
		border-color: var(--ring);
		box-shadow: 0 0 0 3px color-mix(in oklch, var(--ring) 22%, transparent);
	}

	.disclosure {
		display: flex;
		align-items: center;
		justify-content: space-between;
		width: 100%;
		height: 36px;
		margin-top: 12px;
		padding: 0;
		background: transparent;
		border: 0;
		border-top: 1px solid var(--border);
		font-weight: 550;
		cursor: pointer;
	}
	.disclosure :global(svg) {
		color: var(--muted-foreground);
		transition: transform 150ms ease;
	}
	.disclosure[aria-expanded='true'] :global(svg) {
		transform: rotate(180deg);
	}

	.controls {
		display: flex;
		flex-direction: column;
		gap: 14px;
		padding: 2px 0 4px;
	}
	.group {
		display: flex;
		flex-direction: column;
		gap: 4px;
	}
	.group + .group {
		padding-top: 12px;
		border-top: 1px solid var(--border);
	}
	.control {
		display: grid;
		grid-template-columns: 96px minmax(0, 1fr) 48px 30px;
		align-items: center;
		column-gap: 10px;
		min-height: 38px;
	}
	.control > label,
	.control-label {
		color: var(--muted-foreground);
	}
	.control > .segmented {
		grid-column: 2 / 4;
	}
	/* Colours get lines of their own: the label, the picker with its value, then
	   the colours the page itself defines. */
	.control.colour {
		grid-template-columns: minmax(0, 1fr) 30px;
		row-gap: 0;
		padding-bottom: 8px;
	}
	.colour-pick,
	.colour-line {
		grid-column: 1 / -1;
		display: flex;
		align-items: center;
	}
	.colour-pick {
		gap: 8px;
	}
	.well {
		position: relative;
		flex: none;
		width: 36px;
		height: 36px;
		overflow: hidden;
		border-radius: var(--radius);
		cursor: pointer;
	}
	.well input {
		position: absolute;
		inset: 0;
		width: 100%;
		height: 100%;
		opacity: 0;
		cursor: pointer;
	}
	.colour-value {
		flex: 1;
		min-width: 0;
		height: 36px;
		padding: 0 10px;
		background: var(--background);
		border: 1px solid var(--input);
		border-radius: var(--radius);
		color: var(--foreground);
		font-family: var(--font-mono, monospace);
		font-size: 0.78125rem;
	}
	.colour-value::placeholder {
		color: color-mix(in oklch, var(--muted-foreground) 70%, transparent);
		font-family: var(--font-sans);
	}
	.colour-value:focus {
		outline: none;
		border-color: var(--ring);
		box-shadow: 0 0 0 3px color-mix(in oklch, var(--ring) 22%, transparent);
	}
	.colour-value[aria-invalid='true'] {
		border-color: var(--destructive);
		box-shadow: none;
	}
	/* The lines under the picker share one label column so they line up. */
	.colour-line {
		gap: 10px;
		min-height: 28px;
		margin-top: 8px;
	}
	.colour-line-label {
		flex: none;
		width: 84px;
		color: var(--muted-foreground);
		font-size: 0.75rem;
	}
	.colour-line > .readout {
		flex: none;
		width: 38px;
	}
	/* Brightness runs from black, through the colour, to white. */
	.brightness {
		flex: 1;
		min-width: 0;
		appearance: none;
		background: transparent;
	}
	.brightness::-webkit-slider-runnable-track {
		height: 10px;
		border-radius: 999px;
		background: var(--track);
		box-shadow: inset 0 0 0 1px color-mix(in oklch, var(--foreground) 18%, transparent);
	}
	.brightness::-moz-range-track {
		height: 10px;
		border-radius: 999px;
		background: var(--track);
		box-shadow: inset 0 0 0 1px color-mix(in oklch, var(--foreground) 18%, transparent);
	}
	.brightness::-webkit-slider-thumb {
		appearance: none;
		width: 18px;
		height: 18px;
		margin-top: -4px;
		border: 2px solid var(--card);
		border-radius: 50%;
		background: var(--thumb);
		box-shadow:
			0 0 0 1px color-mix(in oklch, var(--foreground) 38%, transparent),
			0 1px 3px rgb(0 0 0 / 0.25);
	}
	.brightness::-moz-range-thumb {
		width: 14px;
		height: 14px;
		border: 2px solid var(--card);
		border-radius: 50%;
		background: var(--thumb);
		box-shadow:
			0 0 0 1px color-mix(in oklch, var(--foreground) 38%, transparent),
			0 1px 3px rgb(0 0 0 / 0.25);
	}
	.readout {
		text-align: right;
		font-variant-numeric: tabular-nums;
		color: var(--muted-foreground);
	}
	.readout.changed {
		color: var(--foreground);
		font-weight: 550;
	}
	input[type='range'] {
		width: 100%;
		height: 20px;
		margin: 0;
		accent-color: var(--primary);
		cursor: pointer;
	}

	.swatches {
		display: flex;
		flex-wrap: wrap;
		align-items: center;
		gap: 8px;
		padding: 2px 0;
	}
	.swatch {
		position: relative;
		width: 24px;
		height: 24px;
		flex: none;
		padding: 0;
		background: transparent;
		border: 0;
		border-radius: 50%;
		cursor: pointer;
	}
	.swatch-fill {
		position: absolute;
		inset: 0;
		border-radius: inherit;
		box-shadow: inset 0 0 0 1px color-mix(in oklch, var(--foreground) 22%, transparent);
	}
	/* No colour set: a struck-through chip instead of a made-up grey. */
	.swatch-fill.none {
		background-image: linear-gradient(
			to top right,
			transparent calc(50% - 0.75px),
			color-mix(in oklch, var(--foreground) 40%, transparent) calc(50% - 0.75px),
			color-mix(in oklch, var(--foreground) 40%, transparent) calc(50% + 0.75px),
			transparent calc(50% + 0.75px)
		);
	}
	.swatch.on .swatch-fill {
		box-shadow:
			0 0 0 2px var(--card),
			0 0 0 4px var(--primary);
	}

	.editor-actions {
		display: flex;
		align-items: center;
		gap: 12px;
		margin-top: 14px;
	}
	.editor-hint {
		color: var(--muted-foreground);
		font-size: 0.78125rem;
	}
	.solid-btn {
		height: 34px;
		padding: 0 14px;
		background: var(--primary);
		border: 0;
		border-radius: var(--radius);
		color: var(--primary-foreground);
		font-weight: 600;
		cursor: pointer;
		transition: opacity 120ms ease;
	}
	.solid-btn:hover:not(:disabled) {
		opacity: 0.9;
	}
	.solid-btn:disabled {
		opacity: 0.45;
		cursor: default;
	}

	/* ── Foot ───────────────────────────────────────────────────────────── */
	.rail-foot {
		flex: none;
		display: flex;
		flex-direction: column;
		gap: 12px;
		padding: 14px var(--pad) calc(16px + env(safe-area-inset-bottom, 0px));
		border-top: 1px solid var(--border);
	}
	.sheet .rail-foot {
		padding-top: 10px;
		gap: 8px;
	}
	.compare {
		display: flex;
		align-items: center;
		gap: 12px;
	}
	.compare-label {
		flex: none;
		color: var(--muted-foreground);
	}
	.send-btn {
		height: 40px;
		padding: 0 16px;
		background: var(--action);
		border: 0;
		border-radius: var(--radius);
		color: var(--action-foreground);
		font-size: 0.875rem;
		font-weight: 600;
		cursor: pointer;
		transition: opacity 120ms ease;
	}
	.send-btn:hover:not(:disabled) {
		opacity: 0.9;
	}
	.send-btn:disabled {
		background: var(--muted);
		color: var(--muted-foreground);
		cursor: default;
	}

	@media (max-width: 639px) {
		/* 16px keeps iOS from zooming the page when the field takes focus. */
		.note,
		.colour-value {
			font-size: 16px;
		}
		.icon-btn {
			width: 36px;
			height: 36px;
		}
	}
	@media (prefers-reduced-motion: reduce) {
		.rail *,
		.rail *::before {
			transition: none !important;
		}
	}
</style>
