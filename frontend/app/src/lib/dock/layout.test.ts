import { describe, expect, it } from 'vitest';
import { validateConfig, type Id } from 'horizon-layout';
import type { PanelGates } from '#lib/explore/pane-capabilities.ts';
import {
	PANELS,
	closePanel,
	closedPanels,
	defaultLayout,
	maximizePanel,
	openPanel,
	popOut,
	readStoredLayout,
	revealPanel,
	shownPanels,
	type LayoutFit,
} from './layout.ts';

const registry: ReadonlyMap<Id, true> = new Map(PANELS.map((p) => [p.id, true] as const));

const OPEN: PanelGates = { results: null, player: null, atlas: null, graph: null, tree: null };
const NO_VIEWS: PanelGates = { ...OPEN, atlas: 'no', graph: 'no', tree: 'no' };
const fits: Record<string, LayoutFit> = {
	'a page corpus with every view': { kind: 'image', gates: OPEN },
	'a page corpus with none': { kind: 'image', gates: NO_VIEWS },
	'a recording with an atlas': { kind: 'time', gates: { ...NO_VIEWS, atlas: null } },
	'a recording with none': { kind: 'time', gates: NO_VIEWS },
	'a plain table': { kind: 'plain', gates: NO_VIEWS },
};

describe('the panel registry and the default layout', () => {
	it('names each panel once, and every default is one the library accepts, side by side or stacked', () => {
		expect(new Set(PANELS.map((p) => p.id)).size).toBe(PANELS.length);
		// A split under its minimum pane ratio renders no dock.
		for (const fit of Object.values(fits)) {
			for (const narrow of [false, true]) {
				expect(() => validateConfig(defaultLayout(narrow, fit), registry)).not.toThrow();
			}
		}
	});

	it('opens only the panels the dataset can feed, its views tabbed together with the atlas in front', () => {
		const every = defaultLayout(false, fits['a page corpus with every view']!);
		expect(closedPanels(every)).toEqual([]);
		expect(shownPanels(every)).toEqual(['results', 'player', 'atlas']);
		expect(closedPanels(defaultLayout(false, fits['a page corpus with none']!))).toEqual([
			'atlas',
			'graph',
			'tree',
		]);
		expect(shownPanels(defaultLayout(false, fits['a recording with an atlas']!))).toEqual([
			'results',
			'player',
			'atlas',
		]);
		expect(shownPanels(defaultLayout(true, fits['a page corpus with every view']!))).toEqual([
			'results',
			'player',
		]);
		const plain = defaultLayout(false, fits['a plain table']!);
		expect(shownPanels(plain)).toEqual(['results', 'player']);
		expect(closedPanels(plain)).toEqual(['atlas', 'graph', 'tree']);
	});
});

describe('bringing a panel in front', () => {
	it('fronts a tab behind another, restores a maximized other panel, and opens a closed one', () => {
		const base = defaultLayout(false, fits['a page corpus with every view']!);
		expect(shownPanels(revealPanel(base, 'graph'))).toEqual(['results', 'player', 'graph']);
		expect(revealPanel(base, 'results')).toBe(base);

		const maximized = maximizePanel(base, 'tree');
		expect(shownPanels(maximized)).toEqual(['tree']);
		expect(shownPanels(revealPanel(maximized, 'results'))).toEqual(['results', 'player', 'atlas']);

		const closed = closePanel(base, 'graph');
		expect(closedPanels(closed)).toEqual(['graph']);
		expect(shownPanels(revealPanel(closed, 'graph'))).toContain('graph');
	});
});

describe('maximizing a panel', () => {
	it('maximizes an open panel where it is, and opens a closed one before maximizing it', () => {
		const base = defaultLayout(false, fits['a page corpus with none']!);
		const open = maximizePanel(base, 'player');
		expect(open.maximizedView).toBe('player');
		expect(open.root).toEqual(base.root);

		const closed = maximizePanel(base, 'graph');
		expect(shownPanels(closed)).toEqual(['graph']);
		expect(closedPanels(closed)).not.toContain('graph');
		expect(() => validateConfig(closed, registry)).not.toThrow();
	});
});

describe('closing and opening a panel', () => {
	it('closes a panel out of its split and opens it again beside the panel whose control was used', () => {
		const recording = defaultLayout(false, fits['a recording with an atlas']!);
		const closed = closePanel(recording, 'atlas');
		expect(closed.root).toEqual({
			direction: 'horizontal',
			views: [
				{ tabs: ['results'], activeTabIndex: 0 },
				{ tabs: ['player'], activeTabIndex: 0 },
			],
			splitPoints: [0.48],
		});
		expect(closedPanels(closed)).toEqual(['atlas', 'graph', 'tree']);

		const reopened = openPanel(closePanel(closed, 'player'), 'atlas', 'results');
		expect(reopened.root).toEqual({ tabs: ['results', 'atlas'], activeTabIndex: 1 });
		expect(() => validateConfig(reopened, registry)).not.toThrow();
	});

	it('counts a popped-out panel as open, and closing it drops the window', () => {
		const out = popOut(defaultLayout(), 'atlas');
		expect(out.popouts).toEqual(['atlas']);
		expect(closedPanels(out)).not.toContain('atlas');
		expect(closePanel(out, 'atlas').popouts).toBeUndefined();
	});
});

describe('reading a stored layout', () => {
	it('accepts a layout of known panels and calls anything else unreadable', () => {
		expect(readStoredLayout(JSON.parse(JSON.stringify(defaultLayout())))).toEqual({
			status: 'ok',
			layout: defaultLayout(),
		});
		expect(readStoredLayout({ root: { tabs: ['gone'], activeTabIndex: 0 } }).status).toBe(
			'unreadable',
		);
		expect(readStoredLayout({ root: { tabs: [] } }).status).toBe('unreadable');
		expect(readStoredLayout('not a layout').status).toBe('unreadable');
	});

	it('docks the panels a stored layout had popped out, since no window survives a reload', () => {
		expect(readStoredLayout({ popouts: ['atlas', 'results'] })).toEqual({
			status: 'ok',
			layout: { root: { tabs: ['atlas', 'results'], activeTabIndex: 1 } },
		});
	});
});
