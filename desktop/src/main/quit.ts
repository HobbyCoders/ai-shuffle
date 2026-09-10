export interface QuitActions {
	activeSessionCount(): Promise<number>;
	confirm(activeCount: number | null): Promise<boolean>;
	stop(): Promise<void>;
	quit(): void;
	failed(error: unknown): Promise<void>;
}

/** Serialize close requests so a second click cannot bypass confirmation. */
export class QuitGuard {
	private pending = false;
	private approved = false;

	constructor(private readonly actions: QuitActions) {}

	get canQuit(): boolean { return this.approved; }

	async request(): Promise<void> {
		if (this.pending || this.approved) return;
		this.pending = true;
		try {
			let active: number | null;
			try { active = await this.actions.activeSessionCount(); }
			catch { active = null; }
			if (active !== 0 && !(await this.actions.confirm(active))) return;
			await this.actions.stop();
			this.approved = true;
			this.actions.quit();
		} catch (error) {
			await this.actions.failed(error);
		} finally {
			this.pending = false;
		}
	}
}
