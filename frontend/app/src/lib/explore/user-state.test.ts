import { describe, expect, it } from 'vitest';
import {
	readUserState,
	writeUserState,
	type UserStateReader,
	type UserStateWriter,
} from '#lib/explore/user-state.ts';

/**
 * The three outcomes, and above all the difference between "you have nothing saved" and "we cannot read
 * what you have". Conflating them is what destroys a person's work: told the document is empty, the canvas
 * seeds a fresh one and the next autosave overwrites a record that is still there. The stubs hand back the
 * `ApiResult` a transport answers.
 */
/** A store that answered with a document envelope. */
const answered =
	(data: { exists?: boolean; value?: unknown }): UserStateReader =>
	async () => ({ ok: true, data });
/** A store that answered with a status. `0` is a store nobody could use (no person, storage refused). */
const refused =
	(status: number, detail = ''): UserStateReader =>
	async () => ({ ok: false, status, detail });
const writeAnswered = (): UserStateWriter => async () => ({ ok: true, data: {} });
const writeRefused =
	(status: number): UserStateWriter =>
	async () => ({ ok: false, status, detail: '' });

describe('readUserState', () => {
	it('a saved document comes back', async () => {
		const got = await readUserState<{ nodes: number[] }>(
			'workflow-graph',
			answered({ exists: true, value: { nodes: [1, 2] } }),
		);

		expect(got).toEqual({ status: 'ok', value: { nodes: [1, 2] } });
	});

	it('never saved is ABSENT — the caller may seed', async () => {
		expect(await readUserState('saved-views', answered({ exists: false }))).toEqual({
			status: 'absent',
		});
	});

	it('a 409 is UNREADABLE, never absent — this is the data-loss case', async () => {
		const got = await readUserState(
			'workflow-graph',
			refused(409, 'your saved workflow-graph exists but cannot be read'),
		);

		expect(got.status).toBe('unreadable');
		// The message has to reach the user: "we could not read it" and "you have nothing" look identical
		// on a blank canvas, and only one of them means their work is still there.
		expect(got).toHaveProperty('detail', expect.stringContaining('cannot be read'));
	});

	it('an unreachable store is UNREADABLE, not absent', async () => {
		expect((await readUserState('workflow-graph', refused(0, 'network down'))).status).toBe(
			'unreadable',
		);
	});

	it('a transport that rejects is unreadable too, never absent', async () => {
		// It must not escape as an unhandled rejection, and must not read as empty.
		const offline: UserStateReader = async () => {
			throw new Error('failed to fetch');
		};

		expect((await readUserState('workflow-graph', offline)).status).toBe('unreadable');
	});

	it('a 500 is unreadable — any non-answer is, since none of them mean "empty"', async () => {
		expect((await readUserState('saved-views', refused(500))).status).toBe('unreadable');
	});
});

describe('writeUserState', () => {
	it('reports that the store took the document', async () => {
		expect(await writeUserState('saved-views', [{ name: 'a' }], writeAnswered())).toBe(true);
	});

	it('a refused write is false', async () => {
		expect(await writeUserState('workflow-graph', { nodes: [] }, writeRefused(409))).toBe(false);
	});
});
