import adapter from '@sveltejs/adapter-static';
import { sveltekit } from '@sveltejs/kit/vite';
import { vitePreprocess } from '@sveltejs/vite-plugin-svelte';
import tailwindcss from '@tailwindcss/vite';
import { readFileSync } from 'node:fs';
import { createRequire } from 'node:module';
import type { ServerResponse } from 'node:http';
import { fileURLToPath } from 'node:url';
import { build, defineConfig, type Connect, type Plugin, type ProxyOptions } from 'vite';

// The dev server's runtime config: `WORKBENCH_APP_CONFIG_FILE` when set, else the committed
// `dev/config.json`, which turns sign-in off (`{"signIn": false}`) and names each service's public base,
// as the local stack runs them (`scripts/local-stack.sh`). A config with sign-in names an issuer and the
// workbench's own public client instead. The preview server serves the built app as the image does, so
// it reads `WORKBENCH_APP_CONFIG_FILE` alone. Either way a config the app cannot use fails closed.
const DEV_CONFIG = fileURLToPath(new URL('./dev/config.json', import.meta.url));

/** The issuer's origin, when the config names an absolute issuer. */
function issuerOrigin(file: string): string | null {
	try {
		const issuer: unknown = (JSON.parse(readFileSync(file, 'utf8')) as { issuer?: unknown }).issuer;
		return typeof issuer === 'string' && URL.canParse(issuer) ? new URL(issuer).origin : null;
	} catch {
		return null;
	}
}

/** `connect-src` widened by exactly one origin, every other directive as SvelteKit wrote it. */
function allowConnect(policy: string, origin: string): string {
	return policy
		.split(';')
		.map((d) => d.trim())
		.filter(Boolean)
		.map((d) => (d.startsWith('connect-src ') ? `${d} ${origin}` : d))
		.join('; ');
}

function appConfig(): Plugin {
	const serveFrom =
		(file: string | undefined): Connect.NextHandleFunction =>
		(req, res, next) => {
			if (req.url !== '/config.json' || !file) return next();
			res.setHeader('content-type', 'application/json');
			res.setHeader('cache-control', 'no-store');
			res.end(readFileSync(file));
		};
	// Under `vite dev` SvelteKit sends the plugin's `csp` as a response HEADER, which would block the
	// browser's discovery and token requests to a cross-origin issuer. The page's policy gains that
	// issuer's origin in `connect-src`, as `serve.ts` does for the image; the built app's own policy is
	// untouched.
	const allowIssuer =
		(file: string): Connect.NextHandleFunction =>
		(_req, res, next) => {
			const origin = issuerOrigin(file);
			if (!origin) return next();
			const setHeader = res.setHeader.bind(res);
			res.setHeader = (name: string, value: number | string | readonly string[]): ServerResponse =>
				setHeader(
					name,
					name.toLowerCase() === 'content-security-policy' && typeof value === 'string'
						? allowConnect(value, origin)
						: value,
				);
			next();
		};
	return {
		name: 'app-config',
		configureServer: (server) => {
			const file = process.env.WORKBENCH_APP_CONFIG_FILE || DEV_CONFIG;
			server.middlewares.use(serveFrom(file));
			server.middlewares.use(allowIssuer(file));
		},
		configurePreviewServer: (server) =>
			void server.middlewares.use(serveFrom(process.env.WORKBENCH_APP_CONFIG_FILE)),
	};
}

/**
 * The appearance boot script, `/appearance-boot.js`, which `app.html` loads before anything paints: built from
 * `@workbench/ui/appearance-boot` and the record definition it imports (`core.ts`), the same code the app's store reads
 * the record with, into one classic script (a module would run after the first paint). Same-origin, so the strict CSP
 * admits it with no hash. Emitted into the client build, and served by the dev server.
 */
function appearanceBoot(): Plugin {
	const entry = createRequire(import.meta.url).resolve('@workbench/ui/appearance-boot');
	const bundle = async (): Promise<string> => {
		const result = await build({
			configFile: false,
			logLevel: 'silent',
			build: {
				write: false,
				minify: true,
				lib: {
					entry,
					formats: ['iife'],
					name: 'appearanceBoot',
					fileName: () => 'appearance-boot.js',
				},
			},
		});
		const outputs = Array.isArray(result) ? result : [result];
		for (const output of outputs) {
			if ('output' in output) {
				const chunk = output.output.find((item) => item.type === 'chunk');
				if (chunk && 'code' in chunk) return chunk.code;
			}
		}
		throw new Error('the appearance boot script built to nothing');
	};
	return {
		name: 'appearance-boot',
		async generateBundle() {
			this.emitFile({ type: 'asset', fileName: 'appearance-boot.js', source: await bundle() });
		},
		configureServer: (server) =>
			void server.middlewares.use((req, res, next) => {
				if (req.url !== '/appearance-boot.js') return next();
				void bundle().then((code) => {
					res.setHeader('content-type', 'text/javascript');
					res.end(code);
				}, next);
			}),
	};
}

// Every `/api/*` call is same-origin. The dev server forwards them to the Gateway at
// `WORKBENCH_EDGE_URL`, the same routes a deployed app takes: the local stack's `edge.ts` serves them.
// (The Playwright suite runs the built app behind `edge.ts` with its mocks instead.)
const EDGE = process.env.WORKBENCH_EDGE_URL ?? 'http://127.0.0.1:8700';
const proxy: Record<string, ProxyOptions> = { '/api': { target: EDGE, changeOrigin: true } };

export default defineConfig({
	plugins: [
		appConfig(),
		appearanceBoot(),
		tailwindcss(),
		sveltekit({
			preprocess: vitePreprocess(),
			compilerOptions: { experimental: { async: true } },
			// A single-page app with no backend of its own: every route renders in the browser from
			// `index.html`, which `serve.ts` (and any static server) answers for an unknown path.
			adapter: adapter({ fallback: 'index.html' }),
			// The content security policy travels with the app. `hash` puts the SHA-256 of SvelteKit's
			// inline bootstrap script into the page's own CSP meta tag; `serve.ts` sends the same policy as
			// a header, adding the issuer's origin to `connect-src` when the issuer is another origin.
			csp: {
				mode: 'hash',
				directives: {
					'default-src': ['self'],
					'script-src': ['self'],
					// Svelte and Bits UI set inline `style` attributes (positioning, transitions).
					'style-src': ['self', 'unsafe-inline'],
					// Signed media is same-origin: the viewer's links are paths on this origin.
					'img-src': ['self', 'data:', 'blob:'],
					'media-src': ['self'],
					'font-src': ['self', 'data:'],
					'connect-src': ['self'],
					'worker-src': ['self', 'blob:'],
					'object-src': ['none'],
					'base-uri': ['self'],
					'form-action': ['self'],
				},
			},
		}),
	],
	server: { port: 5173, strictPort: true, proxy },
	preview: { port: 4173, strictPort: true, proxy },
});
