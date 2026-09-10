/** Per-popover memory: detached labels retain their last visible value, not live updates. */
export function createUsageNumberAnimation() {
	const seen = new Map<string, string>();
	const numberPattern = /-?\d+(?:\.\d+)?/g;

	function interpolate(from: string, to: string): ((progress: number) => string) | null {
		// Never count between different units or meanings (e.g. dollars used vs left).
		if (from.replace(numberPattern, '#') !== to.replace(numberPattern, '#')) return null;
		const old = from.match(numberPattern)?.map(Number) ?? [];
		const next = to.match(numberPattern) ?? [];
		if (!old.length || old.length !== next.length) return null;
		return (progress) => {
			let index = 0;
			return to.replace(numberPattern, (token) => {
				const start = old[index++];
				const decimals = token.split('.')[1]?.length ?? 0;
				return (start + (Number(token) - start) * progress).toFixed(decimals);
			});
		};
	}

	return function usageNumber(node: HTMLElement, initial: { key: string; text: string }) {
		let current = initial;
		let frame: number | undefined;
		const motion = window.matchMedia?.('(prefers-reduced-motion: reduce)');
		const render = (text: string) => {
			node.textContent = text;
			seen.set(current.key, text);
		};
		const stop = () => {
			if (frame !== undefined) cancelAnimationFrame(frame);
			frame = undefined;
		};
		const finish = () => { stop(); render(current.text); };
		const reducedMotionChanged = () => { if (motion?.matches) finish(); };
		motion?.addEventListener('change', reducedMotionChanged);
		function animate() {
			stop();
			const previous = seen.get(current.key);
			const tween = previous && previous !== current.text ? interpolate(previous, current.text) : null;
			if (tween && !motion?.matches) {
				render(previous!);
				let started: number | undefined;
				const step = (time: number) => {
					started ??= time;
					const progress = Math.min((time - started) / 700, 1);
					render(progress === 1 ? current.text : tween(1 - (1 - progress) ** 3));
					frame = progress < 1 ? requestAnimationFrame(step) : undefined;
				};
				frame = requestAnimationFrame(step);
			} else {
				render(current.text);
			}
		}
		animate();
		return {
			update(next: { key: string; text: string }) {
				// Countdown/store refreshes must not cut short an unchanged animation.
				if (next.key === current.key && next.text === current.text) return;
				const sameKey = next.key === current.key;
				current = next;
				// Refresh-on-open may return after mounting. Retarget from the visible value.
				if (sameKey) animate();
				else finish();
			},
			destroy() {
				stop();
				motion?.removeEventListener('change', reducedMotionChanged);
			}
		};
	};
}
