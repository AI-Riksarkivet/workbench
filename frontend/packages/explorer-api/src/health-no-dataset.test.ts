import { beforeEach, describe, expect, it } from 'vitest';
import { getHealth } from './api';
import { resetApiBase } from './base';

/**
 * `/api/health` answers 200 with `db: null` when the media service is up but no corpus dataset resolves,
 * and this client must ACCEPT that.
 *
 * A probe that answered 404 when no dataset resolves could not tell "service down" from "nothing loaded" (live-proof
 * 2026-07-28, defect 4), so the backend answers 200 with `db: null` and `db_error`, and each encoder only as reachable or
 * not. A strict client would turn that 200 into a parse error, so the client half is pinned here.
 */
beforeEach(() => resetApiBase());

/** A fetch that answers one JSON body with 200, the way the degraded probe really does. */
function respondWith(body: unknown): typeof fetch {
	return (() =>
		Promise.resolve(
			new Response(JSON.stringify(body), {
				status: 200,
				headers: { 'content-type': 'application/json' },
			}),
		)) as unknown as typeof fetch;
}

const DEAD_PING = { ok: false };

describe('health with no dataset loaded', () => {
	it('parses a 200 whose db is null', async () => {
		const health = await getHealth(
			respondWith({
				db: null,
				db_error: 'no dataset could be read',
				embed: DEAD_PING,
				rerank: DEAD_PING,
			}),
		);
		expect(health.db).toBeNull();
		// The reason must survive parsing: it is what lets the UI say "no dataset" instead of guessing
		// "unreachable".
		expect(health.db_error).toBe('no dataset could be read');
	});

	it('parses a 200 that omits db entirely', async () => {
		// `nullish`, not `nullable`: an older/leaner backend may omit the key rather than send null, and a
		// probe is the last place that should fail closed on a missing optional field.
		const health = await getHealth(respondWith({ embed: DEAD_PING, rerank: DEAD_PING }));
		expect(health.db ?? null).toBeNull();
	});

	it('still reports encoder capability when there is no dataset', async () => {
		// The zone gates its Meaning/Hybrid search modes on `embed.ok`. While health 404'd, the store's
		// reading stayed null and the modes fell back to "assume available" — offering a mode that 503s.
		const health = await getHealth(
			respondWith({ db: null, db_error: 'x', embed: DEAD_PING, rerank: DEAD_PING }),
		);
		expect(health.embed.ok).toBe(false);
		expect(health.rerank.ok).toBe(false);
	});

	it('still parses a fully healthy response', async () => {
		const health = await getHealth(
			respondWith({
				db: {
					path: '/media-corpus/transcripts_v2.lance',
					tables: ['chunks'],
					chunks: 3,
					documents: 1,
				},
				embed: { ok: true },
				rerank: { ok: true },
			}),
		);
		expect(health.db?.path).toBe('/media-corpus/transcripts_v2.lance');
		expect(health.db?.chunks).toBe(3);
	});
});
