import { describe, expect, it } from 'vitest';
import { readConfig } from './config.ts';

const services = { viewer: '/api/viewer', search: '/api/search', annotator: '/api/annotator' };

/** A `/config.json` answering `body`. */
const serving =
	(body: unknown): typeof fetch =>
	async () =>
		Response.json(body);

const signIn = (issuer: string): unknown => ({
	issuer,
	clientId: 'workbench-app',
	scopes: ['openid'],
	services,
});

describe('the issuer /config.json names', () => {
	it.each([
		['an https issuer', 'https://id.example/realms/w', 'https://wb.example'],
		[
			'an http issuer on a loopback host',
			'http://localhost:8080/realms/w',
			'http://localhost:5173',
		],
		['a same-origin path on an https page', '/oidc', 'https://wb.example'],
	])('is accepted: %s', async (_case, issuer, origin) => {
		expect(await readConfig(serving(signIn(issuer)), origin)).toMatchObject({ off: false });
	});

	it.each([
		['an http issuer on another host', 'http://id.example/realms/w', 'https://wb.example'],
		['a same-origin path on an http page that is not loopback', '/oidc', 'http://wb.example'],
	])('is refused: %s', async (_case, issuer, origin) => {
		expect(await readConfig(serving(signIn(issuer)), origin)).toEqual({
			error: expect.stringMatching(/not https/),
		});
	});
});
