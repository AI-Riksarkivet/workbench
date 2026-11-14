// The workbench's Dagger module: every image and every gate runs through it, locally and in CI alike.
//
//	dagger call image --name=<stem>     build .docker/<stem>.dockerfile from the repository root
//	dagger call lint                    ruff (check and format) and ty over the Python workspace
//	dagger call test                    the offline Python suite (-m "not e2e")
//	dagger call frontend-check          the frontend workspace's check, lint and fmt:check
package main

import (
	"context"
	"fmt"
	"strings"

	"dagger/workbench/internal/dagger"
)

type Workbench struct{}

const (
	// The uv image the dockerfiles' builder stages use, pinned by digest.
	UvImage = "ghcr.io/astral-sh/uv:python3.13-trixie-slim@sha256:7820aa74c8a3147ab13553c127432656969548971fbe350ba46a975b59dd42b2"
	// The bun the frontend workspace's lock was written by.
	BunImage = "oven/bun:1.4.2"
)

// Image builds .docker/<name>.dockerfile with the repository root as its context.
func (m *Workbench) Image(
	// +ignore=["**/.venv", ".git", "**/node_modules", "**/.svelte-kit", "**/e2e", "**/test-results",
	//          "**/build", "**/.pytest_cache", "**/__pycache__", "**/.ruff_cache"]
	// +defaultPath="/"
	src *dagger.Directory,
	// Dockerfile stem under .docker/.
	name string,
	// Extra build args as KEY=VALUE.
	// +optional
	buildArg []string,
	// The platform to build for; the engine's own by default.
	// +optional
	platform dagger.Platform,
) *dagger.Container {
	var args []dagger.BuildArg
	for _, kv := range buildArg {
		k, v, _ := strings.Cut(kv, "=")
		args = append(args, dagger.BuildArg{Name: k, Value: v})
	}
	return src.DockerBuild(dagger.DirectoryDockerBuildOpts{
		Dockerfile: fmt.Sprintf(".docker/%s.dockerfile", name),
		BuildArgs:  args,
		Platform:   platform,
	})
}

// python is the workspace synced from the root lock, dev group included.
func (m *Workbench) python(src *dagger.Directory) *dagger.Container {
	return dag.Container().From(UvImage).
		WithMountedCache("/root/.cache/uv", dag.CacheVolume("workbench-uv")).
		WithEnvVariable("UV_LINK_MODE", "copy").
		WithEnvVariable("UV_PROJECT_ENVIRONMENT", "/opt/venv").
		WithDirectory("/src", src).
		WithWorkdir("/src").
		WithExec([]string{"uv", "sync", "--frozen", "--all-packages"})
}

// Lint runs ruff (check and format --check) and ty over the Python workspace.
func (m *Workbench) Lint(
	ctx context.Context,
	// +ignore=["**/.venv", ".git", "**/node_modules", "**/.svelte-kit", "**/__pycache__", "**/.ruff_cache"]
	// +defaultPath="/"
	src *dagger.Directory,
) (string, error) {
	return m.python(src).
		WithExec([]string{"uv", "run", "--frozen", "ruff", "check", "."}).
		WithExec([]string{"uv", "run", "--frozen", "ruff", "format", "--check", "."}).
		WithExec([]string{"uv", "run", "--frozen", "ty", "check"}).
		Stdout(ctx)
}

// Test runs the offline Python suite: every member's tests/, live suites deselected.
func (m *Workbench) Test(
	ctx context.Context,
	// +ignore=["**/.venv", ".git", "**/node_modules", "**/.svelte-kit", "**/__pycache__", "**/.pytest_cache"]
	// +defaultPath="/"
	src *dagger.Directory,
	// +optional
	// +default="8"
	workers string,
) (string, error) {
	return m.python(src).
		WithExec([]string{"uv", "run", "--frozen", "pytest", "-m", "not e2e", "-n", workers, "-q",
			"--timeout=300", "--timeout-method=thread"}).
		Stdout(ctx)
}

// FrontendCheck runs the frontend workspace's check, lint and fmt:check.
func (m *Workbench) FrontendCheck(
	ctx context.Context,
	// +ignore=["**/node_modules", "**/.svelte-kit", "**/build", "**/test-results", ".git", "**/.venv"]
	// +defaultPath="/"
	src *dagger.Directory,
) (string, error) {
	return dag.Container().From(BunImage).
		WithMountedCache("/root/.bun/install/cache", dag.CacheVolume("workbench-bun")).
		WithDirectory("/src", src).
		WithWorkdir("/src/frontend").
		WithExec([]string{"bun", "install", "--frozen-lockfile"}).
		WithExec([]string{"bun", "run", "check"}).
		WithExec([]string{"bun", "run", "lint"}).
		WithExec([]string{"bun", "run", "fmt:check"}).
		Stdout(ctx)
}
