import { describe, expect, it } from 'vitest';
import type { LayoutConfig } from 'horizon-layout';
import { defaultLayout } from './layout.ts';
import { openWorkbenchStore, viewsOf, type ViewSnapshot } from './store.ts';
import { DockViews } from './views.svelte.ts';

/** A `Storage` over a Map: the test environment has no DOM. */
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

const resultsOnly: LayoutConfig = { root: { tabs: ['results'], activeTabIndex: 0 } };

const D = 'archive~field$survey';
const ATLAS = { place: 'vector', vector: 'embedding', color: 'site' } as const;

/** The sidebar's state model over a store, with what is on screen in `live`. */
async function viewsOver(storage: Storage, dataset = D) {
	const store = await openWorkbenchStore(null, () => storage);
	const live: { layout: LayoutConfig; view: () => ViewSnapshot; setups: ViewSnapshot['setups'] } = {
		layout: defaultLayout(),
		setups: {},
		view: () => ({ layout: live.layout, table: null, setups: live.setups, q: null, node: null }),
	};
	const views = new DockViews(viewsOf(store, dataset), () => live.view());
	await views.refresh();
	return { views, live };
}

describe('the named views', () => {
	it('refuses every write while the stored views cannot be read', async () => {
		const stored = JSON.stringify({ views: 'not a list' });
		const storage = memoryStorage({ 'workbench-dock:local': stored });
		const { views } = await viewsOver(storage);
		expect(views.phase).toBe('unreadable');

		expect(await views.saveAs('X')).toBe(false);
		expect(await views.rename('a', 'Y')).toBe(false);
		expect(await views.remove('a')).toBe(false);
		expect(views.lastError).toMatch(/refused/);
		expect(storage.getItem('workbench-dock:local')).toBe(stored);
	});

	it('marks the active view modified when the dock moves away from it, and clears it on the way back', async () => {
		const { views, live } = await viewsOver(memoryStorage());
		expect(await views.saveAs('Both')).toBe(true);
		expect(views.dirty).toBe(false);

		live.layout = resultsOnly;
		views.touch();
		expect(views.dirty).toBe(true);

		live.layout = defaultLayout();
		views.touch();
		expect(views.dirty).toBe(false);
	});

	it('takes the saved arrangement as the new baseline', async () => {
		const { views, live } = await viewsOver(memoryStorage());
		await views.saveAs('Both');
		live.layout = resultsOnly;
		views.touch();
		expect(await views.save()).toBe(true);
		expect(views.dirty).toBe(false);
		expect(views.select(views.activeId ?? '')).toMatchObject({
			status: 'ok',
			view: { layout: resultsOnly },
		});
	});

	it('keeps a plain table’s set-ups with the view, marks a changed set-up modified, and one view of a dataset default', async () => {
		const storage = memoryStorage();
		const { views, live } = await viewsOver(storage);
		live.setups = { atlas: ATLAS };
		await views.saveAs('By site');
		live.setups = {};
		expect(views.dirty).toBe(true);
		await views.saveAs('Plain');
		const [plainId, siteId] = views.views.map((v) => v.id);
		await views.toggleDefault(siteId ?? '');
		await views.toggleDefault(plainId ?? '');

		const reopened = (await viewsOver(storage)).views;
		const other = (await viewsOver(storage, 'scans~')).views;
		expect(reopened.select(siteId ?? '')).toMatchObject({
			status: 'ok',
			view: { setups: { atlas: ATLAS } },
		});
		expect([reopened.defaultId, other.views]).toEqual([plainId, []]);
	});

	it('refuses to apply a stored view whose set-up does not read, and says why', async () => {
		const storage = memoryStorage({
			'workbench-dock:local': JSON.stringify({
				views: [
					{
						id: 'v',
						name: 'Bad',
						dataset: D,
						layout: defaultLayout(),
						setups: { atlas: { place: 'columns', x: '' } },
						updated: '2026-11-16T00:00:00Z',
					},
				],
			}),
		});
		const { views } = await viewsOver(storage);
		expect(views.select('v')).toMatchObject({ status: 'unreadable' });
	});

	it('deleting the active view leaves the arrangement on screen as the draft', async () => {
		const { views, live } = await viewsOver(memoryStorage());
		await views.saveAs('Both');
		const id = views.activeId ?? '';
		expect(await views.remove(id)).toBe(true);
		expect(views.activeId).toBeNull();
		expect(views.views).toEqual([]);
		expect(live.layout).toEqual(defaultLayout());
	});
});
