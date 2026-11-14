/**
 * A playing `<video>` or `<audio>` and its signed link. The element keeps ONE source while it plays: it
 * fetches the media in Range requests for as long as it plays, and swapping the source on every renewal
 * would restart it. A link is valid for minutes, so a long item outlives it: the element's next Range
 * request is refused, and the element reports a network or source error. Then, and only then, the
 * player asks for a fresh link and swaps it in, resuming at the same position and play state.
 */

/** What of a media element the swap reads and restores. */
export interface MediaElementLike {
	currentTime: number;
	readonly paused: boolean;
	play(): Promise<void>;
	addEventListener(
		type: 'loadedmetadata',
		listener: () => void,
		options?: { once?: boolean },
	): void;
}

/** `MediaError.MEDIA_ERR_NETWORK` and `MEDIA_ERR_SRC_NOT_SUPPORTED`: how a refused Range request (an
 *  expired link answers 403) surfaces. A decode error or an abort is not the link's. */
const LINK_ERRORS = new Set([2, 4]);

/** Whether an element's error may be its link's having stopped serving, so a fresh link may fix it. */
export function linkMayHaveExpired(code: number | null | undefined): boolean {
	return code !== null && code !== undefined && LINK_ERRORS.has(code);
}

/** Before the source is swapped: once the new source's metadata loads, the element is put back where it
 *  was, and plays again if it was playing. */
export function resumeAfterSwap(el: MediaElementLike): void {
	const at = el.currentTime;
	const playing = !el.paused;
	el.addEventListener(
		'loadedmetadata',
		() => {
			el.currentTime = at;
			if (playing) void el.play().catch(() => {});
		},
		{ once: true },
	);
}
