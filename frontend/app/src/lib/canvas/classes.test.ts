import { describe, expect, it } from 'vitest';
import {
	classColours,
	classesFor,
	classForKey,
	matchClasses,
	regionClasses,
	submitBlockers,
	submittable,
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
		expect(blockers).toEqual({ unlabelled: [0, 4], missing: ['seal'], empty: false });
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
		});
	});
});
