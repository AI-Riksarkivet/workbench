import { describe, expect, it } from 'vitest';
import { modelSource, proposalsFrom } from './proposal-rows.ts';

describe('proposalsFrom', () => {
	const rows = [
		{ id: 'a', status: 'prediction', source: 'model:tagger' },
		{ id: 'b', status: 'prediction', source: 'model:vlm' },
		{ id: 'c', status: 'accepted', source: 'model:tagger' },
		{ id: 'd', status: 'accepted', source: 'human' },
		{ id: 'e', status: 'prediction', source: 'model:tagger' },
	];

	it('names the unsettled proposals of that model alone', () => {
		expect(proposalsFrom(rows, modelSource('tagger'))).toEqual(['a', 'e']);
	});

	it('names none where the model proposed nothing still unsettled', () => {
		expect(proposalsFrom(rows, modelSource('detector'))).toEqual([]);
	});
});
