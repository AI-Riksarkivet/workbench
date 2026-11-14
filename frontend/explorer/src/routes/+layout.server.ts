import { env } from '$env/dynamic/private';
import type { LayoutServerLoad } from './$types';
import { makeZoneLayoutLoad } from '@workbench/api/bff';

// The estate-wide zone layout contract — identity, auth flag, active project AND the resolved
// `me` — owned by @workbench/api so every zone renders one shell by construction (#107).
export const load: LayoutServerLoad = makeZoneLayoutLoad(env);
