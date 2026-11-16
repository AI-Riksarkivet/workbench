import { describe, expect, it } from 'vitest';
import { cellText } from './cells.ts';

describe('a plain cell as text', () => {
	it.each([
		['vector', { dim: 16, head: [0.5, -1] }, '[0.500, -1.000, …] · 16d'],
		['vector', { dim: 2, head: [0.5, -1] }, '[0.500, -1.000] · 2d'],
		['blob', { size: 9293 }, '9.1 KiB'],
		['binary', { size: 12 }, '12 B'],
		['timestamp', '2020-10-02T12:26:40+00:00', '2020-10-02 12:26:40+00:00'],
		['json', '["coast", "trade"]', '["coast", "trade"]'],
		['integer', 42, '42'],
		['text', null, ''],
	] as const)('%s %j reads %j', (kind, value, text) => {
		expect(cellText(kind, value)).toBe(text);
	});
});
