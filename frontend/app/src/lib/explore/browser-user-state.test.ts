import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import {
	readUserStateDoc,
	setUserStateOwner,
	writeUserStateDoc,
} from '#lib/explore/browser-user-state.ts';
import { readUserState } from '#lib/explore/user-state.ts';

/**
 * A person's documents live in this browser's storage under their own key. What matters is that one person never reads
 * another's, and that "nothing saved", "saved and readable" and "saved but unreadable" stay three answers through
 * `readUserState`, so an unreadable document is never seeded over.
 */
const store = new Map<string, string>();

beforeEach(() => {
	store.clear();
	vi.stubGlobal('localStorage', {
		getItem: (k: string) => store.get(k) ?? null,
		setItem: (k: string, v: string) => void store.set(k, v),
		removeItem: (k: string) => void store.delete(k),
	});
	setUserStateOwner('dex~alice');
});
afterEach(() => vi.unstubAllGlobals());

describe('the browser store', () => {
	it('gives back what the same person wrote', async () => {
		await writeUserStateDoc({ document: 'saved-views', value: [{ name: 'mine' }] });

		expect(await readUserState('saved-views', readUserStateDoc)).toEqual({
			status: 'ok',
			value: [{ name: 'mine' }],
		});
	});

	it("never gives one person another's documents", async () => {
		await writeUserStateDoc({ document: 'saved-views', value: [{ name: 'alice' }] });
		setUserStateOwner('dex~bob');

		expect(await readUserState('saved-views', readUserStateDoc)).toEqual({ status: 'absent' });
	});

	it('keeps nothing while no person is signed in', async () => {
		setUserStateOwner(null);

		expect((await writeUserStateDoc({ document: 'saved-views', value: [] })).ok).toBe(false);
		expect((await readUserState('saved-views', readUserStateDoc)).status).toBe('unreadable');
		expect(store.size).toBe(0);
	});

	it('answers unreadable, never absent, for a stored value that does not parse', async () => {
		await writeUserStateDoc({ document: 'workflow-graph', value: {} });
		const [key] = store.keys();
		store.set(key ?? '', '{not json');

		expect((await readUserState('workflow-graph', readUserStateDoc)).status).toBe('unreadable');
	});

	it('answers unreadable when the browser refuses storage', async () => {
		vi.stubGlobal('localStorage', {
			getItem: () => {
				throw new DOMException('denied', 'SecurityError');
			},
		});

		expect((await readUserState('saved-views', readUserStateDoc)).status).toBe('unreadable');
	});
});
