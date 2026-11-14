/**
 * Where each service is, as `/config.json` named it: its public base on this origin. A service answers
 * its own paths below that base (the Gateway rewrites the base to the service's root), so a path a
 * service hands back (a signed media link, `/api/signed-media/<token>`) is joined to the same base.
 */
import type { ServiceName, Services } from '../config.ts';

let services: Services | null = null;

/** Set once, from `/config.json`, before any read runs. */
export function setServices(next: Services): void {
	services = next;
}

/** `path` (the service's own, with a leading slash) on `name`'s public base. */
export function serviceUrl(name: ServiceName, path: string): string {
	if (services === null) throw new Error('the services are read from /config.json before any call');
	return `${services[name]}${path}`;
}
