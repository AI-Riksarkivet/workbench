import { describe, expect, it } from 'vitest';

import { activity } from './activity.svelte';
import { command, query } from './reads.svelte';

/** A read whose every start waits for the test to answer it, in whatever order the test chooses. */
function heldRead() {
	const answer: ((value: string) => void)[] = [];
	const read = query(() => new Promise<string>((resolve) => answer.push(resolve)));
	return { read, answer };
}

/** Let every pending promise callback run: a read starts after its input is validated. */
const settle = (): Promise<void> => new Promise((resolve) => setTimeout(resolve, 0));

describe('query', () => {
	it('keeps the newest read when an older one lands after it', async () => {
		const { read, answer } = heldRead();
		const held = read();
		void held.refresh();
		await settle();

		answer[1]!('newer');
		await settle();
		answer[0]!('older');
		await settle();

		expect(held.current).toBe('newer');
		expect(held.loading).toBe(false);
	});

	it('stays loading while the newest read is out, though an older one landed', async () => {
		const { read, answer } = heldRead();
		const held = read();
		void held.refresh();
		await settle();

		answer[0]!('older');
		await settle();

		expect(held.loading).toBe(true);
		expect(held.current).toBeUndefined();
	});
});

describe('the instance cache', () => {
	it('answers the same arguments with the same instance, so a refresh reaches every holder', async () => {
		let runs = 0;
		const read = query('unchecked', async (id: string) => `${id}:${++runs}`);

		const first = read('a');
		await first;
		const again = read('a');
		const other = read('b');

		await other;
		expect(again).toBe(first);
		expect(other).not.toBe(first);
		// Read again within a second of settling: the burst is served by the settled read.
		expect(runs).toBe(2);
		await first.refresh();
		expect(again.current).toBe('a:3');
	});
});

describe('a command', () => {
	it('records a write that got no answer in the reader’s activity log as NOT CONFIRMED', async () => {
		const write = command('Save annotation letter-001', async () => ({
			ok: false as const,
			status: 0,
			detail: 'NOT CONFIRMED: the annotator gave no answer, so the write may have landed',
		}));

		await write();

		expect(activity.entries[0]).toMatchObject({
			label: 'Save annotation letter-001',
			ok: false,
			status: 0,
			detail: expect.stringMatching(/^NOT CONFIRMED/),
		});
	});
});
