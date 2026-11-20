import type { Page } from '@playwright/test';
import { ADAPTER_WAIT_MS } from '../src/lib/browser-support.ts';

/**
 * Make the browser's GPU adapter answer late: after `ms`, with the adapter it has, or with none (`none`), as a browser
 * whose GPU is slow to start, or whose GPU goes away after the page has started, does. The app waits a bounded time for
 * that answer before it starts (`ADAPTER_WAIT_MS`, `#lib/browser-support.ts`), so a late answer of none reaches the
 * canvases' own refusals rather than the unsupported-browser page. Every document of the page waits, a sign-in's round
 * trip included, so a test that uses it is marked slow.
 */
export async function adapterAnswersLate(page: Page, ms: number, none = false): Promise<void> {
	await page.addInitScript(
		({ ms, none }) => {
			const gpu = navigator.gpu;
			if (!gpu) return;
			const requestAdapter = gpu.requestAdapter.bind(gpu);
			gpu.requestAdapter = async (options) => {
				const adapter = await requestAdapter(options);
				await new Promise((done) => setTimeout(done, ms));
				return none ? null : adapter;
			};
		},
		{ ms, none },
	);
}

/** Past the app's wait for the adapter (`ADAPTER_WAIT_MS`), so the app starts before the answer comes. */
export const AFTER_THE_WAIT_MS = ADAPTER_WAIT_MS + 300;

/** Slow, but within the app's wait for the adapter, so the app waits for the answer. */
export const WITHIN_THE_WAIT_MS = ADAPTER_WAIT_MS - 500;

/** Record whether the unsupported-browser page is ever put on screen, read back with `sawUnsupported`. */
export async function watchUnsupported(page: Page): Promise<void> {
	await page.addInitScript(() => {
		const seen = { unsupported: false };
		(window as unknown as { __unsupported: typeof seen }).__unsupported = seen;
		new MutationObserver(() => {
			if (document.title === 'This browser cannot run the workbench') seen.unsupported = true;
		}).observe(document, { subtree: true, childList: true, characterData: true });
	});
}

export function sawUnsupported(page: Page): Promise<boolean> {
	return page.evaluate(
		() =>
			(window as unknown as { __unsupported: { unsupported: boolean } }).__unsupported.unsupported,
	);
}
