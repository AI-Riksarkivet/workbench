import { describe, expect, it } from 'vitest';
import { linkMayHaveExpired, resumeAfterSwap, type MediaElementLike } from './media-source.ts';

// The swap a long item needs when its signed link stops serving mid-play, on a stand-in element.

class Element extends EventTarget implements MediaElementLike {
	currentTime = 0;
	paused = true;
	plays = 0;
	async play(): Promise<void> {
		this.plays += 1;
		this.paused = false;
	}
}

describe('swapping a playing element’s source for a fresh link', () => {
	it('resumes at the same position, playing again only if it was playing', () => {
		const playing = new Element();
		playing.currentTime = 412.5;
		playing.paused = false;
		resumeAfterSwap(playing);
		// The new source loads from its start.
		playing.currentTime = 0;
		playing.dispatchEvent(new Event('loadedmetadata'));
		expect(playing.currentTime).toBe(412.5);
		expect(playing.plays).toBe(1);

		const paused = new Element();
		paused.currentTime = 9;
		resumeAfterSwap(paused);
		paused.dispatchEvent(new Event('loadedmetadata'));
		paused.dispatchEvent(new Event('loadedmetadata'));
		expect(paused.currentTime).toBe(9);
		expect(paused.plays).toBe(0);
	});

	it('renews only on the errors a refused Range request raises', () => {
		expect([1, 2, 3, 4, null, undefined].map(linkMayHaveExpired)).toEqual([
			false,
			true,
			false,
			true,
			false,
			false,
		]);
	});
});
