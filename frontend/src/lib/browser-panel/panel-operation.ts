/** Event-driven waits owned by one open panel connection. */
export class PanelOperation {
	readonly controller = new AbortController();
	private listeners = new Set<() => void>();
	notify() { for (const listener of [...this.listeners]) listener(); }
	close() { this.controller.abort(); this.notify(); }
	wait(check: () => boolean, message: string, timeout = 20_000): Promise<void> {
		return new Promise((resolve, reject) => {
			const finish = (error?: unknown) => {
				clearTimeout(timer);
				this.listeners.delete(test);
				error ? reject(error) : resolve();
			};
			const test = () => {
				try {
					if (this.controller.signal.aborted) throw new Error('Browser panel closed.');
					if (check()) finish();
				} catch (error) { finish(error); }
			};
			const timer = setTimeout(() => finish(new Error(message)), timeout);
			this.listeners.add(test);
			test();
		});
	}
}
