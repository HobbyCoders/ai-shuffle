/**
 * The "Saved" tick. Call `flashSaved()` after an auto-saved change lands; the
 * `SavedTick` in the visible page or detail header shows for 1.6s.
 */

export const SAVED_VISIBLE_MS = 1600;

class SaveStatus {
	visible = $state(false);
	/** Bumps on every flash, so a second save restarts the timer. */
	count = $state(0);
	#timer: ReturnType<typeof setTimeout> | null = null;

	flash() {
		this.count += 1;
		this.visible = true;
		if (this.#timer) clearTimeout(this.#timer);
		this.#timer = setTimeout(() => {
			this.visible = false;
			this.#timer = null;
		}, SAVED_VISIBLE_MS);
	}

	reset() {
		if (this.#timer) clearTimeout(this.#timer);
		this.#timer = null;
		this.visible = false;
	}
}

export const saveStatus = new SaveStatus();

export function flashSaved(): void {
	saveStatus.flash();
}
