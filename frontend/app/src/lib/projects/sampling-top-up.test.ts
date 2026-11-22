import { describe, expect, it, vi } from 'vitest';

import type { ApiResult } from '#lib/api/client.ts';
import { SamplingTopUp, type SamplingDoors } from './sampling-top-up.svelte.ts';
import type { SamplingReads, SamplingState, SamplingStrategy, TaskDetail } from './types.ts';

const item = (id: string): TaskDetail => ({ task_id: id }) as TaskDetail;
const items = (n: number, prefix = 't'): TaskDetail[] =>
	Array.from({ length: n }, (_, i) => item(`${prefix}${i}`));
const ok = <T>(data: T): ApiResult<T> => ({ ok: true, data });
const reads = (over: Partial<SamplingReads>): ApiResult<SamplingReads> =>
	ok({ scored: [], measured: [], ask: [], notes: [], ...over });
const state = (over: Partial<SamplingState>): SamplingState => ({
	strategy: 'least_sure',
	model: 'm',
	available: true,
	reason: '',
	asks: true,
	scored: 0,
	unscored: 0,
	measured: 0,
	unmeasured: 0,
	...over,
});
const scoredAs = (id: string) => ({
	task_id: id,
	kind: 'least_sure' as const,
	score: 0.5,
	model: 'm',
	version: '1',
	asked_by: 'u',
	scored_at: '2026-11-22T10:00:00Z',
});
const measuredAs = (id: string) => ({
	task_id: id,
	novelty: 0.5,
	against: 1,
	vectors: 'v',
	model: null,
	asked_by: 'u',
	measured_at: '2026-11-22T10:00:00Z',
});

function doors(strategy: SamplingStrategy, over: Partial<SamplingDoors>) {
	const fakes = {
		strategy: () => strategy,
		lacking: vi.fn(async (): Promise<ApiResult<TaskDetail[]>> => ok([])),
		reads: vi.fn(async (): Promise<ApiResult<SamplingReads>> => reads({})),
		ask: vi.fn(async (): Promise<ApiResult<unknown>> => ok({})),
		reread: vi.fn(async () => undefined),
		...over,
	};
	return fakes;
}

describe('SamplingTopUp', () => {
	it('asks the model about at most the 50 items one lane lists, from where the visit stands', async () => {
		const fifty = items(50);
		const d = doors('least_sure', {
			lacking: vi.fn(async () => ok(fifty)),
			reads: vi.fn(async () => reads({ ask: fifty.map((t) => t.task_id) })),
		});

		await new SamplingTopUp(d).round();

		expect(d.lacking).toHaveBeenCalledWith('scored', 0, expect.any(AbortSignal));
		expect(d.ask).toHaveBeenCalledTimes(50);
		expect(d.reread).toHaveBeenCalledOnce();
	});

	it('passes by, in the next round, each item the last could not score', async () => {
		const lacking = vi.fn(async (_lane: string, _offset: number) => ok(items(3)));
		const d = doors('least_sure', {
			lacking,
			reads: vi.fn(async () => reads({ scored: [scoredAs('t0')], ask: ['t1', 't2'] })),
			ask: vi.fn(async (t: TaskDetail) =>
				t.task_id === 't2'
					? ({ ok: false, status: 422, detail: 'not a picture' } as const)
					: ok({}),
			),
		});
		const topUp = new SamplingTopUp(d);

		await topUp.round();
		await topUp.round();

		expect(lacking.mock.calls.map(([lane, offset]) => [lane, offset])).toEqual([
			['scored', 0],
			['scored', 1],
		]);
		expect(topUp.canContinue(state({ unscored: 1 }))).toBe(false);
	});

	it('stops at a model that cannot answer, says so, and starts no further round', async () => {
		const ask = vi.fn(async (): Promise<ApiResult<unknown>> => ({
			ok: false,
			status: 502,
			detail: 'the endpoint failed',
		}));
		const d = doors('least_sure', {
			lacking: vi.fn(async () => ok(items(8))),
			reads: vi.fn(async () => reads({ ask: items(8).map((t) => t.task_id) })),
			ask,
		});
		const topUp = new SamplingTopUp(d);

		await topUp.round();
		await topUp.round();

		expect(topUp.failure).toBe('the endpoint failed');
		expect(ask.mock.calls.length).toBeLessThan(8);
		expect(d.lacking).toHaveBeenCalledOnce();
		expect(topUp.canContinue(state({ unscored: 8 }))).toBe(false);
	});

	it('gives up the asks in flight when stopped, and reads nothing again', async () => {
		let inFlight: AbortSignal | null = null;
		const d = doors('least_sure', {
			lacking: vi.fn(async () => ok(items(2))),
			reads: vi.fn(async () => reads({ ask: ['t0', 't1'] })),
			ask: vi.fn(
				(_t: TaskDetail, signal: AbortSignal) =>
					new Promise<ApiResult<unknown>>((resolve) => {
						inFlight = signal;
						signal.addEventListener('abort', () =>
							resolve({ ok: false, status: 0, detail: 'given up' }),
						);
					}),
			),
		});
		const topUp = new SamplingTopUp(d);

		const round = topUp.round();
		await vi.waitFor(() => expect(inFlight).not.toBeNull());
		topUp.stop();
		await round;

		expect(inFlight!.aborted).toBe(true);
		expect(d.reread).not.toHaveBeenCalled();
		expect(topUp.running).toBe(false);
	});

	it('offers no more diversity once a round measured nothing', async () => {
		const d = doors('least_alike', {
			lacking: vi.fn(async () => ok(items(2))),
			reads: vi.fn(async () => reads({ notes: ['no item is labelled yet'] })),
		});
		const measuring = doors('least_alike', {
			lacking: vi.fn(async () => ok(items(2))),
			reads: vi.fn(async () => reads({ measured: [measuredAs('t0')] })),
		});
		const [none, some] = [new SamplingTopUp(d), new SamplingTopUp(measuring)];

		await none.round();
		await some.round();

		const left = state({ strategy: 'least_alike', unmeasured: 3 });
		expect([none.canContinue(left), some.canContinue(left)]).toEqual([false, true]);
		expect(none.notes).toEqual(['no item is labelled yet']);
	});
});
