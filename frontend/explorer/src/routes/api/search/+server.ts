import { env } from '$env/dynamic/private';
import { KEEP_API_PREFIX, makeBackendProxy } from '@workbench/api/bff';

// Same-origin proxy to the SEARCH service. GET is the text search; POST is the same query surface with
// an image attached (multipart query-by-example), and like every non-GET here it fails closed without a
// signed-in user on an auth-enabled stack. The service answers its hits as one Arrow IPC stream of rows
// (`service_kit.arrow_rows`), which this forwards as it came.
const SEARCH_API = env.SEARCH_API ?? 'http://localhost:8102';

export const GET = makeBackendProxy({ backendUrl: SEARCH_API, stripPrefix: KEEP_API_PREFIX });

export const POST = makeBackendProxy({
	backendUrl: SEARCH_API,
	stripPrefix: KEEP_API_PREFIX,
	requireSession: true,
});
