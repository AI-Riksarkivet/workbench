import type { ClientInit } from '@sveltejs/kit/hooks';
import {
	ADAPTER_WAIT_MS,
	answerWithin,
	missingAtOnce,
	showUnsupported,
} from '#lib/browser-support.ts';
import { gpuUnsupportedReason } from '#lib/gpu/gpu-support.ts';

/**
 * Before the first component renders: a browser that cannot run the app is shown the page that says so, and the app
 * never starts (ADR 0018). The GPU adapter is asked once, through the probe every canvas reads, so the app and its
 * canvases agree; an answer later than `ADAPTER_WAIT_MS` lets the app start, and the canvases refuse plainly if no
 * adapter comes.
 */
export const init: ClientInit = async () => {
	const reason = missingAtOnce() ?? (await answerWithin(gpuUnsupportedReason(), ADAPTER_WAIT_MS));
	if (reason === null) return;
	showUnsupported(reason);
	await new Promise<never>(() => {});
};
