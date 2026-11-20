import { describe, expect, it } from 'vitest';
import {
	classColours,
	classesFor,
	classForKey,
	matchClasses,
	regionClasses,
	submitBlockers,
	submittable,
	unsettledProposals,
	type ClassDef,
} from './classes.ts';

const c = (name: string, tools: string[] = [], over: Partial<ClassDef> = {}): ClassDef => ({
	name,
	colour: null,
	tools,
	required: false,
	...over,
});

const ONTOLOGY = [
	c('seal', ['bbox'], { required: true }),
	c('line', ['polygon']),
	c('damaged', ['tag']),
	c('any'),
];

describe('the classes a region may take', () => {
	it('are those whose tools name its shape or name none, in the ontology’s order', () => {
		expect(classesFor(ONTOLOGY, 'bbox').map((x) => x.name)).toEqual(['seal', 'any']);
	});

	it('leave a whole-item tag class out of the picker', () => {
		expect(regionClasses(ONTOLOGY).map((x) => x.name)).toEqual(['seal', 'line', 'any']);
	});

	it('take their own colour, else the palette’s at their place', () => {
		const colours = classColours([c('a', [], { colour: '#123456' }), c('b')]);
		expect([colours.get('a'), colours.get('b')]).toEqual(['#123456', '#3182bd']);
	});
});

describe('the picker’s keys', () => {
	it('pick the first nine classes by digit and nothing past them', () => {
		const nine = Array.from({ length: 10 }, (_, i) => c(`k${i + 1}`));
		expect([
			classForKey(nine, '1')?.name,
			classForKey(nine, '9')?.name,
			classForKey(nine, '0'),
		]).toEqual(['k1', 'k9', null]);
		expect(classForKey(ONTOLOGY.slice(0, 2), '3')).toBeNull();
	});

	it('narrow by a typed prefix first, then by what a name contains', () => {
		const names = matchClasses([c('stamp'), c('seal'), c('postscript'), c('signature')], 'st');
		expect(names.map((x) => x.name)).toEqual(['stamp', 'postscript']);
	});
});

describe('what blocks a submit', () => {
	it('names each region and text span without a class and each required class missing', () => {
		const blockers = submitBlockers(
			ONTOLOGY,
			[
				{ index: 0, shape: 'bbox', label: '' },
				{ index: 1, shape: 'tag', label: '' },
				{ index: 2, shape: 'polygon', label: 'line' },
				// The item's text is its content, not a mark; a span in it is a mark.
				{ index: 3, shape: 'text', label: '', parentId: '' },
				{ index: 4, shape: 'text', label: '', parentId: 'doc' },
			],
			false,
		);
		expect(blockers).toEqual({
			unlabelled: [0, 4],
			missing: ['seal'],
			empty: false,
			proposals: 0,
		});
		expect(submittable(blockers)).toBe(false);
	});

	it('is an empty item, unless the ontology allows one', () => {
		expect(submitBlockers([c('seal')], [], false).empty).toBe(true);
		expect(submittable(submitBlockers([c('seal')], [], true))).toBe(true);
	});

	it('is nothing on a canvas whose ontology names no class', () => {
		expect(submitBlockers([], [{ index: 0, shape: 'bbox', label: '' }], false)).toEqual({
			unlabelled: [],
			missing: [],
			empty: false,
			proposals: 0,
		});
	});

	it('counts each proposal nobody kept or removed, whether the ontology names classes or none', () => {
		const rows = [
			{ index: 0, shape: 'bbox', label: 'seal', status: 'prediction' },
			{ index: 1, shape: 'bbox', label: 'seal', status: 'accepted' },
			{ index: 2, shape: 'polygon', label: 'line', status: 'prediction' },
		];
		expect(submitBlockers(ONTOLOGY, rows, false).proposals).toBe(2);
		const none = submitBlockers([], rows, false);
		expect(none.proposals).toBe(2);
		expect(submittable(none)).toBe(false);
	});

	it('judges the contract only on what would be published: a rejected row is left out, a proposal counted only as one', () => {
		const rows = [
			// The only `seal` is a rejected proposal, and an unclassed rejected box: neither is a label.
			{ index: 0, shape: 'bbox', label: 'seal', status: 'rejected' },
			{ index: 1, shape: 'bbox', label: '', status: 'rejected' },
			// An unclassed proposal blocks as a proposal, not as a region without a class.
			{ index: 2, shape: 'bbox', label: '', status: 'prediction' },
		];
		expect(submitBlockers(ONTOLOGY, rows, false)).toEqual({
			unlabelled: [],
			missing: ['seal'],
			empty: true,
			proposals: 1,
		});
		expect(submitBlockers(ONTOLOGY, rows, true).empty).toBe(false);
	});

	it.each([
		[
			1,
			'submit',
			'1 proposal on this item is neither kept nor removed: keep or remove it before submitting',
		],
		[
			3,
			'accept',
			'3 proposals on this item are neither kept nor removed: keep or remove each before accepting',
		],
		[
			1,
			'fix_and_accept',
			'1 proposal on this item is neither kept nor removed: keep or remove it before accepting',
		],
	] as const)('is said for %i proposal(s) at %s in the annotator’s words', (count, event, said) => {
		expect(unsettledProposals(count, event)).toBe(said);
	});
});
