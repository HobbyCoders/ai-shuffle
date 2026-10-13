<script lang="ts">
	import { copyText, COPY_ERROR_MESSAGE } from '$lib/utils/clipboard';
	/**
	 * AssistantMessage - Assistant message with markdown rendering and hover-only action buttons
	 */
	import { onDestroy, untrack } from 'svelte';
	import { apiBase, withAuth } from '$lib/api/runtime';
	import { sanitizeHtml } from '$lib/utils/sanitize';
	import { rewriteMediaMarkdown, clampRevealToMediaBoundary, retryImages } from '$lib/utils/media';
	import { renderChatMarkdown } from '$lib/utils/chat-markdown';
	import { routeChatFiles } from '$lib/utils/chat-files';
	import { rewriteWithGallery } from '$lib/utils/media-gallery';
	import { highlightGenerationForSource, onHighlighterReady } from '$lib/utils/shiki';
	import { Typewriter } from '$lib/utils/typewriter';
	import MessageActions from './MessageActions.svelte';
	import HtmlPreviews from './HtmlPreviews.svelte';
	import UsageEstimatedBadge from '$lib/components/UsageEstimatedBadge.svelte';
	import { formatMessageTime } from '$lib/utils/message-time';
	import { taskChecks, taskMessageKey, type TaskChecks } from '$lib/stores/task-checks';
	import { terminals } from '$lib/stores/terminals.svelte';
	import { lookupCommandBlock } from '$lib/terminal/commands';
	import { blockAccess, blockTarget, cwdProblem, folderPieces, printableFolder, targetLine, type BlockTarget } from '$lib/terminal/gating';

	interface Props {
		content: string;
		gallerySources?: ReadonlySet<string>;
		/** Real-time WS stream buffer — updates every delta, not just every poll */
		wsContent?: string;
		timestamp?: Date | string;
		messageUuid?: string | null;  // UUID from JSONL for direct rewind
		sessionId?: string | null;
		streaming?: boolean;
		hasPartialMessages?: boolean;
		fileCheckpointingEnabled?: boolean;  // If true, show option to rewind files too
		metadata?: Record<string, unknown>;
		onRewindComplete?: () => void;
		/** Profile name to display in header (defaults to "Assistant") */
		profileName?: string;
		/** Model display name for this specific message, when the turn reported one. */
		modelLabel?: string | null;
	}

	let {
		content,
		wsContent = '',
		gallerySources,
		timestamp,
		messageUuid = null,  // UUID from JSONL for direct rewind
		sessionId,
		streaming = false,
		hasPartialMessages = true,
		fileCheckpointingEnabled = false,
		metadata,
		onRewindComplete,
		profileName = 'Assistant',
		modelLabel = null
	}: Props = $props();

	/** The name line's text: the model that produced this message when we know
	 *  it, otherwise the profile that was active. */
	const speakerName = $derived(modelLabel || profileName);

	// Determine if this is a final message with cost/duration metadata (query completion)
	const hasFinalMetadata = $derived(Boolean(
		metadata && (metadata.total_cost_usd || metadata.cost_usd || metadata.duration_ms)
	));

	// Show Read button only on final messages with content
	const canRead = $derived(Boolean(!streaming && content && content.length > 0 && hasFinalMetadata));

	// Copy is offered on every settled text block, not only the turn's final
	// one: each block is what the reader sees as one message, and commentary
	// between tool calls is worth copying too. Hidden while streaming so a
	// press never copies a half-written reply.
	const canCopy = $derived(Boolean(!streaming && content?.trim()));
	const showFooter = $derived(Boolean(!streaming && (metadata || canCopy)));

	let isHovered = $state(false);
	let isTouchDevice = $state(false);
	let showActionsOnMobile = $state(false);
	let isReading = $state(false);
	let currentAudio = $state<HTMLAudioElement | null>(null);

	function handleTouchStart() {
		isTouchDevice = true;
	}

	function handleMessageClick(e: MouseEvent) {
		// Don't toggle if clicking on interactive elements (links, buttons, code blocks, etc.)
		const target = e.target as HTMLElement;
		if (target.closest('a, button, pre, code, video, .media-overlay-buttons')) {
			return;
		}
		// Only toggle on touch devices - desktop uses hover
		if (isTouchDevice) {
			showActionsOnMobile = !showActionsOnMobile;
		}
	}

	// Show actions if hovered (desktop) or tapped (mobile)
	const showActions = $derived(isHovered || showActionsOnMobile);

	function formatCost(cost: number | undefined): string {
		if (cost === undefined) return '';
		return `$${cost.toFixed(4)}`;
	}

	// ── Typewriter animation ───────────────────────────────────────────────
	// Smoothly reveals streaming content char-by-char via rAF instead of
	// showing chunky 150ms poll bursts. Purely presentational — does not
	// modify the data pipeline, WS buffer, or single-writer pattern.
	//
	// Uses wsContent (WS buffer, updates every ~50-100ms) when available
	// and longer than poller content, giving sub-poll-interval granularity.
	let _twLength = $state(0);
	const _tw = new Typewriter((len) => { _twLength = len; });

	/** Best available content: WS buffer if ahead, otherwise poller content */
	const _twBestContent = $derived(
		streaming && wsContent && wsContent.length > (content || '').length
			? wsContent
			: (content || '')
	);

	$effect(() => { _tw.setTarget(_twBestContent, streaming); });

	/** Content fed to the markdown renderer — smoothed during streaming. The
	 * reveal length is clamped so a media markdown token is never shown
	 * half-typed; it pops in whole once the cursor passes its end. */
	const displayContent = $derived(
		streaming && _twLength < _twBestContent.length
			? _twBestContent.slice(0, clampRevealToMediaBoundary(_twBestContent, _twLength))
			: _twBestContent
	);

	// ── Incremental markdown rendering ──────────────────────────────────────
	// During streaming, content is only appended. We cache the rendered HTML
	// for completed paragraph blocks and only re-parse the tail, reducing
	// marked() work from O(full_content) to O(last_paragraph) per update.

	// Streaming cursor HTML to inject inline with text
	const STREAMING_CURSOR = '<span class="streaming-cursor inline-block w-2 h-4 ml-0.5 bg-primary animate-pulse align-middle"></span>';

	// Use a plain object to avoid Svelte 5 compiling these as reactive signals.
	// Mutating reactive `let` vars inside `$derived` corrupts the effect linked list.
	// `result.{prefix,tail}` and `stablePrefixHtml` hold SANITIZED markup.
	// Sanitization is folded into the render functions so that during streaming
	// DOMPurify only runs over the growing tail paragraph (and the stable prefix
	// once, when it advances) instead of re-sanitizing the entire message on
	// every delta — which was O(full content) per frame and got heavier as a
	// response grew.
	const _cache = {
		content: '',
		wasStreaming: false,
		stablePrefixContent: '',
		stablePrefixHtml: '',
		highlightGen: 0,
		result: { prefix: '', tail: '' }
	};

	// Rendered output is split into a stable PREFIX (completed paragraphs, whose
	// HTML string is byte-identical across deltas) and a TAIL (the still-growing
	// last paragraph). The template renders them as two separate {@html} blocks;
	// because {@html} only mutates the DOM when its string value changes, any
	// media that has settled into the prefix is NOT torn down and refetched when
	// the tail keeps streaming. That is what stops the image from flickering /
	// reloading while the rest of the message arrives.
	// Shiki loads its core and each grammar lazily, so a settled message can
	// render with plain-text code blocks and then have its grammar arrive. The
	// generation counters are plain module state; this reactive epoch is what
	// makes the $derived re-run, and `highlightGen` in the memo key is what
	// stops it returning the stale plain-text HTML. A message with no fences
	// has generation 0 forever and is never invalidated.
	let highlightEpoch = $state(0);
	const _stopHighlightWatch = onHighlighterReady(() => { highlightEpoch += 1; });
	onDestroy(_stopHighlightWatch);

	const unscopedRendered = $derived(gallerySources?.size && !streaming
		? { prefix: sanitizeHtml(renderChatMarkdown(rewriteWithGallery(displayContent, gallerySources, sessionId), true)), tail: '' }
		: renderMarkdownMemoized(displayContent, streaming, highlightEpoch));
	const rendered = $derived({
		prefix: routeChatFiles(unscopedRendered.prefix, sessionId),
		tail: routeChatFiles(unscopedRendered.tail, sessionId)
	});

	// Task-list checkboxes (#1813). The markdown renders them disabled; a
	// settled message enables them and lays the owner's ticks, kept as account
	// data, over what the text says. A still-streaming message stays read-only:
	// its text, and so its key, is not final yet.
	let proseEl = $state<HTMLElement | null>(null);
	let taskState = $state.raw<TaskChecks>(taskChecks.store.current);
	$effect(() => taskChecks.store.subscribe((value) => { taskState = value; }));
	const taskKey = $derived(!streaming && sessionId && content ? taskMessageKey(sessionId, content) : null);

	function taskBoxes(): HTMLInputElement[] {
		return proseEl ? Array.from(proseEl.querySelectorAll<HTMLInputElement>('input[type="checkbox"]')) : [];
	}

	$effect(() => {
		void rendered; // the {@html} blocks replaced the boxes; re-apply
		const key = taskKey;
		const ticks = key ? taskState[key] : undefined;
		taskBoxes().forEach((box, index) => {
			box.disabled = !key;
			const tick = ticks?.[String(index)];
			if (tick !== undefined) box.checked = tick;
		});
	});

	// Shell blocks in a settled reply get Open terminal and Open and paste
	// (#1945). The command comes from the registry by the block's id, never
	// from the page, and the buttons only appear for a viewer the worker
	// lets open terminals. Nothing runs until the owner presses Enter.
	const RUN_ICON = '<svg width="13" height="13" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><path d="m7 11 2-2-2-2"/><path d="M11 13h4"/><rect width="18" height="18" x="3" y="3" rx="2"/></svg>';
	const PASTE_ICON = '<svg width="13" height="13" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><path d="M15 2H9a1 1 0 0 0-1 1v2c0 .6.4 1 1 1h6c.6 0 1-.4 1-1V3c0-.6-.4-1-1-1Z"/><path d="M8 4H6a2 2 0 0 0-2 2v14a2 2 0 0 0 2 2h12a2 2 0 0 0 2-2M16 4h2a2 2 0 0 1 2 2v2M11 14h10"/><path d="m17 10 4 4-4 4"/></svg>';

	function runButton(act: 'open' | 'paste', reason: string | null, target: string): HTMLButtonElement {
		const button = document.createElement('button');
		button.type = 'button';
		button.className = 'code-run-btn';
		button.dataset.act = act;
		const label = act === 'open' ? 'Open terminal' : 'Open and paste';
		button.innerHTML = (act === 'open' ? RUN_ICON : PASTE_ICON) + `<span class="cb-run-label"></span>`;
		(button.lastElementChild as HTMLElement).textContent = label;
		button.setAttribute('aria-label', label);
		if (reason) {
			button.setAttribute('aria-disabled', 'true');
			button.title = reason;
		} else {
			button.title = act === 'open'
				? `Open a terminal here${target ? ` (${target})` : ''}`
				: `Type this command at the prompt${target ? ` (${target})` : ''}. It runs only when you press Enter.`;
		}
		return button;
	}

	$effect(() => {
		void rendered; // {@html} replaced the blocks; re-decorate
		const root = proseEl;
		const sid = sessionId;
		if (!root || streaming || !sid) return;
		const blocks = Array.from(root.querySelectorAll<HTMLElement>('.codeblock[data-cb-id]'));
		if (!blocks.length) return;
		const probe = terminals.probeFor(sid);
		if (probe.status === 'idle') untrack(() => void terminals.ensureProbe(sid));
		for (const node of blocks) {
			const head = node.querySelector('.codehead');
			const copy = head?.querySelector('.code-copy-btn');
			head?.querySelectorAll('.code-run-btn, .cb-target').forEach((el) => el.remove());
			node.querySelectorAll(':scope > .cb-where').forEach((el) => el.remove());
			const block = lookupCommandBlock(node.dataset.cbId);
			if (!head || !copy || !block) continue;
			const access = blockAccess(block, probe);
			if (access.kind === 'hidden') continue;
			const reason = access.kind === 'disabled' ? access.reason : null;
			const target = targetLine(block, probe);
			const where = blockTarget(block, probe);
			const folderRefused = block.cwd ? cwdProblem(block.cwd) : null;
			if (folderRefused) {
				// Never echo a refused folder: its characters may display as
				// something else. Say why the buttons are off instead.
				head.after(whereRow(null, folderRefused));
			} else if (where?.fromFence && !reason) {
				head.after(whereRow(where, null));
			} else if (where && !reason) {
				head.insertBefore(targetStrip(where), copy);
			}
			head.insertBefore(runButton('open', reason, target), copy);
			head.insertBefore(runButton('paste', reason, target), copy);
		}
	});

	function span(className: string, text: string): HTMLSpanElement {
		const el = document.createElement('span');
		el.className = className;
		el.textContent = text;
		return el;
	}

	/** "Unraid · app · bash" in the head. The folder shrinks first, so the worker stays whole. */
	function targetStrip(where: BlockTarget): HTMLSpanElement {
		const strip = span('cb-target', '');
		strip.title = [where.worker, where.path, where.shell].filter(Boolean).join(' · ');
		strip.append(span('cb-t-worker', where.worker));
		if (where.folder) strip.append(span('cb-t-sep', ' · '), span('cb-t-folder', where.folder));
		if (where.shell) strip.append(span('cb-t-sep', ' · '), span('cb-t-shell', where.shell));
		return strip;
	}

	/**
	 * A folder the fence asked for gets its own row on every screen size: the
	 * whole path, wrapped and never cut, the worker and shell, and a flag when
	 * it is not this chat's folder or has characters that need a second look
	 * (those show as code points). Or, for a refused folder, the reason.
	 */
	function whereRow(where: BlockTarget | null, refused: string | null): HTMLDivElement {
		const row = document.createElement('div');
		row.className = 'cb-where';
		if (refused || !where) {
			row.classList.add('refused');
			row.setAttribute('role', 'note');
			row.append(span('cb-where-flag', "Can't open"), span('cb-where-reason', refused ?? ''));
			return row;
		}
		if (where.outside) row.classList.add('outside');
		if (where.unusual) row.classList.add('unusual');
		row.append(span('cb-where-label', 'Opens in'));
		const path = span('cb-where-path', '');
		path.title = printableFolder(where.path);
		for (const piece of folderPieces(where.path)) {
			if (piece.kind === 'text') {
				path.append(piece.text);
				continue;
			}
			const chip = span('cb-where-cp', piece.code);
			chip.title = `Character ${piece.code}`;
			path.append(chip);
		}
		row.append(path, span('cb-where-meta', [where.worker, where.shell].filter(Boolean).join(' · ')));
		if (where.unusual) row.append(span('cb-where-flag', 'Unusual characters in this folder name'));
		if (where.outside) row.append(span('cb-where-flag', "Outside this chat's folder"));
		return row;
	}

	function handleTaskChange(e: Event) {
		const box = e.target as HTMLInputElement | null;
		if (!taskKey || box?.type !== 'checkbox') return;
		const index = taskBoxes().indexOf(box);
		if (index >= 0) taskChecks.setChecked(taskKey, index, box.checked);
	}

	// `_epoch` is unused by the body: it exists so the $derived above tracks the
	// highlighter-readiness signal as a dependency.
	function renderMarkdownMemoized(
		text: string,
		isStreaming: boolean,
		_epoch: number = 0
	): { prefix: string; tail: string } {
		const gen = highlightGenerationForSource(text);
		const prevGen = _cache.highlightGen;
		// Fast path: content, streaming flag AND available highlighting unchanged
		if (
			text === _cache.content &&
			isStreaming === _cache.wasStreaming &&
			gen === prevGen
		) {
			return _cache.result;
		}
		_cache.highlightGen = gen;

		// Transition from streaming→not-streaming: full re-render for final media processing
		if (_cache.wasStreaming && !isStreaming) {
			_cache.stablePrefixContent = '';
			_cache.stablePrefixHtml = '';
			_cache.content = text;
			_cache.wasStreaming = false;
			_cache.result = { prefix: renderMarkdownFull(text, false), tail: '' };
			return _cache.result;
		}

		// During streaming: incremental render if content was only appended
		if (
			isStreaming &&
			_cache.content.length > 0 &&
			text.startsWith(_cache.content) &&
			gen === prevGen
		) {
			_cache.result = renderMarkdownIncremental(text);
			_cache.content = text;
			_cache.wasStreaming = true;
			return _cache.result;
		}

		// Full render (first render, content rewritten, or non-streaming).
		// While streaming with no cached prefix yet, the whole thing is the tail
		// (carries the streaming cursor); otherwise it is one settled block.
		_cache.stablePrefixContent = '';
		_cache.stablePrefixHtml = '';
		_cache.content = text;
		_cache.wasStreaming = isStreaming;
		_cache.result = isStreaming
			? { prefix: '', tail: renderMarkdownFull(text, true) }
			: { prefix: renderMarkdownFull(text, false), tail: '' };
		return _cache.result;
	}

	/**
	 * Incremental render: reuse cached HTML for completed paragraphs,
	 * only re-parse from the last paragraph boundary onwards. Returns the
	 * stable prefix and the growing tail separately so the template can render
	 * them as independent {@html} blocks (settled media stays put).
	 */
	function renderMarkdownIncremental(text: string): { prefix: string; tail: string } {
		// Find the last paragraph boundary (\n\n) in the text.
		// Everything before it is "stable" — its markdown structure is complete.
		const lastBoundary = text.lastIndexOf('\n\n');

		if (lastBoundary <= 0) {
			// No paragraph boundary yet — full parse (content is short)
			const html = renderChatMarkdown(rewriteMediaMarkdown(text), false);
			return { prefix: '', tail: sanitizeHtml(injectStreamingCursor(html)) };
		}

		const stableContent = text.substring(0, lastBoundary);
		const tailContent = text.substring(lastBoundary);

		// If the stable prefix hasn't changed, reuse its cached (sanitized) HTML
		let prefixHtml: string;
		if (stableContent === _cache.stablePrefixContent) {
			prefixHtml = _cache.stablePrefixHtml;
		} else {
			// Stable prefix grew — re-parse, sanitize, and cache it once. Only this
			// side gets syntax highlighting: its fences are closed, and Shiki's
			// memo makes the repeat renders cheap.
			prefixHtml = sanitizeHtml(renderChatMarkdown(rewriteMediaMarkdown(stableContent), true));
			_cache.stablePrefixContent = stableContent;
			_cache.stablePrefixHtml = prefixHtml;
		}

		// Always re-parse the tail (last incomplete paragraph + new content).
		// This is the only DOMPurify pass that runs every delta — bounded to the
		// last paragraph rather than the whole message. Highlighting is off here:
		// the fence may not be closed yet, and tokenising per delta would be the
		// most expensive thing in the stream loop.
		let tailHtml = renderChatMarkdown(rewriteMediaMarkdown(tailContent), false);
		tailHtml = sanitizeHtml(injectStreamingCursor(tailHtml));

		return { prefix: prefixHtml, tail: tailHtml };
	}

	/**
	 * Inject the streaming cursor inside the last text-containing element
	 * so it appears inline with the text instead of below block elements.
	 */
	function injectStreamingCursor(html: string): string {
		const inlineClosingTags = ['</p>', '</li>', '</td>', '</th>', '</span>', '</strong>', '</em>', '</a>', '</code>'];

		let lastIndex = -1;
		for (const tag of inlineClosingTags) {
			const idx = html.lastIndexOf(tag);
			if (idx > lastIndex) {
				lastIndex = idx;
			}
		}

		if (lastIndex !== -1) {
			return html.slice(0, lastIndex) + STREAMING_CURSOR + html.slice(lastIndex);
		}
		return html + STREAMING_CURSOR;
	}

	/**
	 * Full markdown render (non-streaming or first render). Media markdown
	 * (images / video / audio) is rewritten into inline players with download +
	 * expand controls in both cases, so generated media shows up immediately —
	 * the only difference while streaming is the trailing cursor.
	 * See $lib/utils/media for detection rules and produced markup.
	 */
	function renderMarkdownFull(text: string, isStreaming: boolean): string {
		const html = renderChatMarkdown(rewriteMediaMarkdown(text), !isStreaming);
		return isStreaming ? sanitizeHtml(injectStreamingCursor(html)) : sanitizeHtml(html);
	}

	// ── Code-block copy ────────────────────────────────────────────────────
	// Delegated rather than inline: the markup comes out of {@html}, so there is
	// no component instance to bind to, and an inline onclick would have to
	// survive DOMPurify. Reading the code back out of the rendered <pre> also
	// means the button always copies exactly what is on screen.
	let copyError = $state('');
	const _copyTimers = new Set<ReturnType<typeof setTimeout>>();

	async function handleProseClick(e: MouseEvent) {
		const target = e.target as HTMLElement | null;
		const run = target?.closest?.('.code-run-btn') as HTMLElement | null;
		if (run) {
			e.preventDefault();
			e.stopPropagation();
			if (run.getAttribute('aria-disabled') === 'true' || !sessionId) return;
			const block = lookupCommandBlock(run.closest<HTMLElement>('.codeblock')?.dataset.cbId);
			if (block) terminals.request(sessionId, run.dataset.act === 'paste' ? 'paste' : 'open', block, run);
			return;
		}
		const btn = target?.closest?.('.code-copy-btn') as HTMLElement | null;
		if (!btn) return;
		e.preventDefault();
		e.stopPropagation();
		const code = btn.closest('.codeblock')?.querySelector('pre code')?.textContent ?? '';
		if (!code) return;
		copyError = '';
		try {
			await copyText(code);
		} catch {
			btn.classList.remove('is-copied');
			btn.title = COPY_ERROR_MESSAGE;
			copyError = COPY_ERROR_MESSAGE;
			return;
		}
		btn.removeAttribute('title');
		btn.classList.add('is-copied');
		const timer = setTimeout(() => {
			btn.classList.remove('is-copied');
			_copyTimers.delete(timer);
		}, 1200);
		_copyTimers.add(timer);
	}

	/**
	 * Strip markdown/HTML and prepare text for TTS
	 */
	function prepareTextForTTS(text: string): string {
		// Remove code blocks
		let cleaned = text.replace(/```[\s\S]*?```/g, ' code block ');
		// Remove inline code
		cleaned = cleaned.replace(/`[^`]+`/g, ' code ');
		// Remove image markdown
		cleaned = cleaned.replace(/!\[[^\]]*\]\([^)]+\)/g, '');
		// Remove link markdown but keep text
		cleaned = cleaned.replace(/\[([^\]]+)\]\([^)]+\)/g, '$1');
		// Remove bold markers (** or __) but keep text - use non-greedy match
		cleaned = cleaned.replace(/\*\*(.*?)\*\*/g, '$1');
		cleaned = cleaned.replace(/__(.*?)__/g, '$1');
		// Remove italic markers (* or _) but keep text - use non-greedy match
		cleaned = cleaned.replace(/\*(.*?)\*/g, '$1');
		cleaned = cleaned.replace(/_(.*?)_/g, '$1');
		// Remove headers
		cleaned = cleaned.replace(/^#{1,6}\s+/gm, '');
		// Remove horizontal rules
		cleaned = cleaned.replace(/^[-*_]{3,}$/gm, '');
		// Remove numbered list markers (e.g., "1. ", "2. ", "10. ")
		cleaned = cleaned.replace(/^\d+\.\s+/gm, '');
		// Remove bullet list markers (e.g., "- ", "* ", "+ ")
		cleaned = cleaned.replace(/^[-*+]\s+/gm, '');
		// Remove blockquotes
		cleaned = cleaned.replace(/^>\s*/gm, '');
		// Remove table formatting (pipes and alignment markers)
		cleaned = cleaned.replace(/\|/g, ' ');
		cleaned = cleaned.replace(/^[\s\-:]+$/gm, '');

		// Remove emojis (surrogate pairs for emoji ranges)
		cleaned = cleaned.replace(/[\uD83C-\uDBFF][\uDC00-\uDFFF]/g, '');

		// Remove specific unicode symbol ranges that cause TTS issues:
		// - Misc symbols (U+2600-U+26FF) - weather, zodiac, chess, etc.
		// - Dingbats (U+2700-U+27BF) - ornaments, arrows, etc.
		// - Arrows (U+2190-U+21FF)
		// - Mathematical operators (U+2200-U+22FF)
		// - Box drawing (U+2500-U+257F)
		// - Geometric shapes (U+25A0-U+25FF)
		// - Variation selectors (U+FE00-U+FE0F)
		// - Zero-width characters (U+200B-U+200D, U+FEFF)
		cleaned = cleaned.replace(/[\u2600-\u26FF\u2700-\u27BF\u2190-\u21FF\u2200-\u22FF\u2500-\u257F\u25A0-\u25FF\uFE00-\uFE0F\u200B-\u200D\uFEFF]/g, '');

		// Remove common decorative symbols that might appear in text
		cleaned = cleaned.replace(/[•◦▪▫●○◆◇★☆✓✔✗✘✕✖]/g, '');

		// Clean up extra whitespace
		cleaned = cleaned.replace(/\s+/g, ' ').trim();
		// Limit length for TTS (4000 chars max)
		if (cleaned.length > 4000) {
			cleaned = cleaned.substring(0, 4000) + '...';
		}
		return cleaned;
	}

	/**
	 * Handle reading the message with TTS
	 */
	async function handleRead() {
		// If already reading, stop
		if (isReading && currentAudio) {
			currentAudio.pause();
			currentAudio.currentTime = 0;
			currentAudio = null;
			isReading = false;
			return;
		}

		const ttsText = prepareTextForTTS(content);
		if (!ttsText) return;

		isReading = true;
		let audioUrl: string | null = null;

		try {
			const response = await fetch(`${apiBase()}/settings/tts/stream`, withAuth({
				method: 'POST',
				headers: { 'Content-Type': 'application/json' },
				body: JSON.stringify({
					text: ttsText,
					output_format: 'mp3'
				})
			}));

			if (!response.ok) {
				throw new Error(`TTS request failed: ${response.status}`);
			}

			const audioBlob = await response.blob();
			audioUrl = URL.createObjectURL(audioBlob);

			const audio = new Audio(audioUrl);
			currentAudio = audio;

			audio.onended = () => {
				isReading = false;
				currentAudio = null;
				if (audioUrl) URL.revokeObjectURL(audioUrl);
			};

			audio.onerror = () => {
				console.error('Audio playback error');
				isReading = false;
				currentAudio = null;
				if (audioUrl) URL.revokeObjectURL(audioUrl);
			};

			await audio.play();
		} catch (e) {
			console.error('Failed to generate TTS:', e);
			isReading = false;
			currentAudio = null;
			if (audioUrl) URL.revokeObjectURL(audioUrl);
		}
	}

	// Cleanup on component destroy
	onDestroy(() => {
		if (currentAudio) {
			currentAudio.pause();
			currentAudio = null;
		}
		for (const timer of _copyTimers) clearTimeout(timer);
		_copyTimers.clear();
		_tw.destroy();
	});
</script>

{#if copyError}<p role="alert" class="text-sm text-destructive">{copyError}</p>{/if}

<div
	class="w-full overflow-hidden message-container"
	onmouseenter={() => isHovered = true}
	onmouseleave={() => { isHovered = false; showActionsOnMobile = false; }}
	ontouchstart={handleTouchStart}
	ontouchend={(e) => { const target = e.target; if (target instanceof HTMLElement && !target.closest('a, button, pre, code, video, .media-overlay-buttons')) { showActionsOnMobile = !showActionsOnMobile; } }}
	role="article"
>
	<div class="min-w-0 max-w-full">
		<div class="aname" title={formatMessageTime(timestamp)}>
			{#if streaming}
				<span class="live" aria-label="Streaming"></span>
			{/if}
			<span class="aname-text">{speakerName}</span>
		</div>
		<div class="overflow-hidden max-w-full">
			<!-- svelte-ignore a11y_click_events_have_key_events -->
			<!-- svelte-ignore a11y_no_static_element_interactions -->
			<div
				class="prose prose-sm abody max-w-none wrap-break-word"
				style="overflow-wrap: anywhere; word-break: break-word;"
				onclick={handleProseClick}
				onchange={handleTaskChange}
				bind:this={proseEl}
				use:retryImages
			>
				<!-- use:retryImages — a reply image can 404 for a few seconds when the
				     chat is viewed through another worker than the one running it. -->
				{#if content}
					<!-- Two blocks: stable prefix (settled media stays mounted) + streaming tail -->
					{@html rendered.prefix}{@html rendered.tail}
				{:else if streaming && !hasPartialMessages}
					<!-- Only show placeholder dots when partial messages is disabled -->
					<div class="flex gap-1 py-2">
						<span class="w-2 h-2 bg-muted-foreground rounded-full animate-pulse"></span>
						<span class="w-2 h-2 bg-muted-foreground rounded-full animate-pulse" style="animation-delay: 200ms"></span>
						<span class="w-2 h-2 bg-muted-foreground rounded-full animate-pulse" style="animation-delay: 400ms"></span>
					</div>
				{/if}
			</div>
		</div>
		<HtmlPreviews content={_twBestContent} {streaming} {sessionId} />
		{#if showFooter}
			<div class="ameta" class:is-actions-only={!metadata}>
				{#if metadata && timestamp}<span>{formatMessageTime(timestamp)}</span>{/if}
				{#if metadata?.usage_estimated}
					<!-- The turn's token counts (and so its cost) were estimated by
					     the harness: the provider reported no usage, or the reply
					     was stopped before it arrived. -->
					<UsageEstimatedBadge />
				{/if}
				{#if metadata?.total_cost_usd}
					<span>{formatCost(metadata.total_cost_usd as number)}</span>
				{:else if metadata?.cost_usd}
					<span>{formatCost(metadata.cost_usd as number)}</span>
				{/if}
				{#if metadata?.duration_ms}
					<span>{((metadata.duration_ms as number) / 1000).toFixed(1)}s</span>
				{/if}
				{#if metadata?.tokens_out && metadata?.duration_ms && (metadata.duration_ms as number) > 0}
					<span>{((metadata.tokens_out as number) / ((metadata.duration_ms as number) / 1000)).toFixed(1)} t/s</span>
				{/if}
				{#if canCopy}
					<div class="assistant-actions" class:is-visible={showActions}>
						<!-- A block with no stats row (mid-turn, or a reply without
						     metadata) has no visible time; show it with the actions on
						     hover, as the user bubble does. -->
						{#if timestamp && !metadata}<span class="assistant-time">{formatMessageTime(timestamp)}</span>{/if}
						<MessageActions
							{sessionId}
							messageContent={content}
							messageRole="assistant"
							copyAs="markdown"
							{messageUuid}
							{canRead}
							isStreaming={streaming}
							onRead={handleRead}
							{isReading}
							{onRewindComplete}
							{fileCheckpointingEnabled}
						/>
					</div>
				{/if}
			</div>
		{/if}
	</div>
</div>

<style>
	/* A task item shows its checkbox in place of the list bullet (#1813). */
	.abody :global(li:has(> input[type='checkbox'])),
	.abody :global(li:has(> p > input[type='checkbox'])) {
		list-style: none;
	}
	.abody :global(li input[type='checkbox']) {
		margin: 0 0.45em 0 0;
		vertical-align: -0.1em;
	}
	.abody :global(li input[type='checkbox']:not(:disabled)) {
		cursor: pointer;
	}
	.message-container {
		position: relative;
	}

	/* ── Name line ─────────────────────────────────────────────────────── */
	.aname {
		display: flex;
		align-items: center;
		gap: 7px;
		font-size: 11.5px;
		font-weight: 500;
		letter-spacing: 0.02em;
		color: var(--subtle-foreground);
		margin-bottom: 9px;
		min-height: 16px;
	}

	.aname-text {
		white-space: nowrap;
		overflow: hidden;
		text-overflow: ellipsis;
	}

	.live {
		width: 5px;
		height: 5px;
		flex: none;
		border-radius: 50%;
		background: var(--primary);
		box-shadow: 0 0 8px 0 var(--glow);
		animation: aname-pulse 1.4s ease-in-out infinite;
	}

	@keyframes aname-pulse {
		0%, 100% { opacity: 1; }
		50% { opacity: 0.35; }
	}

	@media (prefers-reduced-motion: reduce) {
		.live { animation: none; }
	}

	.ameta {
		margin-top: 10px;
		display: flex;
		align-items: center;
		gap: 12px;
		font-size: 11px;
		font-family: var(--font-mono);
		color: var(--subtle-foreground);
	}

	/* A mid-turn block has no stats, only the copy button: sit it closer to
	   the prose it belongs to, as the user bubble's actions do. */
	.ameta.is-actions-only {
		margin-top: 4px;
	}

	/* Space is always reserved so revealing the controls on hover never
	   reflows the transcript — only opacity changes. Keyboard focus reveals
	   them too, so tabbing onto the button never lands on something invisible. */
	/* Left-aligned with the reply it belongs to, after any stats: the reply
	   starts at the left edge, so its actions do too. */
	.assistant-actions {
		display: flex;
		align-items: center;
		gap: 8px;
		opacity: 0;
		pointer-events: none;
		transition: opacity 0.12s ease;
	}

	.assistant-time {
		font-family: var(--font-mono);
		font-size: 10.5px;
		color: var(--subtle-foreground);
	}

	.assistant-actions.is-visible,
	.assistant-actions:focus-within {
		opacity: 1;
		pointer-events: auto;
	}

	/* Touch: the controls stay visible, and the row is a full 44px with the
	   button inset 6px from the edge. The message container clips overflow,
	   so this is what leaves room for the button's invisible 44px hit area
	   (MessageActions) instead of cutting it off at the right and bottom. */
	@media (max-width: 639px) {
		.ameta {
			min-height: 44px;
			gap: 9px;
		}

		.assistant-actions {
			opacity: 1;
			pointer-events: auto;
			margin-right: 6px;
		}
	}

	@media (prefers-reduced-motion: reduce) {
		.assistant-actions { transition: none; }
	}

	/* ── Code block chrome (markup produced by $lib/utils/chat-markdown) ── */
	.abody :global(.codeblock) {
		margin: 0 0 14px;
		border-radius: 10px;
		border: 1px solid var(--border);
		background: var(--code-bg);
		overflow: hidden;
	}

	.abody :global(.codehead) {
		display: flex;
		align-items: center;
		gap: 7px;
		padding: 6px 8px 6px 11px;
		border-bottom: 1px solid var(--border);
		background: color-mix(in oklch, var(--muted) 45%, transparent);
	}

	.abody :global(.codehead .cb-icon) {
		color: var(--subtle-foreground);
		flex: none;
	}

	.abody :global(.codehead .fname) {
		font-family: var(--font-mono);
		font-size: 11.5px;
		color: var(--muted-foreground);
		margin-right: auto;
		min-width: 0;
		overflow: hidden;
		text-overflow: ellipsis;
		white-space: nowrap;
	}

	.abody :global(.codehead .lang) {
		font-size: 10px;
		letter-spacing: 0.06em;
		text-transform: uppercase;
		color: var(--subtle-foreground);
		padding: 2px 6px;
		border-radius: 5px;
		border: 1px solid var(--border);
		flex: none;
	}

	.abody :global(.code-copy-btn) {
		width: 24px;
		height: 24px;
		flex: none;
		border: none;
		background: transparent;
		border-radius: 6px;
		display: grid;
		place-items: center;
		cursor: pointer;
		color: var(--subtle-foreground);
		transition: background-color 0.13s ease, color 0.13s ease;
	}

	.abody :global(.code-copy-btn:hover) {
		background: var(--accent);
		color: var(--foreground);
	}

	/* Terminal buttons on shell blocks (#1945): text chips before Copy. */
	.abody :global(.codehead .cb-target) {
		display: inline-flex;
		font-family: var(--font-mono);
		font-size: 10.5px;
		color: var(--subtle-foreground);
		white-space: nowrap;
		min-width: 0;
		max-width: 40ch;
		flex: 0 1 auto;
	}

	/* The worker and shell stay whole; only the folder name gives way. */
	.abody :global(.cb-target .cb-t-worker),
	.abody :global(.cb-target .cb-t-shell),
	.abody :global(.cb-target .cb-t-sep) {
		flex: none;
		white-space: pre;
	}

	.abody :global(.cb-target .cb-t-folder) {
		min-width: 2ch;
		overflow: hidden;
		text-overflow: ellipsis;
	}

	/* A folder named by the fence: its own row, full path, every screen size. */
	.abody :global(.cb-where) {
		display: flex;
		flex-wrap: wrap;
		align-items: baseline;
		gap: 2px 8px;
		padding: 5px 11px;
		border-bottom: 1px solid var(--border);
		background: color-mix(in oklch, var(--muted) 25%, transparent);
		font: 11px/1.45 var(--font-mono);
		color: var(--muted-foreground);
	}

	.abody :global(.cb-where-label) {
		font-family: var(--font-sans);
		color: var(--subtle-foreground);
	}

	.abody :global(.cb-where-path) {
		color: var(--foreground);
		overflow-wrap: anywhere;
	}

	/* A character that could hide or pass for ASCII, shown as its code point. */
	.abody :global(.cb-where-cp) {
		display: inline-block;
		margin: 0 1px;
		padding: 0 3px;
		border: 1px solid color-mix(in oklch, var(--warning) 55%, transparent);
		border-radius: 3px;
		background: color-mix(in oklch, var(--warning) 14%, transparent);
		color: var(--warning);
		font-size: 10px;
		line-height: 1.35;
		vertical-align: 1px;
	}

	.abody :global(.cb-where-meta) {
		color: var(--subtle-foreground);
	}

	.abody :global(.cb-where-flag) {
		margin-left: auto;
		padding: 0 6px;
		border: 1px solid color-mix(in oklch, var(--warning) 45%, transparent);
		border-radius: 4px;
		background: color-mix(in oklch, var(--warning) 12%, transparent);
		color: var(--warning);
		font: 600 10.5px/1.6 var(--font-sans);
		white-space: nowrap;
	}

	.abody :global(.cb-where.outside),
	.abody :global(.cb-where.unusual) {
		background: color-mix(in oklch, var(--warning) 7%, transparent);
		box-shadow: inset 2px 0 0 var(--warning);
	}

	.abody :global(.cb-where.refused) {
		font-family: var(--font-sans);
		box-shadow: inset 2px 0 0 var(--warning);
	}

	.abody :global(.cb-where-flag + .cb-where-flag),
	.abody :global(.cb-where.refused .cb-where-flag) {
		margin-left: 0;
	}

	.abody :global(.cb-where-reason) {
		color: var(--foreground);
	}

	.abody :global(.code-run-btn) {
		display: inline-flex;
		align-items: center;
		gap: 5px;
		height: 24px;
		padding: 0 8px;
		flex: none;
		border: 1px solid var(--border);
		border-radius: 6px;
		background: transparent;
		color: var(--muted-foreground);
		font: 500 11.5px var(--font-sans);
		cursor: pointer;
		transition: background-color 0.13s ease, color 0.13s ease, border-color 0.13s ease;
	}

	.abody :global(.code-run-btn:hover:not([aria-disabled='true'])) {
		border-color: color-mix(in oklch, var(--primary) 45%, var(--border));
		background: color-mix(in oklch, var(--primary) 10%, transparent);
		color: var(--foreground);
	}

	.abody :global(.code-run-btn:active:not([aria-disabled='true'])) {
		transform: translateY(1px);
	}

	.abody :global(.code-run-btn:focus-visible),
	.abody :global(.code-copy-btn:focus-visible) {
		outline: 2px solid var(--ring);
		outline-offset: 1px;
	}

	.abody :global(.code-run-btn[aria-disabled='true']) {
		opacity: 0.45;
		cursor: not-allowed;
	}

	.abody :global(.code-run-btn svg) {
		color: var(--primary);
		flex: none;
	}

	@media (max-width: 700px) {
		.abody :global(.codehead .cb-target) { display: none; }
		.abody :global(.code-run-btn) { height: 32px; padding: 0 9px; }
		.abody :global(.code-run-btn[data-act='open'] .cb-run-label) { display: none; }
		.abody :global(.code-copy-btn) { width: 32px; height: 32px; }
	}

	.abody :global(.code-copy-btn .cb-check) {
		display: none;
	}

	.abody :global(.code-copy-btn.is-copied) {
		color: var(--emerald);
	}

	.abody :global(.code-copy-btn.is-copied .cb-copy) {
		display: none;
	}

	.abody :global(.code-copy-btn.is-copied .cb-check) {
		display: block;
	}

	.abody :global(.codeblock .cb-pre) {
		margin: 0;
		padding: 11px 13px 13px;
		overflow-x: auto;
		background: transparent;
		border-radius: 0;
		font-family: var(--font-mono);
		font-size: 12.25px;
		line-height: 1.72;
		color: var(--muted-foreground);
	}

	.abody :global(.codeblock .cb-pre code) {
		font-size: inherit;
		line-height: inherit;
		background: transparent;
		padding: 0;
		border: none;
	}

	/* Shiki emits both palettes as custom properties on every span, so one
	   highlighted string serves both themes and a theme switch never
	   invalidates the highlight cache. */
	.abody :global(.codeblock .cb-pre span) {
		color: var(--shiki-light, inherit);
	}

	:global(html.dark) .abody :global(.codeblock .cb-pre span) {
		color: var(--shiki-dark, inherit);
	}
</style>
