// The two shapes of OIDC provider the suite signs in at, one place. The mock issuer serves both (`mock-oidc.ts`), the
// mock services hold each token to its deployment's rule (`mock-services.ts`), and the mock lakehouse reaches the
// lakehouse the way each allows (`mock-lakehouse.ts`).
//
// - `keycloak`, under `/oidc`: it binds tokens with DPoP and exchanges its own tokens for another audience (RFC 8693),
//   as Keycloak 26.4+ does. The person's token names the workbench's audience alone, and the services exchange it.
// - `dex`, under `/oidc-dex`: as the dev Dex v2.46.1 is (its discovery read 2026-11-16; rask's ADR 0181): no DPoP and no
//   end-session endpoint in discovery, the token-exchange grant advertised but refused for one of its own tokens, and
//   a token's audiences the clients its `audience:server:client_id:<id>` scopes name, beside the client itself. The
//   person's token names the lakehouse's audience too, and the services pass it on.

export type Shape = 'keycloak' | 'dex';

/** The workbench's own public client. */
export const APP_CLIENT = 'workbench-app';
/** The workbench's confidential client, which its services exchange and take their own token with. */
export const SERVICE_CLIENT = 'workbench-services';
export const SERVICE_SECRET = 'workbench-services-e2e-secret';
/** The lakehouse's audience. */
export const LAKEHOUSE_AUDIENCE = 'rask-lakehouse';
/** Dex's cross-client scope for an audience. */
export const audienceScope = (audience: string): string => `audience:server:client_id:${audience}`;

export const SHAPES = {
	keycloak: {
		prefix: '/oidc',
		/** The audience the services accept: the workbench's own, which Keycloak's audience mapper puts in its token. */
		workbenchAudience: 'workbench',
		dpopRequired: true,
	},
	dex: {
		prefix: '/oidc-dex',
		/** Under Dex the workbench's tokens name its sign-in client, the one audience no other client's token names. */
		workbenchAudience: APP_CLIENT,
		dpopRequired: false,
	},
} as const satisfies Record<
	Shape,
	{ prefix: string; workbenchAudience: string; dpopRequired: boolean }
>;

/** The shape whose issuer `iss` is. */
export const shapeOf = (iss: string): Shape =>
	iss.endsWith(SHAPES.dex.prefix) ? 'dex' : 'keycloak';
