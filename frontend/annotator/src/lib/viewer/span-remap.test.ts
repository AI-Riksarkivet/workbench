/**
 * Span offsets FOLLOW the text they annotate (§8d.1). Before this existed, correcting one typo
 * at the start of a line silently re-pointed every entity after it at the wrong characters —
 * annotations corrupted by the act of improving the data they annotate.
 */

import { tableFromArrays } from 'apache-arrow';
import { describe, expect, it, vi } from 'vitest';

vi.mock('svelte-sonner', () => ({
	toast: {
		success: () => {},
		error: () => {},
		message: () => {},
		warning: () => {},
		info: () => {},
	},
}));
vi.mock('./remote/assist.remote', () => ({
	requestAssist: async () => ({ ok: true, data: { shapes: [], source: 's', dropped: [] } }),
	assistProducers: async () => [],
}));
vi.mock('./remote/jobs.remote', () => ({
	submitBatchJob: async () => ({
		ok: true,
		data: { job_id: 'j', status: 'queued', backend: 'mock' },
	}),
	jobStatus: async () => null,
}));

import { AnnotatorController } from './annotator.svelte.js';

// 'Gustaf Eriksson Vasa reste till Dalarna' — span 'Vasa' at [16, 20), 'Dalarna' at [32, 39).
const TEXT = 'Gustaf Eriksson Vasa reste till Dalarna';

describe('the controller applies the remap on a transcription edit', () => {
	function harness() {
		const noop = () => {};
		return {
			plugins: {
				arrow: new Proxy(
					{ getDirtyOverrides: () => new Map(), isDeleted: () => false },
					{ get: (t: Record<string, unknown>, k: string) => t[k] ?? noop },
				),
				interaction: new Proxy(
					{ getSelectedSet: () => new Set<number>(), cvCapable: false },
					{ get: (t: Record<string, unknown>, k: string) => t[k] ?? noop, set: () => true },
				),
				image: { onViewportChange: undefined, zoomPercent: 100 },
			},
		};
	}

	const TABLE = tableFromArrays({
		id: ['line', 'vasa', 'dala'],
		shape_type: ['text', 'text', 'text'],
		label: ['', 'person', 'place'],
		text: [TEXT, '', ''],
		parent_id: ['', 'line', 'line'],
		char_start: Int32Array.from([-1, 16, 32]),
		char_end: Int32Array.from([-1, 20, 39]),
		status: ['accepted', 'accepted', 'accepted'],
		source: ['ingest', 'human', 'human'],
	});

	it('spans shift in the rendered rows AND ride the Save payload as span edits', () => {
		const c = new AnnotatorController();
		// eslint-disable-next-line @typescript-eslint/no-explicit-any -- structural double
		c.attach(harness() as any, TABLE, '/annotator/api/annotations/d/0/1');

		c.updateField(0, 'text', TEXT.replace('Eriksson', 'Erikssonn'));

		const byId = Object.fromEntries(c.rows.map((r) => [r.id, r]));
		expect([byId['vasa']!.charStart, byId['vasa']!.charEnd]).toEqual([17, 21]);
		expect([byId['dala']!.charStart, byId['dala']!.charEnd]).toEqual([33, 40]);
		expect(c.dirty).toBe(true);
	});

	it('a span edited away is DELETED, not left pointing at the wrong characters', () => {
		const c = new AnnotatorController();
		// eslint-disable-next-line @typescript-eslint/no-explicit-any -- structural double
		c.attach(harness() as any, TABLE, '/annotator/api/annotations/d/0/1');

		c.updateField(0, 'text', TEXT.replace('Vasa reste till ', ''));

		const ids = c.rows.map((r) => r.id);
		expect(ids).not.toContain('vasa');
		// The surviving span followed the shift.
		const dala = c.rows.find((r) => r.id === 'dala')!;
		expect([dala.charStart, dala.charEnd]).toEqual([16, 23]);
	});
});
