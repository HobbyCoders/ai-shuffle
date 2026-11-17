/** A resize can clamp the live offset without the reader moving. */
export function isScrollClamped(
	{ scrollTop, scrollHeight, clientHeight }: Pick<HTMLElement, 'scrollTop' | 'scrollHeight' | 'clientHeight'>,
	saved: number
): boolean {
	return scrollTop >= scrollHeight - clientHeight - 1 && saved > scrollTop + 1;
}

/** Split keeps the same scroll owners while the width measurement catches up. */
export function canRecordSplitScroll(width: number, wasSplit: boolean, placing: boolean): boolean {
	return width > 0 && wasSplit && !placing;
}
