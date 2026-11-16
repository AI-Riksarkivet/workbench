import { afterEach, describe, expect, it, vi } from 'vitest';
import { LayoutAutosave } from './autosave.ts';
import { defaultLayout } from './layout.ts';
import { MAX_LAYOUTS, openWorkbenchStore, viewsOf } from './store.ts';
import type { LayoutConfig } from 'horizon-layout';

/** A `Storage` over a Map: the test environment has no DOM, as `activity.test.ts` does it. */
function memoryStorage(initial: Record<string, string> = {}): Storage {
	const kept = new Map(Object.entries(initial));
	return {
		getItem: (key) => kept.get(key) ?? null,
		setItem: (key, value) => void kept.set(key, value),
		removeItem: (key) => void kept.delete(key),
		clear: () => kept.clear(),
		key: (index) => [...kept.keys()][index] ?? null,
		get length() {
			return kept.size;
		},
	};
}

const refusing = (): Storage => {
	throw new DOMException('denied', 'SecurityError');
};

/** The dataset the layouts below are stored for. */
const D = 'lake~acme$letters';

afterEach(() => {
	vi.useRealTimers();
});

describe('the workbench store', () => {
	it('keys a person by a hash of their id, and gives back each dataset’s layout and the views they stored', async () => {
		const storage = memoryStorage();
		const store = await openWorkbenchStore('user:alice', () => storage);
		expect(store.loadLayout(D)).toEqual({ status: 'absent' });

		const layout: LayoutConfig = { root: { tabs: ['atlas', 'results'], activeTabIndex: 1 } };
		store.saveLayout(D, layout);
		await viewsOf(store).create('Both', layout);

		const key = storage.key(0) ?? '';
		expect(key).toMatch(/^workbench-dock:[0-9a-f]{64}$/);
		expect(key).not.toContain('alice');
		const again = await openWorkbenchStore('user:alice', () => storage);
		expect(again.loadLayout(D)).toEqual({ status: 'ok', layout });
		// Another dataset opens with its own default: a layout is the dataset's, the views are the person's.
		expect(again.loadLayout('scans~')).toEqual({ status: 'absent' });
		expect(again.loadViews()).toMatchObject({ status: 'ok', views: [{ name: 'Both', layout }] });
		expect((await openWorkbenchStore('user:bob', () => storage)).loadLayout(D).status).toBe(
			'absent',
		);
	});

	it('calls a stored layout it cannot show unreadable, saves nothing over it until a reset', async () => {
		vi.useFakeTimers();
		const stored = JSON.stringify({
			layouts: [{ dataset: D, layout: { root: { tabs: ['gone'], activeTabIndex: 0 } } }],
		});
		const storage = memoryStorage({ 'workbench-dock:local': stored });

		const store = await openWorkbenchStore(null, () => storage);
		const autosave = new LayoutAutosave(store.layoutOf(D), 10);
		expect(autosave.restore().status).toBe('unreadable');
		autosave.schedule(defaultLayout());
		await vi.advanceTimersByTimeAsync(50);
		expect(storage.getItem('workbench-dock:local')).toBe(stored);

		autosave.reset(defaultLayout());
		expect(JSON.parse(storage.getItem('workbench-dock:local') ?? '')).toEqual({
			layouts: [{ dataset: D, layout: defaultLayout() }],
		});
	});

	it('works from memory, and says so, where the browser refuses storage', async () => {
		const store = await openWorkbenchStore('user:alice', refusing);
		expect(store.persistent).toBe(false);
		expect(store.loadLayout(D)).toEqual({ status: 'absent' });
		store.saveLayout(D, defaultLayout());
		expect(store.loadLayout(D)).toEqual({ status: 'ok', layout: defaultLayout() });
	});

	it('lets two tabs of one person write without either erasing what the other saved', async () => {
		const storage = memoryStorage();
		const tabA = await openWorkbenchStore('user:alice', () => storage);
		const tabB = await openWorkbenchStore('user:alice', () => storage);

		expect(await viewsOf(tabB).create('Kept', defaultLayout())).not.toBeNull();
		tabA.saveLayout(D, { root: { tabs: ['results'], activeTabIndex: 0 } });

		const reloaded = await openWorkbenchStore('user:alice', () => storage);
		expect(reloaded.loadViews()).toMatchObject({ status: 'ok', views: [{ name: 'Kept' }] });
		expect(reloaded.loadLayout(D)).toEqual({
			status: 'ok',
			layout: { root: { tabs: ['results'], activeTabIndex: 0 } },
		});
	});

	it('refuses a write over a document another tab has since made unreadable', async () => {
		const storage = memoryStorage({
			'workbench-dock:local': JSON.stringify({
				layouts: [{ dataset: D, layout: defaultLayout() }],
			}),
		});
		const store = await openWorkbenchStore(null, () => storage);
		expect(store.loadLayout(D).status).toBe('ok');

		storage.setItem('workbench-dock:local', '{not json');
		expect(store.saveLayout(D, defaultLayout())).toBe(false);
		expect(await viewsOf(store).create('X', defaultLayout())).toBeNull();
		expect(storage.getItem('workbench-dock:local')).toBe('{not json');
	});

	it('keeps unreadable views, and the layout beside them, until the views are reset', async () => {
		const stored = JSON.stringify({
			layouts: [{ dataset: D, layout: defaultLayout() }],
			views: [{ id: 1 }],
		});
		const storage = memoryStorage({ 'workbench-dock:local': stored });
		const store = await openWorkbenchStore(null, () => storage);
		const views = viewsOf(store);
		expect((await views.list()).status).toBe('unreadable');
		expect(await views.create('X', defaultLayout())).toBeNull();
		expect(storage.getItem('workbench-dock:local')).toBe(stored);

		expect(await views.reset()).toBe(true);
		expect(await views.list()).toEqual({ status: 'ok', views: [] });
		expect(store.loadLayout(D)).toEqual({ status: 'ok', layout: defaultLayout() });
	});

	it('stops saving, and says why, when a save finds the stored document unreadable', async () => {
		vi.useFakeTimers();
		const storage = memoryStorage({
			'workbench-dock:local': JSON.stringify({
				layouts: [{ dataset: D, layout: defaultLayout() }],
			}),
		});
		const store = await openWorkbenchStore(null, () => storage);
		const refused: string[] = [];
		const autosave = new LayoutAutosave(store.layoutOf(D), 10, (detail) => refused.push(detail));
		expect(autosave.restore().status).toBe('ok');

		storage.setItem('workbench-dock:local', '{not json');
		autosave.schedule({ root: { tabs: ['results'], activeTabIndex: 0 } });
		await vi.advanceTimersByTimeAsync(50);
		expect(autosave.saving).toBe(false);
		expect(refused).toHaveLength(1);
		expect(storage.getItem('workbench-dock:local')).toBe('{not json');
	});

	it('keeps the most recently arranged datasets’ layouts, and forgets the oldest beyond the bound', async () => {
		const storage = memoryStorage();
		const store = await openWorkbenchStore(null, () => storage);
		const layout: LayoutConfig = { root: { tabs: ['results'], activeTabIndex: 0 } };
		for (let i = 0; i <= MAX_LAYOUTS; i++) store.saveLayout(`d${i}`, layout);
		// Arranging the first again makes it the most recent.
		store.saveLayout('d1', layout);
		store.saveLayout('another', layout);
		expect(store.loadLayout('d0').status).toBe('absent');
		expect(store.loadLayout('d2').status).toBe('absent');
		expect(store.loadLayout('d1').status).toBe('ok');
		expect(store.loadLayout(`d${MAX_LAYOUTS}`).status).toBe('ok');
		// A dataset named like an object's own property is a dataset like any other.
		expect(store.loadLayout('constructor').status).toBe('absent');
		store.saveLayout('__proto__', layout);
		expect(store.loadLayout('__proto__')).toEqual({ status: 'ok', layout });
	});
});
