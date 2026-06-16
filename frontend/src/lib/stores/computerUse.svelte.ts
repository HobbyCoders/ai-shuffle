/**
 * Shared computer-use status store.
 *
 * Polls `/api/v1/computer-use/status` and exposes live state consumed by the
 * toolbar toggle (ControlsDock) and the "AI Shuffle is controlling your screen"
 * banner (ComputerUseBanner). Polling is ref-counted so the two consumers share
 * a single interval, and only runs while at least one is mounted with the
 * capability enabled.
 */
import { getComputerUseStatus, stopComputerUse, resumeComputerUse } from '$lib/api/computerUse';
import { toast } from '$lib/stores/toast';

class ComputerUseStore {
	available = $state(true);
	reason = $state('ok');
	stopped = $state(false);
	active = $state(false);
	display = $state<number | null>(null);
	displayLabel = $state<string | null>(null);

	#timer: ReturnType<typeof setInterval> | null = null;
	#subscribers = 0;
	#wasActive = false;

	async refresh() {
		try {
			const s = await getComputerUseStatus();
			this.available = s.available;
			this.reason = s.reason;
			this.stopped = s.stopped;
			this.active = s.active;
			this.display = s.display;
			this.displayLabel = s.display_label;
			// Toast on the rising edge of control activity, so the user is told
			// the moment the agent starts driving a screen (and which one).
			if (s.active && !this.#wasActive) {
				toast.push(`AI Shuffle is controlling ${s.display_label ?? 'your screen'}`, {
					tone: 'info',
					duration_ms: 4000
				});
			}
			this.#wasActive = s.active;
		} catch {
			// Keep last-known state on transient errors.
		}
	}

	/** Begin polling. Ref-counted: multiple components share one interval. */
	start() {
		this.#subscribers++;
		if (!this.#timer) {
			this.refresh();
			this.#timer = setInterval(() => this.refresh(), 2000);
		}
	}

	/** Stop polling when the last consumer unmounts. */
	stop() {
		this.#subscribers = Math.max(0, this.#subscribers - 1);
		if (this.#subscribers === 0 && this.#timer) {
			clearInterval(this.#timer);
			this.#timer = null;
		}
	}

	/** Toggle the emergency stop (pause/resume all computer-use actions). */
	async toggleStopped() {
		try {
			const res = this.stopped ? await resumeComputerUse() : await stopComputerUse();
			this.stopped = res.stopped;
			await this.refresh();
		} catch (e) {
			console.warn('computer-use stop/resume failed', e);
		}
	}
}

export const computerUse = new ComputerUseStore();
