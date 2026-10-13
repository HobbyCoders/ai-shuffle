/** Window bounds and a few shell preferences, persisted as JSON in userData. */
import { promises as fs } from 'node:fs';
import path from 'node:path';
import { screen, type BrowserWindow, type Rectangle } from 'electron';

export interface WindowState {
	bounds: Rectangle | null;
	maximized: boolean;
}

export class WindowStateStore {
	private state: WindowState = { bounds: null, maximized: false };

	constructor(private readonly file: string) {}

	async load(): Promise<WindowState> {
		try {
			const parsed = JSON.parse(await fs.readFile(this.file, 'utf8')) as Partial<WindowState>;
			const bounds = parsed.bounds;
			this.state = {
				bounds:
					bounds &&
					typeof bounds.x === 'number' &&
					typeof bounds.y === 'number' &&
					typeof bounds.width === 'number' &&
					typeof bounds.height === 'number'
						? bounds
						: null,
				maximized: parsed.maximized === true
			};
		} catch {
			this.state = { bounds: null, maximized: false };
		}
		return this.visible();
	}

	/** Drops saved bounds that no longer intersect a connected display. */
	private visible(): WindowState {
		const bounds = this.state.bounds;
		if (!bounds) return this.state;
		const onScreen = screen.getAllDisplays().some((display) => {
			const area = display.workArea;
			return (
				bounds.x < area.x + area.width &&
				bounds.x + bounds.width > area.x &&
				bounds.y < area.y + area.height &&
				bounds.y + bounds.height > area.y
			);
		});
		return onScreen ? this.state : { bounds: null, maximized: this.state.maximized };
	}

	track(win: BrowserWindow): void {
		let timer: NodeJS.Timeout | null = null;
		const capture = () => {
			if (win.isDestroyed()) return;
			this.state = {
				bounds: win.isMaximized() || win.isFullScreen() ? this.state.bounds : win.getNormalBounds(),
				maximized: win.isMaximized()
			};
			if (timer) clearTimeout(timer);
			timer = setTimeout(() => void this.save(), 400);
		};
		win.on('resize', capture);
		win.on('move', capture);
		win.on('maximize', capture);
		win.on('unmaximize', capture);
		win.on('close', () => {
			capture();
			void this.save();
		});
	}

	private async save(): Promise<void> {
		try {
			await fs.mkdir(path.dirname(this.file), { recursive: true });
			await fs.writeFile(this.file, JSON.stringify(this.state), 'utf8');
		} catch {
			/* Losing window placement is not worth surfacing. */
		}
	}
}
