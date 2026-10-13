/** Reconcile provider turns by identity, never by completion arrival order. */
export class SttTranscript {
	private items = new Map<string, { text: string; final: boolean }>();
	private order: string[] = [];
	private legacyId = 0;

	private ensure(id?: string): string {
		const key = id || `legacy-${this.legacyId}`;
		if (!this.items.has(key)) {
			this.items.set(key, { text: '', final: false });
			this.order.push(key);
		}
		return key;
	}

	/** Speech-start/commit events establish order before transcripts finish. */
	start(id?: string, previousId?: string | null): void {
		const key = this.ensure(id);
		if (previousId === undefined) return;
		if (previousId) this.ensure(previousId);
		this.order = this.order.filter((item) => item !== key);
		const at = previousId ? this.order.indexOf(previousId) + 1 : 0;
		this.order.splice(at, 0, key);
	}

	delta(text: string, id?: string): void {
		const item = this.items.get(this.ensure(id))!;
		if (!item.final) item.text += text;
	}

	final(text: string, id?: string): void {
		const item = this.items.get(this.ensure(id))!;
		item.text = text;
		item.final = true;
		if (!id) this.legacyId++;
	}

	getText(): string {
		return this.order.map((id) => this.items.get(id)!.text).join(' ').replace(/\s+/g, ' ').trim();
	}
}
