// The workbench's Dagger module: every image and every gate runs through it, locally and in CI alike.
//
//	dagger call image --name=<stem>     build .docker/<stem>.dockerfile from the repository root
//	dagger call lint                    ruff (check and format) and ty over the Python workspace
//	dagger call test                    the offline Python suite (-m "not e2e")
//	dagger call frontend-check          the frontend workspace's check, lint, fmt:check, vitest and knip
//	dagger call app-end-to-end          the app's Playwright suite on the built app, behind its edge and mocks
//	dagger call fga-test                the authorization model: valid, its own tests green, model.json its compiled form
package main

import (
	"context"
	"encoding/json"
	"fmt"
	"reflect"
	"strings"

	"dagger/workbench/internal/dagger"
)

type Workbench struct{}

const (
	// The uv image the dockerfiles' builder stages use, pinned by digest.
	UvImage = "ghcr.io/astral-sh/uv:python3.13-trixie-slim@sha256:7820aa74c8a3147ab13553c127432656969548971fbe350ba46a975b59dd42b2"
	// The Rust the images build no-wheel packages with (.docker/media.dockerfile's RUST_TOOLCHAIN).
	RustToolchain = "1.90.0"
	// The bun the frontend workspace's lock was written by.
	BunImage = "oven/bun:1.4.2"
	// The browsers the app's Playwright suite runs in: Microsoft's image for the @playwright/test the lock resolves
	// (1.65.1), so the browser revision is the one the runner expects. Pinned by digest.
	PlaywrightImage = "mcr.microsoft.com/playwright:v1.65.1-noble@sha256:1507cbfc550a63a6b7396791ca4f00df17beb56342d2447b3f106c306f525579"
	// The OpenFGA server the offline suite asserts authorization outcomes against (tests/openfga.py), and its CLI for the
	// model's own tests. `make openfga` exports the server binary from the same image for a developer box.
	OpenfgaImage = "openfga/openfga:v1.18.3"
	FgaCliImage  = "openfga/cli:v0.6.4"
	// The PostgreSQL the annotator's state runs on in the offline suite (tests/postgres.py) and the local stack: its
	// server tree, assembled in the image by scripts/postgres-server.sh. `make postgres` exports the same tree for a
	// developer box. The major matches the CloudNativePG Cluster the workbench chart (`chart/`) renders.
	PostgresImage = "postgres:17.6-bookworm"
	// Where the workbench's authorization model lives in the source tree.
	authModelDir = "packages/service-kit/src/service_kit/governed/auth"
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
		// The toolchain .docker/media.dockerfile builds with, for the packages with no wheel for the engine's platform:
		// lance-graph 0.5.4 publishes none for aarch64, so uv builds it from Rust (pinned as the image pins it).
		WithMountedCache("/var/cache/apt", dag.CacheVolume("workbench-apt")).
		WithExec([]string{"apt-get", "update"}).
		WithExec([]string{"apt-get", "install", "-y", "--no-install-recommends", "ca-certificates", "curl", "gcc", "g++",
			"libc6-dev", "make", "cmake", "pkg-config", "protobuf-compiler", "libprotobuf-dev", "libssl-dev"}).
		WithEnvVariable("RUSTUP_HOME", "/opt/rustup").
		WithEnvVariable("CARGO_HOME", "/opt/cargo").
		WithEnvVariable("PATH", "/opt/cargo/bin:$PATH", dagger.ContainerWithEnvVariableOpts{Expand: true}).
		WithExec([]string{"sh", "-c", "curl --proto '=https' --tlsv1.2 -sSf https://sh.rustup.rs | sh -s -- -y --profile minimal --default-toolchain " + RustToolchain}).
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
	// +ignore=["**/.venv", ".git", "**/node_modules", "**/.svelte-kit", "**/__pycache__", "**/.pytest_cache", ".localbin"]
	// +defaultPath="/"
	src *dagger.Directory,
	// +optional
	// +default="8"
	workers string,
) (string, error) {
	// PostgreSQL refuses to run as root, so the suite runs as a user of its own, on the synced environment.
	return m.python(src).
		WithFile("/usr/local/bin/openfga", dag.Container().From(OpenfgaImage).File("/openfga")).
		WithDirectory("/opt/postgres", m.postgresServer(src.File("scripts/postgres-server.sh"))).
		WithEnvVariable("WORKBENCH_POSTGRES_DIR", "/opt/postgres").
		WithExec([]string{"useradd", "--create-home", "tester"}).
		WithExec([]string{"chown", "-R", "tester", "/src"}).
		WithUser("tester").
		WithEnvVariable("PYTHONDONTWRITEBYTECODE", "1").
		WithExec([]string{"/opt/venv/bin/python", "-m", "pytest", "-m", "not e2e", "-n", workers, "--dist", "loadgroup", "-q",
			"-p", "no:cacheprovider", "--timeout=300", "--timeout-method=thread"}).
		Stdout(ctx)
}

// postgresServer is the relocatable PostgreSQL server tree the pinned image yields (scripts/postgres-server.sh).
func (m *Workbench) postgresServer(script *dagger.File) *dagger.Directory {
	return dag.Container().From(PostgresImage).
		WithFile("/postgres-server.sh", script).
		WithExec([]string{"sh", "/postgres-server.sh"}).
		Directory("/out")
}

// FrontendCheck runs the frontend workspace's check, lint, fmt:check, vitest (every member's, the app's included) and
// knip over the whole JS graph.
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
		WithExec([]string{"bun", "run", "test"}).
		WithExec([]string{"bun", "run", "knip"}).
		Stdout(ctx)
}

// AppEndToEnd runs the app's Playwright suite (frontend/app/e2e) on the BUILT app, behind `edge.ts` with its mock issuer
// and services, in Microsoft's Playwright image with the workspace's bun beside it.
func (m *Workbench) AppEndToEnd(
	ctx context.Context,
	// +ignore=["**/node_modules", "**/.svelte-kit", "**/build", "**/test-results", ".git", "**/.venv"]
	// +defaultPath="/"
	src *dagger.Directory,
) (string, error) {
	return dag.Container().From(PlaywrightImage).
		WithFile("/usr/local/bin/bun", dag.Container().From(BunImage).File("/usr/local/bin/bun")).
		WithMountedCache("/root/.bun/install/cache", dag.CacheVolume("workbench-bun")).
		WithDirectory("/src", src).
		WithWorkdir("/src/frontend").
		WithExec([]string{"bun", "install", "--frozen-lockfile"}).
		WithEnvVariable("CI", "1").
		WithExec([]string{"bun", "--cwd=app", "run", "test:e2e"}).
		Stdout(ctx)
}

// FgaTest validates the authorization model, runs its own tests (model.fga.yaml), and refuses a model.json that is not
// the model's compiled form, so the model the services load is the one the tests judged.
func (m *Workbench) FgaTest(
	ctx context.Context,
	// +ignore=["**", "!packages/service-kit/src/service_kit/governed/auth/**"]
	// +defaultPath="/"
	src *dagger.Directory,
) (string, error) {
	cli := dag.Container().From(FgaCliImage).
		WithDirectory("/auth", src.Directory(authModelDir)).
		WithWorkdir("/auth")
	validated, err := cli.WithExec([]string{"/fga", "model", "validate", "--file", "model.fga"}).Stdout(ctx)
	if err != nil {
		return "", err
	}
	var validity struct {
		IsValid bool `json:"is_valid"`
	}
	if err := json.Unmarshal([]byte(validated), &validity); err != nil || !validity.IsValid {
		return "", fmt.Errorf("model.fga is not valid: %s", validated)
	}
	tested, err := cli.WithExec([]string{"/fga", "model", "test", "--tests", "model.fga.yaml"}).Stdout(ctx)
	if err != nil {
		return "", err
	}
	compiled, err := cli.WithExec([]string{"/fga", "model", "transform", "--file", "model.fga"}).Stdout(ctx)
	if err != nil {
		return "", err
	}
	carried, err := src.File(authModelDir + "/model.json").Contents(ctx)
	if err != nil {
		return "", err
	}
	var want, got any
	if err := json.Unmarshal([]byte(compiled), &want); err != nil {
		return "", err
	}
	if err := json.Unmarshal([]byte(carried), &got); err != nil {
		return "", err
	}
	if !reflect.DeepEqual(want, got) {
		return "", fmt.Errorf("model.json is not model.fga's compiled form: run `make fga-model`")
	}
	return tested, nil
}
