import { describe, expect, it } from 'vitest';
import { personName } from './people.ts';

describe('personName', () => {
	it.each([
		['dex~ada', { 'dex~ada': 'Ada Lovelace' }, 'Ada Lovelace'],
		['dex~ada', {}, 'ada'],
		['dex~08a8f3c2d1e04b5a9f', {}, '08a8f3c2…'],
		[null, {}, 'someone'],
	])('%s with %o reads %s', (principal, people, expected) => {
		expect(personName(principal, people)).toBe(expected);
	});
});
