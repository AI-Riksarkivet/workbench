import { DropdownMenu as BitsDropdownMenu } from 'bits-ui';
import Content from './dropdown-menu-content.svelte';
import Item from './dropdown-menu-item.svelte';
import Label from './dropdown-menu-label.svelte';
import Separator from './dropdown-menu-separator.svelte';
import RadioItem from './dropdown-menu-radio-item.svelte';

// Annotate with `typeof` so the emitted .d.ts can name these types (otherwise
// svelte-package's declaration emit silently skips index.d.ts and publint fails).
const Root: typeof BitsDropdownMenu.Root = BitsDropdownMenu.Root;
const Trigger: typeof BitsDropdownMenu.Trigger = BitsDropdownMenu.Trigger;
const Group: typeof BitsDropdownMenu.Group = BitsDropdownMenu.Group;
const RadioGroup: typeof BitsDropdownMenu.RadioGroup = BitsDropdownMenu.RadioGroup;

export {
	Root,
	Trigger,
	Group,
	Content,
	Item,
	Label,
	Separator,
	RadioGroup,
	RadioItem,
	//
	Root as DropdownMenu,
	Trigger as DropdownMenuTrigger,
	Group as DropdownMenuGroup,
	Content as DropdownMenuContent,
	Item as DropdownMenuItem,
	Label as DropdownMenuLabel,
	Separator as DropdownMenuSeparator,
	RadioGroup as DropdownMenuRadioGroup,
	RadioItem as DropdownMenuRadioItem,
};
