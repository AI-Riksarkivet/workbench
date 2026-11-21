import { describe, expect, it } from 'vitest';
import {
	carryCount,
	changeFor,
	digitOf,
	excerpt,
	fits,
	gridClasses,
	gridDoorParams,
	discards,
	labelable,
	moved,
	readGridQuery,
	writeGridQuery,
	DEFAULT_GRID,
	type Tag,
} from './label-grid.ts';
import type { TaskDetail } from './types.ts';

describe('which classes a grid labels', () => {
	it('labels the tag classes with their digits, and names every other class with what it is drawn with', () => {
		const split = gridClasses(
			[
				{ name: 'letter', tools: ['tag'] },
				{ name: 'stamp', tools: ['bbox', 'polygon'] },
				{ name: 'any', tools: [] },
				{ name: 'dated', tools: ['tag'], attributes: [{ required: true }] },
				{ name: 'map', tools: ['tag', 'bbox'], colour: '#123456' },
			],
			new Map([['map', '#123456']]),
		);

		expect(split.labels.map((c) => [c.name, c.key, c.colour])).toEqual([
			['letter', 1, null],
			['map', 2, '#123456'],
		]);
		expect(split.canvas).toEqual([
			{ name: 'stamp', why: 'bbox, polygon' },
			{ name: 'any', why: 'any shape' },
			{ name: 'dated', why: 'a required attribute' },
		]);
	});
});

describe('what a label does to an item’s tags', () => {
	const grid = new Set(['letter', 'map']);
	const tag = (id: string, label: string, over: Partial<Tag> = {}): Tag => ({
		id,
		label,
		detailed: false,
		prediction: false,
		...over,
	});
	const letter = tag('a', 'letter');
	const note = tag('b', 'note');
	const tags = [letter, note];
	const mint = () => 'new';
	const none = { inserts: [], deletes: [], accepts: [] };

	it.each([
		[
			'set another grid label replaces the grid label, keeping every other tag',
			tags,
			{ kind: 'set', label: 'map' },
			{ ...none, inserts: [{ id: 'new', label: 'map' }], deletes: [letter] },
		],
		['set the label held changes nothing', tags, { kind: 'set', label: 'letter' }, none],
		[
			'set the label a model proposed confirms the proposal rather than adding a second',
			[tag('p', 'letter', { prediction: true })],
			{ kind: 'set', label: 'letter' },
			{ ...none, accepts: [tag('p', 'letter', { prediction: true })] },
		],
		[
			'toggle a label held takes it away',
			tags,
			{ kind: 'toggle', label: 'letter' },
			{ ...none, deletes: [letter] },
		],
		[
			'toggle a label held only as a proposal confirms it',
			[tag('p', 'map', { prediction: true })],
			{ kind: 'toggle', label: 'map' },
			{ ...none, accepts: [tag('p', 'map', { prediction: true })] },
		],
		[
			'toggle a label not held adds it beside the others',
			tags,
			{ kind: 'toggle', label: 'map' },
			{ ...none, inserts: [{ id: 'new', label: 'map' }] },
		],
		['clear takes the grid labels only', tags, { kind: 'clear' }, { ...none, deletes: [letter] }],
	] as const)('%s', (_name, held, intent, expected) => {
		expect(changeFor(held, intent, grid, mint)).toEqual(expected);
	});

	it.each([
		['a plain label it replaces', [letter], false],
		['a label carrying text, attributes or a link', [tag('a', 'letter', { detailed: true })], true],
		['a model’s proposal of another class', [tag('a', 'letter', { prediction: true })], true],
	] as const)('whether it asks before discarding %s', (_name, held, asked) => {
		expect(discards(changeFor(held, { kind: 'set', label: 'map' }, grid, mint))).toBe(asked);
	});
});

describe('what a reader may do to an item’s label', () => {
	const item = (over: Partial<TaskDetail>): TaskDetail =>
		({
			task_id: 't',
			state: 'unassigned',
			source: { kind: 'chunks', keys: ['d/0'], where: 'lake~a$b' },
			legal_events: [],
			work: { revision: 0, tags: [], tags_cut: false, proposals: 0 },
			...over,
		}) as TaskDetail;
	const event = (name: string) => ({ event: name, to: '', permission: '' });

	it.each([
		[
			'their own claim is edited',
			item({ state: 'claimed', legal_events: [event('save_draft')] }),
			'edit',
		],
		[
			'work in review they may fix is edited',
			item({ state: 'in_review', legal_events: [event('fix_and_accept')] }),
			'edit',
		],
		[
			'an item they may claim is claimed first',
			item({ legal_events: [event('claim')] }),
			'claim-first',
		],
		['another person’s claim is not theirs', item({ state: 'claimed' }), 'another person holds it'],
		[
			'work they may not read is not theirs',
			item({ work: null }),
			'you may not read this item’s work',
		],
		[
			'an item past the tags read is the canvas’s',
			item({
				legal_events: [event('claim')],
				work: { revision: 1, tags: [], tags_cut: true, proposals: 0 },
			}),
			'it holds more tags than a grid reads: open it on the canvas',
		],
	])('%s', (_name, task, expected) => {
		const answer = labelable(task);
		expect(answer.kind === 'no' ? answer.reason : answer.kind).toBe(expected);
	});
});

describe('where a key moves the focus', () => {
	const shape = { count: 10, columns: 4, pageRows: 2 };

	it.each([
		['right', 1, 'ArrowRight', false, 2],
		['left from the first stays', 0, 'ArrowLeft', false, 0],
		['down a row', 1, 'ArrowDown', false, 5],
		['down into a short last row lands on its last cell', 7, 'ArrowDown', false, 9],
		['down from the last row stays', 8, 'ArrowDown', false, 8],
		['up from the first row stays', 2, 'ArrowUp', false, 2],
		['home to the row’s start', 6, 'Home', false, 4],
		['end to the row’s end', 4, 'End', false, 7],
		['ctrl+end to the last', 1, 'End', true, 9],
		['ctrl+home to the first', 6, 'Home', true, 0],
		['page down by its rows', 0, 'PageDown', false, 8],
	] as const)('%s', (_name, at, key, ctrl, expected) => {
		expect(moved(at, key, ctrl, shape)).toBe(expected);
	});

	it('is no move for any other key', () => {
		expect(moved(3, 'a', false, shape)).toBeNull();
	});

	it('reads a digit from the physical key, the keypad’s too, and no zero', () => {
		expect([digitOf('Digit3'), digitOf('Numpad9'), digitOf('Digit0'), digitOf('KeyA')]).toEqual([
			3,
			9,
			null,
			null,
		]);
	});
});

describe('the grid’s view in the address', () => {
	it('reads back what it wrote, the defaults left out and the other names kept', () => {
		const query = {
			...DEFAULT_GRID,
			pass: 'review' as const,
			label: 'map',
			page: 3,
			size: 100,
			at: 't-9',
		};
		const written = writeGridQuery(query, new URLSearchParams({ tab: 'label', size: '7' }));

		expect(written.toString()).toBe('tab=label&pass=review&label=map&page=3&size=100&at=t-9');
		expect(readGridQuery(written)).toEqual(query);
	});

	it('leaves at its default what it cannot honour', () => {
		expect(
			readGridQuery(new URLSearchParams('pass=x&size=7&page=-1&at=' + 'x'.repeat(513))),
		).toEqual(DEFAULT_GRID);
	});

	it.each([
		[
			'the label pass lists what is open to the reader',
			'label',
			'me',
			'open_to=me&sort=item&order=asc',
		],
		[
			'the review pass lists others’ work in review',
			'review',
			'me',
			'state=in_review&not_submitter=me&sort=item&order=asc',
		],
		[
			'with nobody signed in, the states open to anyone',
			'label',
			null,
			'state=unassigned&state=claimed&state=changes_requested&sort=item&order=asc',
		],
	] as const)('%s', (_name, pass, me, expected) => {
		expect(gridDoorParams({ ...DEFAULT_GRID, pass }, me).toString()).toBe(expected);
	});
});

describe('a text cell’s excerpt', () => {
	it('keeps a short text whole and cuts a long one at a word', () => {
		expect([excerpt('  a  short\ntext '), excerpt('word '.repeat(10), 22)]).toEqual([
			'a short text',
			'word word word word…',
		]);
	});
});

describe('the question asked before a label replaces what others carry', () => {
	it('agrees with how many items it names, singular and plural', () => {
		expect([carryCount(1, 1), carryCount(1, 3), carryCount(2, 3)]).toEqual([
			'1 of 1 item carries',
			'1 of 3 items carries',
			'2 of 3 items carry',
		]);
	});
});

describe('whether an item takes what a model returns', () => {
	const classes = (...tools: string[][]) => ({
		ontology: {
			kind: '',
			modality: 'image',
			classes: tools.map((t, i) => ({
				name: `c${i}`,
				tools: t,
				required: false,
				transcribe: false,
				attributes: [],
			})),
			relations: [],
			allow_empty: false,
		},
	});
	it('takes a model when one of its classes takes a shape the model returns', () => {
		expect(fits({ returns: ['bbox', 'polygon'] }, classes(['tag'], ['bbox']))).toBe(true);
	});
	it('takes no model whose every shape no class takes', () => {
		expect(fits({ returns: ['polygon'] }, classes(['tag'], ['bbox']))).toBe(false);
	});
	it('takes any model where a class names no tool, or no class is declared', () => {
		expect(fits({ returns: ['polygon'] }, classes(['tag'], []))).toBe(true);
		expect(fits({ returns: ['polygon'] }, { ontology: undefined })).toBe(true);
	});
});
