import { defineConfig, devices } from '@playwright/test';
import {
	EDGE,
	EDGE_PORT,
	MOCK_ANNOTATOR_PORT,
	MOCK_OIDC_PORT,
	MOCK_SEARCH_PORT,
	MOCK_VIEWER_PORT,
} from './e2e/ports';

// Hermetic e2e for the app, against the BUILT app (`vite build` first; `test:e2e` does it) served by
// its own `serve.ts` behind `edge.ts`, which routes each service's public base as the Gateway does.
// The app has no backend, so every call it makes is the browser's own, to a seed-driven mock service
// (`e2e/mock-services.ts`) that holds it to the services' DPoP rule. Sign-in runs the app's real PKCE and
// DPoP round trip against `e2e/mock-oidc.ts`, same-origin under `/oidc`.
const local = (port: number): string => `http://localhost:${port}`;

/** Chromium's software GPU adapter, which every project that runs the app draws with. */
const SOFTWARE_ADAPTER = [
	'--enable-unsafe-webgpu',
	'--enable-features=Vulkan',
	'--use-vulkan=swiftshader',
	'--use-webgpu-adapter=swiftshader',
	'--use-angle=swiftshader',
];

export default defineConfig({
	testDir: './e2e',
	timeout: 30_000,
	fullyParallel: true,
	workers: process.env.CI ? 2 : 4,
	forbidOnly: !!process.env.CI,
	retries: process.env.CI ? 1 : 0,
	reporter: process.env.CI ? 'github' : 'list',
	use: { baseURL: EDGE, trace: 'on-first-retry', ...devices['Desktop Chrome'] },
	// The supported browser is Chromium with a GPU adapter (ADR 0018), so every project that runs the app gives Chromium
	// its software adapter (SwiftShader over Vulkan, `--enable-unsafe-webgpu`). `chromium` runs the headless shell,
	// which offers that adapter (measured 2026-11-20: in 13 ms) but shows a WebGPU canvas blank in a screenshot.
	// `webgpu` runs the specs that read what a renderer drew, in the new headless mode (`channel: 'chromium'`), whose
	// screenshots composite a WebGPU canvas (measured 2026-11-14): the atlas and the graph, the labeling canvas
	// (`canvas.spec.ts`, and the write `auth.spec.ts` makes on it), the player's page viewer (`player.spec.ts`), a plain
	// table's set-up views (`setup.spec.ts`) and the time strip; the shell (`shell.spec.ts`) is read there because its page
	// transitions run as in a person's browser, which the headless shell's do not. `no-webgpu` (the headless shell with
	// no adapter) and `firefox` (no WebGPU) are the browsers the app does not support, and run only the page that says
	// so (`unsupported.spec.ts`).
	projects: [
		{
			name: 'chromium',
			testIgnore: /(webgpu|canvas|player|auth|setup|temporal|shell|unsupported)\.spec\.ts$/,
			use: { launchOptions: { args: SOFTWARE_ADAPTER } },
		},
		{
			name: 'webgpu',
			testMatch: /(webgpu|canvas|player|auth|setup|temporal|shell)\.spec\.ts$/,
			// Every frame here is drawn on the CPU, page transitions included: measured 2026-11-16 at 8 workers on a
			// loaded host, a 0.25 s view transition took 2.4 s to finish and the labeling canvas was ready 5-6 s
			// after the page load, past the 5 s default. An assertion in this project waits up to 10 s.
			expect: { timeout: 10_000 },
			use: { channel: 'chromium', launchOptions: { args: SOFTWARE_ADAPTER } },
		},
		{ name: 'no-webgpu', testMatch: /unsupported\.spec\.ts$/ },
		{
			name: 'firefox',
			testMatch: /unsupported\.spec\.ts$/,
			use: { ...devices['Desktop Firefox'] },
		},
	],
	webServer: [
		{
			command: 'bun edge.ts',
			port: EDGE_PORT,
			reuseExistingServer: !process.env.CI,
			timeout: 60_000,
			env: {
				PORT: String(EDGE_PORT),
				HOST: '127.0.0.1',
				WORKBENCH_APP_CONFIG_FILE: './e2e/app-config.json',
				OIDC_MOCK: local(MOCK_OIDC_PORT),
				VIEWER_API: local(MOCK_VIEWER_PORT),
				SEARCH_API: local(MOCK_SEARCH_PORT),
				ANNOTATOR_API: local(MOCK_ANNOTATOR_PORT),
			},
		},
		{ command: 'bun e2e/mock-oidc.ts', port: MOCK_OIDC_PORT, reuseExistingServer: !process.env.CI },
		{
			command: 'bun e2e/mock-services.ts',
			port: MOCK_VIEWER_PORT,
			reuseExistingServer: !process.env.CI,
		},
	],
});
