import './usage-number-animation.css';

const STORAGE_KEY = 'aihub.usageLastShown.v1';
const OPEN_DELAY = 450;
const FLIP_DURATION = 1200;
const numberPattern = /-?\d+(?:\.\d+)?/g;

/** Remember only values actually displayed, never background usage updates. */
export function createUsageNumberAnimation() {
	const seen = new Map<string, string>();
	function recall(key: string): string | undefined {
		try {
			const entries: unknown = JSON.parse(localStorage.getItem(STORAGE_KEY) ?? '[]');
			if (Array.isArray(entries)) {
				for (const entry of entries.slice(-200)) {
					if (Array.isArray(entry) && entry.length === 2 &&
						typeof entry[0] === 'string' && typeof entry[1] === 'string') {
						seen.set(entry[0], entry[1]);
					}
				}
			}
		} catch { /* Storage may be disabled; mounted-panel memory still works. */ }
		return seen.get(key);
	}
	function remember(key: string, text: string) {
		seen.delete(key);
		seen.set(key, text);
		while (seen.size > 200) seen.delete(seen.keys().next().value!);
		try { localStorage.setItem(STORAGE_KEY, JSON.stringify([...seen])); } catch { /* Optional cache. */ }
	}

	return function usageNumber(node: HTMLElement, initial: { key: string; text: string }) {
		let current = initial;
		let visible = initial.text;
		let frame: number | undefined;
		const motion = window.matchMedia?.('(prefers-reduced-motion: reduce)');
		const label = document.createElement('span');
		label.className = 'usage-number-label';
		const clock = document.createElement('span');
		clock.className = 'usage-flip-clock';
		clock.setAttribute('aria-hidden', 'true');
		const shell = document.createElement('span');
		shell.className = 'usage-number';
		shell.append(label, clock);
		node.replaceChildren(shell);
		const stop = () => {
			if (frame !== undefined) cancelAnimationFrame(frame);
			frame = undefined;
		};
		const render = (text: string) => {
			visible = text;
			label.textContent = text;
			clock.replaceChildren();
			shell.classList.remove('usage-number-flipping');
		};
		const finish = () => { stop(); render(current.text); remember(current.key, visible); };
		const reducedMotionChanged = () => { if (motion?.matches) finish(); };
		motion?.addEventListener('change', reducedMotionChanged);

		function animate(previous: string | undefined) {
			stop();
			const compatible = previous && previous !== current.text &&
				previous.replace(numberPattern, '#') === current.text.replace(numberPattern, '#') &&
				current.text.match(numberPattern);
			if (!compatible || motion?.matches) { finish(); return; }
			render(previous!);
			// Pad numeric runs so 9 → 10 and 100 → 4 retain stable digit columns.
			const oldNumbers = previous!.match(numberPattern)!;
			let index = 0;
			const flaps: { old: HTMLElement; next: HTMLElement; up: boolean }[] = [];
			for (const part of current.text.split(/(-?\d+(?:\.\d+)?)/)) {
				if (!/^-?\d/.test(part)) { clock.append(document.createTextNode(part)); continue; }
				const oldNumber = oldNumbers[index++];
				const width = Math.max(oldNumber.length, part.length);
				const from = oldNumber.padStart(width, ' ');
				const to = part.padStart(width, ' ');
				const up = Number(part) > Number(oldNumber);
				for (let digit = 0; digit < width; digit++) {
					const tile = document.createElement('span');
					tile.className = 'usage-flip-digit';
					const half = (text: string, side: string) => {
						const el = document.createElement('span');
						el.className = `usage-flip-half ${side}`;
						const face = document.createElement('span');
						face.textContent = text;
						el.append(face); tile.append(el); return el;
					};
					if (from[digit] === to[digit]) {
						half(to[digit], 'top'); half(to[digit], 'bottom');
					} else {
						half(to[digit], up ? 'bottom' : 'top');
						half(from[digit], up ? 'top' : 'bottom');
						const old = half(from[digit], `${up ? 'bottom' : 'top'} flap`);
						const next = half(to[digit], `${up ? 'top' : 'bottom'} flap`);
						flaps.push({ old, next, up });
					}
					clock.append(tile);
				}
			}
			shell.classList.add('usage-number-flipping');
			node.dataset.flipDirection = Number(current.text.match(numberPattern)![0]) > Number(oldNumbers[0]) ? 'up' : 'down';
			function pose(progress: number) {
				for (const { old, next, up } of flaps) {
					const sign = up ? 1 : -1;
					old.style.transform = `rotateX(${sign * 90 * Math.min(progress * 2, 1)}deg)`;
					next.style.transform = `rotateX(${-sign * 90 * (1 - Math.max(0, progress * 2 - 1))}deg)`;
					old.style.visibility = progress < 0.5 ? 'visible' : 'hidden';
					next.style.visibility = progress >= 0.5 ? 'visible' : 'hidden';
				}
			}
			pose(0);
			let started: number | undefined;
			const step = (time: number) => {
				started ??= time;
				const progress = Math.min(Math.max((time - started - OPEN_DELAY) / FLIP_DURATION, 0), 1);
				pose(progress);
				// The new value becomes readable as the second flap drops into place.
				if (progress >= 0.5) { visible = current.text; label.textContent = visible; }
				if (progress === 1) finish();
				else frame = requestAnimationFrame(step);
			};
			frame = requestAnimationFrame(step);
		}
		animate(recall(current.key));
		return {
			update(next: { key: string; text: string }) {
				if (next.key === current.key && next.text === current.text) return;
				remember(current.key, visible);
				const previous = next.key === current.key ? visible : recall(next.key);
				current = next;
				animate(previous);
			},
			destroy() {
				stop(); remember(current.key, visible);
				motion?.removeEventListener('change', reducedMotionChanged);
			}
		};
	};
}
