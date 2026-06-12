.DEFAULT_GOAL := help

.PHONY: help
help: ## Show this help message
	@grep -E '^[a-zA-Z_-]+:.*?## .*$$' $(MAKEFILE_LIST) | sort | awk 'BEGIN {FS = ":.*?## "}; {printf "\033[36m%-20s\033[0m %s\n", $$1, $$2}'

.PHONY: base-build
base-build: ## Build the runner base image locally
	podman build -t localhost/base:latest -f images/runner/shared/Containerfile.base images/runner/shared/

.PHONY: claude-build
claude-build: base-build ## Build the Claude Code runner image locally
	podman build -t localhost/claude-runner:latest -f images/runner/claude-code/Containerfile images/runner/

.PHONY: opencode-build
opencode-build: base-build ## Build the OpenCode runner image locally
	podman build -t localhost/opencode-runner:latest -f images/runner/opencode/Containerfile images/runner/

.PHONY: ci-build
ci-build: ## Build the CI podman image locally
	podman build -t ci-podman:latest -f images/ci/Containerfile.podman .

.PHONY: openshell-base-build
openshell-base-build: ## Build the OpenShell sandbox base image locally
	podman build -t localhost/openshell-base:latest -f images/runner/shared/Containerfile.openshell-base images/runner/shared/

.PHONY: openshell-claude-build
openshell-claude-build: openshell-base-build ## Build the OpenShell Claude sandbox image locally
	podman build -t localhost/claude-sandbox:latest -f images/runner/claude-code/Containerfile.openshell images/runner/

.PHONY: openshell-opencode-build
openshell-opencode-build: openshell-base-build ## Build the OpenShell OpenCode sandbox image locally
	podman build -t localhost/opencode-sandbox:latest -f images/runner/opencode/Containerfile.openshell images/runner/

.PHONY: openshell-ci-build
openshell-ci-build: ## Build the OpenShell CI image locally
	podman build -t openshell:latest -f images/ci/Containerfile.openshell .

.PHONY: bump-versions
bump-versions: ## Bump pinned dependency versions in Containerfiles
	python3 scripts/bump-versions.py

.PHONY: check-versions
check-versions: ## Check for available dependency updates
	python3 scripts/bump-versions.py --check

.PHONY: image-lint
image-lint: ## Run linting checks on image scripts
	shellcheck --severity=warning images/runner/shared/*.sh tests/images/*.sh tests/e2e/*.sh
	@uv run --with ruff ruff check --select=E,F,W images/runner/claude-code/*.py images/runner/opencode/*.py scripts/bump-versions.py

.PHONY: image-test
image-test: ## Run image unit tests
	bash tests/images/test_entrypoint.sh
	bash tests/images/test_install_plugin.sh
	bash tests/images/test_install_skills.sh

.PHONY: e2e-claude
e2e-claude: ## Run Claude Code runner e2e tests
	bash tests/e2e/e2e-claude-runner.sh

.PHONY: e2e-opencode
e2e-opencode: ## Run OpenCode runner e2e tests
	bash tests/e2e/e2e-opencode-runner.sh

.PHONY: e2e-openshell
e2e-openshell: ## Run OpenShell sandbox e2e tests
	bash tests/e2e/e2e-openshell-sandbox.sh
