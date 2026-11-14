import { describe, expect, it } from 'vitest';
import { validateConfig, type Id } from 'horizon-layout';
import {
	PANELS,
	closePanel,
	closedPanels,
	defaultLayout,
	openPanel,
	popOut,
	readStoredLayout,
} from './layout.ts';

const registry: ReadonlyMap<Id, true> = new Map(PANELS.map((p) => [p.id, true] as const));

describe('the panel registry and the default layout', () => {
	it('names each panel once, and the first visit opens every panel side by side', () => {
		expect(new Set(PANELS.map((p) => p.id)).size).toBe(PANELS.length);
		const layout = defaultLayout();
		expect(() => validateConfig(layout, registry)).not.toThrow();
		expect(closedPanels(layout)).toEqual([]);
	});
});

describe('closing and opening a panel', () => {
	it('closes a panel out of its split and opens it again beside the panel whose control was used', () => {
		const closed = closePanel(defaultLayout(), 'atlas');
		expect(closed.root).toEqual({
			direction: 'horizontal',
			views: [
				{ tabs: ['results'], activeTabIndex: 0 },
				{ tabs: ['media'], activeTabIndex: 0 },
			],
			splitPoints: [0.55],
		});
		expect(closedPanels(closed)).toEqual(['atlas']);

		const reopened = openPanel(closePanel(closed, 'media'), 'atlas', 'results');
		expect(reopened.root).toEqual({ tabs: ['results', 'atlas'], activeTabIndex: 1 });
		expect(() => validateConfig(reopened, registry)).not.toThrow();
	});

	it('counts a popped-out panel as open, and closing it drops the window', () => {
		const out = popOut(defaultLayout(), 'atlas');
		expect(out.popouts).toEqual(['atlas']);
		expect(closedPanels(out)).toEqual([]);
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
