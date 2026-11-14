import { describe, expect, it } from 'vitest';
import { roleSummary } from './presentation.ts';

describe('the role a reader holds on a project, as the page says it', () => {
	it.each([
		[['can_administer', 'can_manage'], 'You own this project'],
		[['can_manage', 'can_review'], 'You manage this project'],
		[['can_review', 'can_annotate'], 'You review in this project'],
		[['can_annotate', 'can_claim'], 'You annotate in this project'],
		[[], 'You can view this project'],
	])('%j reads as the highest rung it holds', (permissions, said) => {
		expect(roleSummary(permissions)).toMatch(new RegExp(`^${said}`));
	});
});
