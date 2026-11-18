// Adapted from Baby UI's `tabs` (Apache-2.0; see THIRD_PARTY.md in this package).
import { createContext } from 'svelte';
import type { TabsOrientation, TabsSize, TabsVariant } from './variants.js';

export type TabsContext = {
	readonly value: string;
	readonly variant: TabsVariant;
	readonly size: TabsSize;
	readonly orientation: TabsOrientation;
};

export const [getTabs, setTabs] = createContext<TabsContext>();
