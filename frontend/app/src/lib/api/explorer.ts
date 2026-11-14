/**
 * The explorer's client (`@workbench/explorer-api`) on this app's transport: every call it makes goes
 * through the one DPoP fetch seam with a read's timeout (`timedFetch`), same-origin, to the public base
 * of the service that owns the path (`upstreamFor`, the client's own routing table).
 */
import { setTransport, upstreamFor } from '@workbench/explorer-api';
import { timedFetch } from './client.ts';
import { serviceUrl } from './services.ts';
import type { ServiceName } from '../config.ts';

/** The service that answers `path`: search's door, the annotator's, or the viewer, which owns the rest. */
function serviceOf(path: string): ServiceName {
	const upstream = upstreamFor(path);
	if (upstream === 'search') return 'search';
	if (upstream === 'annotator' || upstream === 'assist') return 'annotator';
	return 'viewer';
}

const transport = { fetch: timedFetch, url: (path: string) => serviceUrl(serviceOf(path), path) };

/** Install the transport; idempotent, run once the services are known. */
export function installExplorerTransport(): void {
	setTransport(transport);
}
