/** Who the reader is to the workbench: the annotator's `GET /me`, the navbar's one source of identity.
 *  It is the workbench's own answer from its own store; no person's token goes to the lakehouse for it. */
import * as v from 'valibot';
import { readJSON, type ApiResult } from './client.ts';
import { serviceUrl } from './services.ts';

export const MeSchema = v.object({
	sub: v.string(),
	name: v.nullish(v.string()),
	email: v.nullish(v.string()),
	estate_admin: v.boolean(),
	projects: v.array(v.object({ project: v.string(), role: v.picklist(['admin', 'member']) })),
});
export type Me = v.InferOutput<typeof MeSchema>;

export function fetchMe(): Promise<ApiResult<Me>> {
	return readJSON(serviceUrl('annotator', '/me'), MeSchema, 'the annotator');
}
