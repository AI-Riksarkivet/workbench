/**
 * Signed media links, batched: every `<img>`, `<video>` and `<audio>` a page renders asks here for its
 * link, and the asks made in one turn of the event loop go to the viewer as ONE `POST
 * /api/signed-media` (at most {@link MAX_BATCH} items each, the viewer's own cap), the batching a
 * DataLoader does. Media elements send no token, so the link is the permission: the viewer resolves
 * each cell by the person's own read and signs a same-origin link valid for minutes (owner decision on
 * #9).
 *
 * LIFETIMES ARE RELATIVE. The viewer names when a link stops serving on ITS clock; the batch door turns
 * that into a lifetime against the answer's own `Date` (`lifetimeOf`, the age arithmetic of RFC 9111
 * § 4.2.3), and this loader counts it down on the browser's monotonic clock from when the answer
 * arrived. The browser's wall clock is never compared with the server's, so a skewed laptop neither
 * reuses a dead link nor re-signs a live one.
 *
 * A link is reused until shortly before it expires ({@link RENEW_BEFORE_S}), then signed again on the
 * next ask; past its expiry it is never handed out. A renewal that fails keeps the link it would have
 * replaced while that link still serves, and is asked again after {@link RETRY_AFTER_S}. What the
 * viewer answers for an item it cannot sign ("no payload for doc_id") holds for the page's life: a cell
 * that has no thumbnail does not grow one.
 *
 * Reading a link is reactive and has no side effect a render can see: an unknown link answers
 * `undefined` and is queued in plain (non-reactive) state, and the batch's answer lands in a
 * `SvelteMap`, which re-renders whoever asked.
 */
import { SvelteMap } from 'svelte/reactivity';

/** The viewer's own cap on one batch (`signed_media.MAX_BATCH`). */
export const MAX_BATCH = 200;
/** A link this close to its expiry is signed again, so one handed out is good for at least this long. */
export const RENEW_BEFORE_S = 30;
/** A batch that failed whole (the viewer unreachable, or refusing) is asked again after this long. */
export const RETRY_AFTER_S = 10;
/** The shortest lifetime the viewer signs for (`VIEWER_MEDIA_URL_TTL_SECONDS` is at least 10). */
export const MIN_LIFETIME_S = 10;

/** One cell to sign, as the viewer's batch door names it: a document's media or thumbnail, a chunk's
 *  frame (the key fields after the doc key, in the descriptor's order), or any table's binary cell by
 *  its qualified table, column, Lance row id and the version its row was read at (a plain table's row). */
export type MediaItem =
	| { role: 'media' | 'thumbnail'; doc_id: string }
	| { role: 'frame'; doc_id: string; keys: number[]; frame_idx: number }
	| { role: 'cell'; table: string; column: string; row: number; version: number };

/** One item's answer: a same-origin link and how many seconds it serves from the answer's arrival, or
 *  why there is none. */
export type MediaLink = { url: string; lifetime: number | null } | { url: null; detail: string };

/** Sign one batch of one dataset's items; one answer per item, in order. Throws when the batch failed. */
export type SignBatch = (dataset: string, items: MediaItem[]) => Promise<MediaLink[]>;

/**
 * How long a link serves, from the viewer's absolute `expires_at` and the `Date` its answer carries:
 * both are the viewer's clock, so their difference is the lifetime whatever the browser's clock says.
 * With no usable `Date`, the viewer's shortest lifetime is assumed, which renews early rather than
 * serving a dead link.
 */
export function lifetimeOf(expiresAt: number | null, served: string | null): number | null {
	if (expiresAt === null) return null;
	const at = served === null ? Number.NaN : Date.parse(served) / 1000;
	if (!Number.isFinite(at)) return MIN_LIFETIME_S;
	return Math.max(0, expiresAt - at);
}

interface Held {
	link: MediaLink;
	/** When the link stops serving (monotonic seconds); null for an answer that holds. */
	expiresAt: number | null;
	/** When to sign again (monotonic seconds); null holds it for the page's life. */
	renewAt: number | null;
}

interface Waiting {
	dataset: string;
	item: MediaItem;
	settle: Array<(link: MediaLink) => void>;
}

const keyOf = (dataset: string, item: MediaItem): string => JSON.stringify([dataset, item]);

/** The browser's monotonic clock, in seconds. */
const monotonic = (): number => performance.now() / 1000;

export class MediaLinks {
	readonly #held = new SvelteMap<string, Held>();
	/** Asked for, not yet sent. Plain state: queuing must not be a change a render observes. */
	readonly #queue = new Map<string, Waiting>();
	/** Sent, not yet answered: asked again meanwhile, an item joins its batch instead of a new one. */
	readonly #inFlight = new Map<string, Waiting>();
	readonly #batches = new Set<Promise<void>>();
	readonly #sign: SignBatch;
	readonly #now: () => number;
	#scheduled = false;

	constructor(sign: SignBatch, now: () => number = monotonic) {
		this.#sign = sign;
		this.#now = now;
	}

	/** The link for `item` while it serves; asks the viewer when none is held, when the held one is due
	 *  for renewal, and when it expired. `undefined` until the viewer answers, and once a link expired. */
	link(dataset: string, item: MediaItem): MediaLink | undefined {
		const key = keyOf(dataset, item);
		const held = this.#held.get(key);
		const now = this.#now();
		if (!held || (held.renewAt !== null && now >= held.renewAt)) this.#ask(key, dataset, item);
		if (held && held.expiresAt !== null && now >= held.expiresAt) return undefined;
		return held?.link;
	}

	/** A link for `item` that serves now, for a caller that sets a `src` itself; `fresh` signs a new one
	 *  even while the held one serves (a media element whose source stopped serving). */
	resolve(dataset: string, item: MediaItem, fresh = false): Promise<MediaLink> {
		const key = keyOf(dataset, item);
		const held = this.#held.get(key);
		if (!fresh && held && (held.renewAt === null || this.#now() < held.renewAt)) {
			return Promise.resolve(held.link);
		}
		return new Promise((settle) => this.#ask(key, dataset, item, settle));
	}

	/** Every batch sent so far has been answered. */
	async settled(): Promise<void> {
		while (this.#scheduled || this.#batches.size > 0) {
			await Promise.resolve();
			await Promise.all(this.#batches);
		}
	}

	#ask(key: string, dataset: string, item: MediaItem, settle?: (link: MediaLink) => void): void {
		const waiting = this.#queue.get(key) ?? this.#inFlight.get(key);
		if (waiting) {
			if (settle) waiting.settle.push(settle);
			return;
		}
		this.#queue.set(key, { dataset, item, settle: settle ? [settle] : [] });
		if (this.#scheduled) return;
		this.#scheduled = true;
		queueMicrotask(() => this.#flush());
	}

	#flush(): void {
		this.#scheduled = false;
		const byDataset = new Map<string, [string, Waiting][]>();
		for (const entry of this.#queue) {
			const [, waiting] = entry;
			byDataset.set(waiting.dataset, [...(byDataset.get(waiting.dataset) ?? []), entry]);
			this.#inFlight.set(entry[0], waiting);
		}
		this.#queue.clear();
		for (const [dataset, entries] of byDataset) {
			for (let start = 0; start < entries.length; start += MAX_BATCH) {
				const batch = entries.slice(start, start + MAX_BATCH);
				const sent = this.#send(dataset, batch).finally(() => this.#batches.delete(sent));
				this.#batches.add(sent);
			}
		}
	}

	async #send(dataset: string, batch: [string, Waiting][]): Promise<void> {
		let answers: MediaLink[];
		let failed: string | null = null;
		try {
			answers = await this.#sign(
				dataset,
				batch.map(([, waiting]) => waiting.item),
			);
		} catch (err) {
			answers = [];
			failed = err instanceof Error ? err.message : String(err);
		}
		const now = this.#now();
		batch.forEach(([key, waiting], index) => {
			const answer = answers[index];
			const held =
				failed !== null || answer === undefined
					? this.#failure(key, now, failed ?? 'the viewer named no link for this item')
					: this.#answer(answer, now);
			this.#inFlight.delete(key);
			this.#held.set(key, held);
			for (const settle of waiting.settle) settle(held.link);
		});
	}

	/** A renewal that failed keeps the link it would have replaced while that link serves, and is asked
	 *  again later; with none to keep, the failure is the answer until then. */
	#failure(key: string, now: number, detail: string): Held {
		const prior = this.#held.get(key);
		if (prior && prior.link.url !== null && (prior.expiresAt === null || now < prior.expiresAt)) {
			return { ...prior, renewAt: now + RETRY_AFTER_S };
		}
		return { link: { url: null, detail }, expiresAt: null, renewAt: now + RETRY_AFTER_S };
	}

	#answer(answer: MediaLink, now: number): Held {
		if (answer.url === null || answer.lifetime === null) {
			return { link: answer, expiresAt: null, renewAt: null };
		}
		const expiresAt = now + answer.lifetime;
		// A short-lived link (the viewer's TTL may be as low as 10 s) renews half-way through its life,
		// and never at once: the answer re-renders whoever asked, and a renewal due on that render would
		// ask again in a loop.
		const renewAt = Math.max(expiresAt - RENEW_BEFORE_S, now + answer.lifetime / 2, now + 1);
		return { link: answer, expiresAt, renewAt };
	}
}
