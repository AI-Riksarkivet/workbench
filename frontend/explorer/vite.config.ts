import { sveltekit } from '@sveltejs/kit/vite';
import tailwindcss from '@tailwindcss/vite';
import { defineConfig } from 'vite';

export default defineConfig({
	plugins: [tailwindcss(), sveltekit()],
	server: {
		// Bind the port declared in microfrontends/home/microfrontends.json — the composition proxy
		// routes by it. strictPort fails loudly on a clash instead of silently drifting to the next free
		// port. This zone's port used to live only in its `dev` script and was absent from the routing
		// config, so the proxy had no way to reach it.
		port: 5173,
		strictPort: true,
		// No /api dev proxy anymore: the zone's own BFF routes (src/routes/api/**) serve
		// `${base}/api/*` in dev and prod alike, defaulting to the three local lance-media
		// services (VIEWER_API :8101 · SEARCH_API :8102 · ANNOTATOR_API :8103).
		proxy: {
			// Zone composition: the annotator app owns /annotator (micro-frontends
			// routing-based zones) — one origin, path-routed to the sibling app.
			'/annotator': { target: 'http://127.0.0.1:5177', changeOrigin: true, ws: true },
		},
	},
});
