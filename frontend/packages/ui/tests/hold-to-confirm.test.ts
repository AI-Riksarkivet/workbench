// @vitest-environment happy-dom
import { flushSync, mount, unmount } from 'svelte';
import { afterEach, describe, expect, test, vi } from 'vitest';
import HoldToConfirm from '../src/lib/components/hold-to-confirm/hold-to-confirm.svelte';

/** Mounts the button as the bell uses it; `reduced` stands in for the reader's reduced-motion setting. */
function render({ reduced = false, duration = 60 } = {}) {
	vi.spyOn(window, 'matchMedia').mockImplementation(
		(query: string) =>
			({
				matches: reduced && query.includes('reduce'),
				media: query,
				addEventListener: () => undefined,
				removeEventListener: () => undefined,
			}) as unknown as MediaQueryList,
	);
	const onConfirm = vi.fn();
	const component = mount(HoldToConfirm, {
		target: document.body,
		props: {
			label: 'Clear',
			armedLabel: 'Press again',
			confirmedLabel: 'Cleared',
			duration,
			onConfirm,
		},
	});
	flushSync();
	const button = document.querySelector<HTMLButtonElement>('[data-slot="hold-to-confirm"]')!;
	const status = document.querySelector('[role="status"]')!;
	const fill = document.querySelector<HTMLElement>('[data-hold-fill]')!;
	return { component, button, status, fill, onConfirm };
}

const wait = (ms: number) => new Promise((done) => setTimeout(done, ms));
const key = (button: HTMLElement, type: 'keydown' | 'keyup', k: string) =>
	button.dispatchEvent(new KeyboardEvent(type, { key: k, bubbles: true, cancelable: true }));

let mounted: ReturnType<typeof mount> | null = null;
afterEach(() => {
	if (mounted) unmount(mounted);
	mounted = null;
	document.body.innerHTML = '';
	vi.restoreAllMocks();
});

describe('a confirm-before-acting button', () => {
	test('its name says it can be held or pressed twice', () => {
		const { component, button } = render();
		mounted = component;
		expect(button.getAttribute('aria-label')).toBe('Clear — hold, or press twice');
	});

	test('one click arms it and says so, and confirms nothing', () => {
		const { component, button, status, onConfirm } = render();
		mounted = component;
		button.click();
		flushSync();
		expect(onConfirm).not.toHaveBeenCalled();
		expect(status.textContent).toBe('Press again to clear');
		expect(button.getAttribute('aria-label')).toBe('Clear — press again to confirm');
	});

	test('a second click confirms, once', () => {
		const { component, button, onConfirm } = render();
		mounted = component;
		button.click();
		button.click();
		flushSync();
		expect(onConfirm).toHaveBeenCalledTimes(1);
		expect(button.getAttribute('aria-label')).toBe('Cleared');
	});

	test('Escape disarms it, so the next click only arms again', () => {
		const { component, button, status, onConfirm } = render();
		mounted = component;
		button.click();
		key(button, 'keydown', 'Escape');
		flushSync();
		expect(status.textContent).toBe('');
		button.click();
		flushSync();
		expect(onConfirm).not.toHaveBeenCalled();
	});

	test('a held Space confirms in one gesture', async () => {
		const { component, button, onConfirm } = render();
		mounted = component;
		key(button, 'keydown', ' ');
		await wait(200);
		key(button, 'keyup', ' ');
		expect(onConfirm).toHaveBeenCalledTimes(1);
	});

	test('under reduced motion a hold draws no fill while it counts, and still confirms', async () => {
		const { component, button, fill, onConfirm } = render({ reduced: true, duration: 120 });
		mounted = component;
		key(button, 'keydown', ' ');
		await wait(50);
		expect(fill.style.clipPath).toBe('inset(0 100.000% 0 0)');
		await wait(150);
		key(button, 'keyup', ' ');
		expect(onConfirm).toHaveBeenCalledTimes(1);
	});
});
