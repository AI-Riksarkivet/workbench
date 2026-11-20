/**
 * Whether this browser can run the workbench at all, and the page that says so when it cannot (ADR 0018).
 *
 * The supported browsers are current Chrome and Edge on a computer with a graphics card: every image, map, graph
 * and time strip is drawn with WebGPU and nothing else (ADR 0007), and the design system's theme computes its
 * colours with CSS relative colour syntax (Chromium 119, Firefox 128, Safari 16.4). A browser missing either is shown
 * one plain page instead of the app; there is no fallback renderer.
 *
 * Two callers: the classic script `/browser-check.js` (`browser-check.ts`, built by `vite.config.ts`), which `app.html`
 * runs in <head> before the app's stylesheets and modules, and the app's `init` hook (`hooks.client.ts`), which runs
 * before the first component renders. So a browser that cannot run the app never sees a half-drawn shell. This module
 * imports nothing, so the classic script is this file and a few lines.
 */

/** What is supported, in the words the page uses. */
const SUPPORTED = 'current Chrome or Edge, on a computer with a graphics card';

/**
 * How long the app waits, before it starts, for the browser to say whether it has a GPU adapter. Measured 2026-11-20:
 * a headless Chromium without one answers in about 10 ms, the software adapter the browser suite draws with in up to
 * 0.6 s. An answer later than this is not taken as "unsupported": the app starts, and each canvas still refuses
 * plainly if the adapter never comes (ADR 0007).
 */
export const ADAPTER_WAIT_MS = 2_000;

/** What this browser lacks that the app needs, known without waiting; `null` when it lacks neither. */
export function missingAtOnce(): string | null {
	if (typeof CSS === 'undefined' || !CSS.supports('color', 'rgb(from red r g b)')) {
		return 'This browser is too old for the colours the workbench is drawn in (it lacks CSS relative colour syntax).';
	}
	if (!('gpu' in navigator) || !navigator.gpu) {
		return 'This browser does not offer WebGPU, which the workbench draws every image, map and graph with.';
	}
	return null;
}

/** The reason `probe` gives, or `null` when it has not answered within `waitMs`. */
export async function answerWithin(
	probe: Promise<string | null>,
	waitMs: number,
): Promise<string | null> {
	let timer: ReturnType<typeof setTimeout> | undefined;
	const late = new Promise<null>((done) => (timer = setTimeout(() => done(null), waitMs)));
	try {
		return await Promise.race([probe, late]);
	} finally {
		clearTimeout(timer);
	}
}

function element<K extends keyof HTMLElementTagNameMap>(
	tag: K,
	style: string,
	text?: string,
): HTMLElementTagNameMap[K] {
	const node = document.createElement(tag);
	node.setAttribute('style', style);
	if (text !== undefined) node.textContent = text;
	return node;
}

/**
 * Replace the document's body with the page that says this browser cannot run the app, and why. Its styles are its
 * own (inline), so it reads the same whether or not the app's stylesheet has loaded: in the theme's colours where the
 * theme has loaded, otherwise in the browser's own colours for the mode the appearance boot script set.
 */
export function showUnsupported(reason: string): void {
	document.title = 'This browser cannot run the workbench';
	const body =
		document.body ?? document.documentElement.appendChild(document.createElement('body'));
	body.setAttribute(
		'style',
		'margin:0;min-height:100vh;background:var(--color-background, Canvas);color:var(--color-foreground, CanvasText);' +
			'font:16px/1.5 system-ui, -apple-system, "Segoe UI", sans-serif',
	);
	const main = element('main', 'max-width:40rem;margin:0 auto;padding:4rem 1.5rem');
	main.append(
		element(
			'h1',
			'font-size:1.5rem;line-height:1.3;margin:0 0 1rem;font-weight:600',
			'This browser cannot run the workbench',
		),
		element('p', 'margin:0 0 1rem', reason),
		element(
			'p',
			'margin:0 0 1rem',
			`The workbench runs in ${SUPPORTED}. It draws with WebGPU and has no other way to draw, so there is ` +
				'no setting to change here.',
		),
		element(
			'p',
			'margin:0;color:var(--color-muted-foreground, GrayText)',
			`Open this address in Chrome or Edge: ${location.href}`,
		),
	);
	body.textContent = '';
	body.appendChild(main);
}
