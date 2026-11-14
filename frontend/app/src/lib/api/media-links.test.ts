import { describe, expect, it } from 'vitest';
import {
	lifetimeOf,
	MAX_BATCH,
	MediaLinks,
	MIN_LIFETIME_S,
	RENEW_BEFORE_S,
	RETRY_AFTER_S,
	type MediaItem,
	type MediaLink,
} from './media-links.svelte.ts';

// The batched signed-media loader against a stand-in for the viewer's batch door: what it is asked,
// in how many batches, and when a link it holds is signed again. `clock.now` is the browser's
// monotonic clock; every link the stand-in signs serves `ttl` seconds from its answer.

/** A viewer that signs every item for `ttl` seconds, numbering each signature, recording each batch. */
function viewer(ttl = 300) {
	const batches: { dataset: string; items: MediaItem[] }[] = [];
	let signature = 0;
	const sign = async (dataset: string, items: MediaItem[]): Promise<MediaLink[]> => {
		batches.push({ dataset, items });
		signature += 1;
		return items.map((item) =>
			item.doc_id === 'missing'
				? { url: null, detail: 'no payload for doc_id' }
				: { url: `/signed/${item.role}/${item.doc_id}/${signature}`, lifetime: ttl },
		);
	};
	return { batches, sign };
}

const thumb = (doc_id: string): MediaItem => ({ role: 'thumbnail', doc_id });

describe('the signed-media loader', () => {
	it('sends what one turn asks for as one batch, and answers each ask once it lands', async () => {
		const clock = { now: 1000 };
		const door = viewer();
		const links = new MediaLinks(door.sign, () => clock.now);

		expect(links.link('acme$letters', thumb('a'))).toBeUndefined();
		expect(links.link('acme$letters', thumb('b'))).toBeUndefined();
		expect(links.link('acme$letters', thumb('a'))).toBeUndefined();
		await links.settled();

		expect(door.batches).toEqual([{ dataset: 'acme$letters', items: [thumb('a'), thumb('b')] }]);
		expect(links.link('acme$letters', thumb('a'))?.url).toBe('/signed/thumbnail/a/1');
		expect(door.batches).toHaveLength(1);
	});

	it('splits a turn larger than the viewer takes into batches of at most 200, per dataset', async () => {
		const clock = { now: 1000 };
		const door = viewer();
		const links = new MediaLinks(door.sign, () => clock.now);

		for (let i = 0; i < 450; i++) links.link('acme$letters', thumb(`d${i}`));
		links.link('acme$notes', thumb('n'));
		await links.settled();

		expect(door.batches.map((b) => [b.dataset, b.items.length])).toEqual([
			['acme$letters', MAX_BATCH],
			['acme$letters', MAX_BATCH],
			['acme$letters', 50],
			['acme$notes', 1],
		]);
	});

	it('reuses a link until shortly before it expires, signs it again then, and never hands out an expired one', async () => {
		const clock = { now: 1000 };
		const door = viewer();
		const links = new MediaLinks(door.sign, () => clock.now);
		links.link('acme$letters', thumb('a'));
		await links.settled();

		clock.now = 1300 - RENEW_BEFORE_S - 1;
		expect(links.link('acme$letters', thumb('a'))?.url).toBe('/signed/thumbnail/a/1');
		await links.settled();
		expect(door.batches).toHaveLength(1);

		// Due for renewal and still serving: answered while the renewal is asked.
		clock.now = 1300 - RENEW_BEFORE_S;
		expect(links.link('acme$letters', thumb('a'))?.url).toBe('/signed/thumbnail/a/1');
		await links.settled();
		expect(links.link('acme$letters', thumb('a'))?.url).toBe('/signed/thumbnail/a/2');

		// Expired before anyone asked again: nothing is handed out until the new signature lands.
		clock.now += 300;
		expect(links.link('acme$letters', thumb('a'))).toBeUndefined();
		await links.settled();
		expect(links.link('acme$letters', thumb('a'))?.url).toBe('/signed/thumbnail/a/3');
	});

	it('keeps a link that still serves when its renewal fails, and asks again later', async () => {
		const clock = { now: 1000 };
		const door = viewer(300);
		let down = false;
		const links = new MediaLinks(
			async (dataset, items) => {
				if (down) throw new Error('the viewer is unreachable');
				return door.sign(dataset, items);
			},
			() => clock.now,
		);
		links.link('acme$letters', thumb('a'));
		await links.settled();

		down = true;
		clock.now = 1271;
		links.link('acme$letters', thumb('a'));
		await links.settled();
		expect(links.link('acme$letters', thumb('a'))?.url).toBe('/signed/thumbnail/a/1');
		expect(door.batches).toHaveLength(1);

		down = false;
		clock.now = 1271 + RETRY_AFTER_S;
		links.link('acme$letters', thumb('a'));
		await links.settled();
		expect(links.link('acme$letters', thumb('a'))?.url).toBe('/signed/thumbnail/a/2');
	});

	it('names each role as the viewer’s door does: media, thumbnail, and a chunk’s frame by its keys', async () => {
		const clock = { now: 1000 };
		const door = viewer();
		const links = new MediaLinks(door.sign, () => clock.now);
		const frame: MediaItem = { role: 'frame', doc_id: 'a', keys: [2, 7], frame_idx: 0 };

		links.link('acme$letters', { role: 'media', doc_id: 'a' });
		links.link('acme$letters', thumb('a'));
		const framed = links.resolve('acme$letters', frame);
		await links.settled();

		expect(door.batches[0]?.items).toEqual([{ role: 'media', doc_id: 'a' }, thumb('a'), frame]);
		expect((await framed).url).toBe('/signed/frame/a/1');
		expect(links.link('acme$letters', { role: 'media', doc_id: 'a' })?.url).toBe(
			'/signed/media/a/1',
		);
		// A media element whose source stopped serving asks for a fresh signature.
		const fresh = links.resolve('acme$letters', { role: 'media', doc_id: 'a' }, true);
		await links.settled();
		expect((await fresh).url).toBe('/signed/media/a/2');
	});

	it('keeps an item the viewer cannot sign for the page, and asks again after a batch that failed', async () => {
		const clock = { now: 1000 };
		let down = true;
		const door = viewer();
		const links = new MediaLinks(
			async (dataset, items) => {
				if (down) throw new Error('the viewer is unreachable');
				return door.sign(dataset, items);
			},
			() => clock.now,
		);

		links.link('acme$letters', thumb('a'));
		await links.settled();
		expect(links.link('acme$letters', thumb('a'))).toEqual({
			url: null,
			detail: 'the viewer is unreachable',
		});

		down = false;
		clock.now += RETRY_AFTER_S;
		links.link('acme$letters', thumb('a'));
		links.link('acme$letters', thumb('missing'));
		await links.settled();
		expect(links.link('acme$letters', thumb('a'))?.url).toBe('/signed/thumbnail/a/1');

		clock.now += 10_000;
		expect(links.link('acme$letters', thumb('missing'))).toEqual({
			url: null,
			detail: 'no payload for doc_id',
		});
		await links.settled();
		expect(door.batches.flatMap((b) => b.items)).toEqual([thumb('a'), thumb('missing')]);
	});
});

describe('a link’s lifetime is read on the viewer’s own clock', () => {
	it('is expires_at less the answer’s Date, however far the browser’s clock is off', () => {
		// A viewer clock in 2031 while the browser believes it is 2026: still five minutes.
		const served = 'Sat, 01 Feb 2031 10:00:00 GMT';
		const expiresAt = Date.parse(served) / 1000 + 300;
		expect(lifetimeOf(expiresAt, served)).toBe(300);
	});

	it('is the shortest lifetime the viewer signs for when the answer names no Date', () => {
		expect(lifetimeOf(Date.now() / 1000 + 300, null)).toBe(MIN_LIFETIME_S);
		expect(lifetimeOf(null, null)).toBeNull();
	});
});
