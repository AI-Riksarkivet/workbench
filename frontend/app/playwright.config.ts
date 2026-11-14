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
// its own `serve.ts` behind `e2e/edge.ts`, which routes each service's public base as the Gateway does.
// The app has no backend, so every call it makes is the browser's own, to a seed-driven mock service
// (`e2e/mock-services.ts`) that holds it to the services' DPoP rule. Sign-in runs the app's real PKCE and
// DPoP round trip against `e2e/mock-oidc.ts`, same-origin under `/oidc`.
const local = (port: number): string => `http://localhost:${port}`;

export default defineConfig({
	testDir: './e2e',
	timeout: 30_000,
	fullyParallel: true,
	workers: process.env.CI ? 2 : 4,
	forbidOnly: !!process.env.CI,
	retries: process.env.CI ? 1 : 0,
	reporter: process.env.CI ? 'github' : 'list',
	use: { baseURL: EDGE, trace: 'on-first-retry', ...devices['Desktop Chrome'] },
	// Headless Chromium offers no WebGPU adapter by default, which is the browser the atlas and the graph
	// must refuse plainly. `webgpu` runs the specs that draw on Chromium's software adapter (SwiftShader
	// over Vulkan, `--enable-unsafe-webgpu`), in the new headless mode (`channel: 'chromium'`), whose
	// screenshots composite a WebGPU canvas (the headless shell's show it blank, measured 2026-11-14), so a
	// spec reads what the renderer drew.
	projects: [
		{ name: 'chromium', testIgnore: /webgpu\.spec\.ts$/ },
		{
			name: 'webgpu',
			testMatch: /webgpu\.spec\.ts$/,
			use: {
				channel: 'chromium',
				launchOptions: {
					args: [
						'--enable-unsafe-webgpu',
						'--enable-features=Vulkan',
						'--use-vulkan=swiftshader',
						'--use-webgpu-adapter=swiftshader',
						'--use-angle=swiftshader',
					],
				},
			},
		},
	],
	webServer: [
		{
			command: 'bun e2e/edge.ts',
			port: EDGE_PORT,
			reuseExistingServer: !process.env.CI,
			timeout: 60_000,
			env: {
				PORT: String(EDGE_PORT),
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
