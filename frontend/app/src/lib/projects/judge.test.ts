import { describe, expect, it, vi } from 'vitest';

import type { ApiResult } from '#lib/api/client.ts';
import { JudgeTopUp } from './judge-top-up.svelte.ts';
import { judgeEach, SubmitAsks } from './judge.ts';
import { IN_FLIGHT } from './label-run.ts';
import type { Judgement, TaskDetail } from './types.ts';

const item = (id: string, over: Partial<TaskDetail> = {}): TaskDetail =>
	({
		task_id: id,
		project_id: 'p',
		state: 'in_review',
		source: { kind: 'chunks', keys: ['d/0'], where: 'lake~a$b' },
		legal_events: [],
		judgement: null,
		...over,
	}) as TaskDetail;
const items = (n: number, prefix = 't'): TaskDetail[] =>
	Array.from({ length: n }, (_, i) => item(`${prefix}${i}`));

const judged: ApiResult<unknown> = { ok: true, data: {} };
const refused = (status: number, type?: string): ApiResult<unknown> => ({
	ok: false,
	status,
	detail: `refused ${status}`,
	...(type ? { type } : {}),
});
const live = (): AbortSignal => new AbortController().signal;

describe('judgeEach', () => {
	it('passes over an item refused for itself and judges the rest', async () => {
		const tasks = items(6);
		const judge = vi.fn(async (t: TaskDetail) =>
			t.task_id === 't0' ? refused(409, 'conflict') : judged,
		);

		expect(await judgeEach(tasks, judge, live())).toEqual({ judged: 5, failure: null });
		expect(judge).toHaveBeenCalledTimes(6);
	});

	it.each([
		['an endpoint that is unavailable', refused(503)],
		['a project that names no judge', refused(409, 'no-judge')],
	])('stops at %s, and says why', async (_name, answer) => {
		const tasks = items(10);
		const judge = vi.fn(async () => answer);

		expect(await judgeEach(tasks, judge, live())).toEqual({
			judged: 0,
			failure: answer.ok ? null : answer.detail,
		});
		// The asks already in flight answer; none is started after the failure.
		expect(judge).toHaveBeenCalledTimes(IN_FLIGHT);
	});
});

describe('the asks after a submit', () => {
	it('keeps at most a few in flight however many items are submitted, and reads the project once', async () => {
		let inFlight = 0;
		let peak = 0;
		const names = vi.fn(async () => true);
		const asks = new SubmitAsks(names, () => {
			inFlight += 1;
			peak = Math.max(peak, inFlight);
			return new Promise<never>(() => {});
		});

		for (const t of items(20)) asks.add(t);
		await vi.waitFor(() => expect(inFlight).toBe(IN_FLIGHT));
		await new Promise((r) => setTimeout(r, 20));

		expect(peak).toBe(IN_FLIGHT);
		expect(names).toHaveBeenCalledTimes(1);
	});

	it('asks nothing for an item that did not land in review, or of a project that names no judge, and survives a failed ask', async () => {
		const judge = vi.fn(async (t: TaskDetail) => {
			if (t.task_id === 'boom') throw new Error('no answer');
			return judged;
		});
		const asks = new SubmitAsks(async (projectId) => projectId === 'p', judge);

		asks.add(item('accepted', { state: 'accepted' }));
		asks.add(item('other', { project_id: 'q' }));
		asks.add(item('boom'));
		asks.add(item('fine'));
		await vi.waitFor(() => expect(judge).toHaveBeenCalledTimes(2));
		expect(judge.mock.calls.map(([t]) => t.task_id)).toEqual(['boom', 'fine']);
	});
});

describe('the top-up', () => {
	const verdict = { flags: [] } as unknown as Judgement;

	it('asks each unread item once, reads the list again when any was judged, and asks the next page’s unread', async () => {
		const judge = vi.fn(async (_task: TaskDetail) => judged);
		const reread = vi.fn(async () => {});
		const topUp = new JudgeTopUp(judge, reread);
		const first = [...items(3), item('seen', { judgement: verdict })];

		await topUp.offer(first);
		expect(judge.mock.calls.map(([t]) => t.task_id)).toEqual(['t0', 't1', 't2']);
		expect(reread).toHaveBeenCalledTimes(1);

		// The list read again holds more unread items: only those are asked.
		await topUp.offer([...first, ...items(2, 'n')]);
		expect(judge.mock.calls.map(([t]) => t.task_id)).toEqual(['t0', 't1', 't2', 'n0', 'n1']);
		expect(reread).toHaveBeenCalledTimes(2);

		// Nothing new: nothing asked, nothing read.
		await topUp.offer(first);
		expect(judge).toHaveBeenCalledTimes(5);
		expect(reread).toHaveBeenCalledTimes(2);
	});

	it('stops for good once the judge cannot answer', async () => {
		const judge = vi.fn(async () => refused(502));
		const topUp = new JudgeTopUp(judge, async () => {});

		await topUp.offer(items(2));
		expect(topUp.failure).toBe('refused 502');
		await topUp.offer(items(2, 'n'));
		expect(judge).toHaveBeenCalledTimes(2);
	});

	it('stopped while asking, gives the asks up and reads nothing again', async () => {
		let seen: AbortSignal | undefined;
		const reread = vi.fn(async () => {});
		const topUp = new JudgeTopUp(async (_t, signal) => {
			seen = signal;
			await new Promise((r) => setTimeout(r, 10));
			return judged;
		}, reread);

		const round = topUp.offer(items(1));
		topUp.stop();
		await round;

		expect(seen?.aborted).toBe(true);
		expect(reread).not.toHaveBeenCalled();
		await topUp.offer(items(1, 'n'));
		expect(topUp.judging).toBe(false);
	});
});
