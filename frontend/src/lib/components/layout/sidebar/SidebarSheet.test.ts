/**
 * The sidebar's phone sheet closes on a downward drag past 70px or a quick
 * flick ($lib/components/ui/sheet-drag); a held pull, a cancelled pointer and
 * the desktop popover never do.
 */
import { afterEach, describe, expect, it, vi } from 'vitest';
import { cleanup, render, screen } from '@testing-library/svelte';
import { createRawSnippet, tick } from 'svelte';
import SidebarSheet from './SidebarSheet.svelte';

const children = createRawSnippet(() => ({ render: () => '<p>Sheet content</p>' }));

function sheet(phone = true) {
	const onClose = vi.fn();
	render(SidebarSheet, { props: { open: true, label: 'More', phone, onClose, children } });
	const node = screen.getByRole('dialog', { name: 'More' }) as HTMLElement & { setPointerCapture: (id: number) => void };
	node.setPointerCapture = vi.fn();
	const pointer = (type: string, y: number, time: number) => {
		const event = new MouseEvent(type, { bubbles: true, cancelable: true, clientX: 40, clientY: y, button: 0 });
		Object.defineProperties(event, {
			pointerType: { value: 'touch' },
			pointerId: { value: 3 },
			isPrimary: { value: true },
			timeStamp: { value: time }
		});
		node.dispatchEvent(event);
		return event;
	};
	return { node, onClose, pointer };
}

afterEach(() => cleanup());

describe('SidebarSheet drag to dismiss', () => {
	it('follows a downward drag and closes past 70px', async () => {
		const { node, onClose, pointer } = sheet();
		pointer('pointerdown', 100, 0);
		pointer('pointermove', 140, 200);
		await tick();
		expect(node.style.getPropertyValue('--sheet-drag')).toBe('40px');
		pointer('pointermove', 190, 400);
		pointer('pointerup', 190, 450);
		expect(onClose).toHaveBeenCalledTimes(1);
	});

	it('closes on a quick flick short of 70px', () => {
		const { onClose, pointer } = sheet();
		pointer('pointerdown', 100, 0);
		pointer('pointermove', 115, 8);
		pointer('pointermove', 130, 16);
		pointer('pointermove', 145, 24);
		pointer('pointerup', 145, 30);
		expect(onClose).toHaveBeenCalledTimes(1);
	});

	it('reads a flick a busy page hands over as one coalesced move (#1637)', () => {
		const { onClose, node } = sheet();
		const at = (type: string, y: number, time: number) => {
			const event = new MouseEvent(type, { bubbles: true, cancelable: true, clientX: 40, clientY: y, button: 0 });
			Object.defineProperties(event, { pointerType: { value: 'touch' }, pointerId: { value: 3 }, isPrimary: { value: true }, timeStamp: { value: time } });
			return event;
		};
		node.dispatchEvent(at('pointerdown', 100, 0));
		// A 100ms rest, then 45px in 24ms delivered as one pointermove.
		const move = at('pointermove', 145, 124);
		Object.defineProperty(move, 'getCoalescedEvents', { value: () => [at('pointermove', 115, 108), at('pointermove', 130, 116), at('pointermove', 145, 124)] });
		node.dispatchEvent(move);
		node.dispatchEvent(at('pointerup', 145, 126));
		expect(onClose).toHaveBeenCalledTimes(1);
	});

	it('springs back from the same pull held still before letting go', async () => {
		const { node, onClose, pointer } = sheet();
		pointer('pointerdown', 100, 0);
		pointer('pointermove', 115, 8);
		pointer('pointermove', 130, 16);
		pointer('pointermove', 145, 24);
		pointer('pointerup', 145, 1024);
		await tick();
		expect(onClose).not.toHaveBeenCalled();
		expect(node.style.getPropertyValue('--sheet-drag')).toBe('0px');
	});

	it('a cancelled pointer never dismisses, however far it pulled', async () => {
		const { node, onClose, pointer } = sheet();
		pointer('pointerdown', 100, 0);
		pointer('pointermove', 260, 200);
		pointer('pointercancel', 260, 210);
		await tick();
		expect(onClose).not.toHaveBeenCalled();
		expect(node.style.getPropertyValue('--sheet-drag')).toBe('0px');
	});

	it('an upward move is left to the content', () => {
		const { node, onClose, pointer } = sheet();
		pointer('pointerdown', 300, 0);
		expect(pointer('pointermove', 200, 100).defaultPrevented).toBe(false);
		pointer('pointerup', 200, 150);
		expect(onClose).not.toHaveBeenCalled();
		expect(node.setPointerCapture).not.toHaveBeenCalled();
	});

	it('the desktop popover does not drag', () => {
		const { onClose, pointer } = sheet(false);
		pointer('pointerdown', 100, 0);
		expect(pointer('pointermove', 300, 100).defaultPrevented).toBe(false);
		pointer('pointerup', 300, 120);
		expect(onClose).not.toHaveBeenCalled();
	});
});
