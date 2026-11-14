// See https://svelte.dev/docs/kit/types#app.d.ts
import type { AuthLocals } from '@workbench/api/bff';

declare global {
	namespace App {
		interface Locals extends AuthLocals {}
	}
}

export {};
