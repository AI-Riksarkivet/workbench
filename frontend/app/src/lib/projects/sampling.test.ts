import { describe, expect, it, vi } from 'vitest';

import type { ApiResult } from '#lib/api/client.ts';
import { chosenLine, sampleEach, samplingChip, samplingLine } from './sampling.ts';
import type { ItemSampling, SamplingState, TaskDetail } from './types.ts';

const score = (kind: 'least_sure' | 'proposed_nothing', doubt: number | null) => ({
	task_id: 't',
	kind,
	score: doubt,
	model: 'm',
	version: '1',
	asked_by: 'user:a',
	scored_at: '2026-11-22T10:00:00Z',
});
const novelty = (n: number) => ({
	task_id: 't',
	novelty: n,
	against: 3,
	vectors: 'emb',
	model: null,
	asked_by: 'user:a',
	measured_at: '2026-11-22T10:00:00Z',
});
const item = (over: Partial<ItemSampling>): ItemSampling => ({
	score: null,
	novelty: null,
	placed_by: null,
	...over,
});

describe('an item’s sampling reason', () => {
	it.each([
		[
			'what placed it by doubt',
			item({ score: score('least_sure', 0.623), placed_by: 'least_sure' }),
			'mix',
			'Least sure · doubt 0.62',
		],
		[
			'what placed it by novelty',
			item({ novelty: novelty(0.41), placed_by: 'least_alike' }),
			'mix',
			'Least alike · 0.41',
		],
		[
			'a model that proposed nothing',
			item({ score: score('proposed_nothing', null) }),
			'least_sure',
			'Model proposed nothing',
		],
		[
			'in item order, the number the strategy reads',
			item({ score: score('least_sure', 0.2), novelty: novelty(0.9) }),
			'least_alike',
			'Least alike · 0.90',
		],
	] as const)('says %s', (_name, sampling, strategy, text) => {
		expect(samplingChip(sampling, strategy)).toEqual({ text, quiet: false });
	});

	it('says quietly that an item has no number yet, and nothing while sampling is off', () => {
		expect([samplingChip(null, 'least_sure'), samplingChip(item({}), 'off')]).toEqual([
			{ text: 'Not scored yet', quiet: true },
			null,
		]);
	});
});

describe('why an item was sent', () => {
	it('names sampling’s strategy and number, a person, or nothing', () => {
		expect([
			chosenLine({ by: 'sampling', kind: 'least_sure', score: 0.62, model: 'm' }),
			chosenLine({ by: 'sampling', kind: 'least_alike', score: null, model: null }),
			chosenLine({ by: 'person', kind: null, score: null, model: null }),
			chosenLine(null),
		]).toEqual([
			'Chosen by sampling: least sure (0.62)',
			'Chosen by sampling: least alike',
			'Picked by a person',
			null,
		]);
	});
});

describe('the line over a sampled list', () => {
	const state = (over: Partial<SamplingState>): SamplingState => ({
		strategy: 'least_sure',
		model: 'stand-in-scorer',
		available: true,
		reason: '',
		asks: true,
		scored: 12,
		unscored: 38,
		measured: 2,
		unmeasured: 48,
		...over,
	});

	it('counts what the strategy orders by, says why it cannot order, and is absent while off', () => {
		expect([
			samplingLine(state({})),
			samplingLine(state({ strategy: 'mix' })),
			samplingLine(state({ available: false, reason: 'this project names no model' })),
			samplingLine(state({ strategy: 'off' })),
		]).toEqual([
			{ warn: false, text: 'Least sure by stand-in-scorer: 12 scored, 38 not yet.' },
			{
				warn: false,
				text: 'Mix by stand-in-scorer: 12 scored, 38 not yet; 2 measured, 48 not yet.',
			},
			{ warn: true, text: 'Sampling cannot order this list: this project names no model' },
			null,
		]);
	});
});

describe('sampleEach', () => {
	const tasks = Array.from({ length: 8 }, (_, i) => ({ task_id: `t${i}` }) as TaskDetail);
	const refused = (status: number, type?: string): ApiResult<unknown> => ({
		ok: false,
		status,
		detail: `refused ${status}`,
		...(type ? { type } : {}),
	});

	it('passes over an item refused for itself, and stops at the first answer the model cannot give', async () => {
		const forItem = vi.fn(async (t: TaskDetail) =>
			t.task_id === 't0' ? refused(404) : ({ ok: true, data: {} } as const),
		);
		const failing = vi.fn(async () => refused(502));

		expect(await sampleEach(tasks, forItem, new AbortController().signal)).toEqual({
			scored: new Set(tasks.slice(1).map((t) => t.task_id)),
			failure: null,
		});
		expect(await sampleEach(tasks, failing, new AbortController().signal)).toEqual({
			scored: new Set(),
			failure: 'refused 502',
		});
		expect(failing.mock.calls.length).toBeLessThan(tasks.length);
	});
});
