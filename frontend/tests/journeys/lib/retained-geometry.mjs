// What a rewind may do to the reply it keeps, judged over every painted frame.
import { expect } from './app.mjs';

/**
 * The expected shrink: the retained reply comes into view once, at its final
 * place, and stays there, and the composer's bottom edge never moves. Scrolling
 * while it is still off screen (focus reaching the rewound row) is not a jump.
 */
export function expectRetainedHolds(geometry, log) {
	expect(geometry.every((f) => !f.gone), 'the retained reply stayed on the page through the rewind');
	const bottoms = geometry.map((f) => f.bottom);
	expect(Math.max(...bottoms) - Math.min(...bottoms) <= 1, `the composer's bottom edge moved: ${Math.min(...bottoms)}..${Math.max(...bottoms)}`);
	// One range over every visible frame: comparing neighbours would pass a slow drift.
	const visible = geometry.filter((f) => f.top + f.height > 0 && f.top < f.viewport).map((f) => f.top);
	expect(Math.max(...visible) - Math.min(...visible) <= 1, `the retained reply moved while on screen: ${Math.min(...visible)}..${Math.max(...visible)}`);
	const last = geometry.at(-1);
	expect(last.top + last.height > 0 && last.top < last.viewport, 'the retained reply is in view after the rewind');
	log(`Retained reply settled at ${Math.round(last.top)}px over ${geometry.length} painted frames; composer bottom held.`);
}
