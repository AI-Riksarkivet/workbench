import { beforeEach, describe, expect, it } from 'vitest';
import { apiFetch, apiUrl, resetApiBase, setApiBase, setTransport } from './base';

beforeEach(() => resetApiBase());

describe('apiUrl carries the zone base', () => {
	it('is root-absolute before a zone sets one', () => {
		expect(apiUrl('/api/health')).toBe('/api/health');
	});

	it('prefixes the zone base', () => {
		setApiBase('/explorer');
		expect(apiUrl('/api/health')).toBe('/explorer/api/health');
	});

	it('normalizes a trailing slash rather than doubling it', () => {
		setApiBase('/annotator/');
		expect(apiUrl('/api/jobs')).toBe('/annotator/api/jobs');
	});
});

describe('the base is a per-PROCESS constant, not per-request state', () => {
	// A zone is a process: media and annotator are separate images, pods and dev servers, so this
	// module only ever sees one base. If that ever stopped being true — two zones composed into one
	// SSR process — every media-plane URL would silently point at the other zone's proxy routes.
	it('refuses to re-base to a different zone', () => {
		setApiBase('/explorer');
		expect(() => setApiBase('/annotator')).toThrow(/already set to "\/explorer"/);
	});

	it('is idempotent for the same base', () => {
		setApiBase('/explorer');
		expect(() => setApiBase('/explorer')).not.toThrow();
		expect(() => setApiBase('/explorer/')).not.toThrow();
		expect(apiUrl('/api/health')).toBe('/explorer/api/health');
	});

	it('a later empty base is a no-op, so an unbased consumer cannot clear a zone', () => {
		setApiBase('/explorer');
		setApiBase('');
		expect(apiUrl('/api/health')).toBe('/explorer/api/health');
	});
});

describe('a transport carries every call instead of a base', () => {
	it('puts each path on the URL the transport maps it to, through its own fetch', async () => {
		const seen: string[] = [];
		setTransport({
			url: (path) => `/svc${path}`,
			fetch: async (input) => {
				seen.push(input);
				return new Response('{}');
			},
		});
		expect(apiUrl('/api/health')).toBe('/svc/api/health');
		await apiFetch(apiUrl('/api/health'));
		expect(seen).toEqual(['/svc/api/health']);
	});

	it('is exclusive with a base, either way round', () => {
		setTransport({ url: (path) => path, fetch: async () => new Response() });
		expect(() => setApiBase('/explorer')).toThrow(/transport is installed/);
		resetApiBase();
		setApiBase('/explorer');
		expect(() => setTransport({ url: (path) => path, fetch: async () => new Response() })).toThrow(
			/is set, so a transport is refused/,
		);
	});
});
