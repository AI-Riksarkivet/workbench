import { describe, expect, it } from 'vitest';
import { datasetLabel } from './dataset-label.ts';

describe('a dataset as a person reads it', () => {
	it.each([
		['lake~acme$corpus', 'lake · acme$corpus'],
		['scans~', 'scans'],
		['acme$letters', 'acme$letters'],
	])('%s reads %s', (id, label) => {
		expect(datasetLabel(id)).toBe(label);
	});
});
