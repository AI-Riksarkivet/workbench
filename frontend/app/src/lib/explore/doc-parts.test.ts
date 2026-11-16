import { describe, expect, it } from 'vitest';
import { partIndexOf } from './doc-parts.ts';

const view = { keyFields: ['doc_id', 'speech_id', 'chunk_id'], docKeyField: 'doc_id' };
const parts = [
	{ speech_id: 0, chunk_id: 0 },
	{ speech_id: 0, chunk_id: 1 },
	{ speech_id: 1, chunk_id: 0 },
];

describe('partIndexOf', () => {
	it('finds the part by the keys after the document key, whatever their type on the wire', () => {
		expect(partIndexOf(view, parts, { doc_id: 'd', speech_id: 1, chunk_id: 0 })).toBe(2);
		expect(partIndexOf(view, parts, { doc_id: 'd', speech_id: '0', chunk_id: '1' })).toBe(1);
	});
	it('is -1 for a row its document does not list', () => {
		expect(partIndexOf(view, parts, { doc_id: 'd', speech_id: 7, chunk_id: 0 })).toBe(-1);
	});
	it('is -1 when the document key is the only key: a document has no parts to tell apart', () => {
		expect(
			partIndexOf({ keyFields: ['doc_id'], docKeyField: 'doc_id' }, parts, { doc_id: 'd' }),
		).toBe(-1);
	});
});
