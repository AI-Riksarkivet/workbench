<script lang="ts">
	import { page } from '$app/state';
	import { AppError } from '@workbench/ui/shell';
</script>

<AppError status={page.status} message={page.error?.message} />
