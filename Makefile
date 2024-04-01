APP_NAME = "TIDAL-Downloader-NG"
app_path_dist = "dist"
path_asset = "tidal_dl_ng/ui"

.PHONY: install
install: ## Install the poetry environment and install the pre-commit hooks
	@echo "🚀 Creating virtual environment using pyenv and poetry"
	@poetry install
	@poetry run pre-commit install
	@poetry shell

.PHONY: check
check: ## Run code quality tools.
	@echo "🚀 Checking Poetry lock file consistency with 'pyproject.toml': Running poetry lock --check"
	@poetry check --lock
	@echo "🚀 Linting code: Running pre-commit"
	@poetry run pre-commit run -a
#	@echo "🚀 Static type checking: Running mypy"
#	@poetry run mypy
	@echo "🚀 Checking for obsolete dependencies: Running deptry"
	@poetry run deptry .

.PHONY: test
test: ## Test the code with pytest
	@echo "🚀 Testing code: Running pytest"
	@poetry run pytest --doctest-modules

.PHONY: build
build: clean-build ## Build wheel file using poetry
	@echo "🚀 Creating wheel file"
	@poetry build

.PHONY: clean-build
clean-build: ## clean build artifacts
	@rm -rf dist

.PHONY: publish
publish: ## publish a release to pypi.
	@echo "🚀 Publishing: Dry run."
	@poetry config pypi-token.pypi $(PYPI_TOKEN)
	@poetry publish --dry-run
	@echo "🚀 Publishing."
	@poetry publish

.PHONY: build-and-publish
build-and-publish: build publish ## Build and publish.

.PHONY: docs-test
docs-test: ## Test if documentation can be built without warnings or errors
	@poetry run mkdocs build -s

.PHONY: docs
docs: ## Build and serve the documentation
	@poetry run mkdocs serve

.PHONY: help
help:
	@grep -E '^[a-zA-Z_-]+:.*?## .*$$' $(MAKEFILE_LIST) | awk 'BEGIN {FS = ":.*?## "}; {printf "\033[36m%-20s\033[0m %s\n", $$1, $$2}'

.PHONY: gui-windows
gui-windows: ## Build GUI app with PyInstaller
	@poetry run pyinstaller --noconfirm \
		--windowed --onedir \
		--name "$(APP_NAME)" \
		--noupx \
		--icon $(path_asset)/icon.ico \
		--add-binary="pyproject.toml:." \
		--add-data="tidal_dl_ng/ui/default_album_image.png:./tidal_dl_ng/ui" \
		tidal_dl_ng/gui.py

.PHONY: gui-linux
gui-linux: ## Build GUI app with PyInstaller
	@poetry run pyinstaller --noconfirm \
		--windowed --onedir \
		--name "$(APP_NAME)" \
		--noupx \
		--icon $(path_asset)/icon.png \
		--add-binary="pyproject.toml:." \
		--add-data="tidal_dl_ng/ui/default_album_image.png:./tidal_dl_ng/ui" \

.PHONY: gui-macos
gui-macos: ## Build GUI app with PyInstaller
	@poetry run pyinstaller --noconfirm \
		--windowed --onedir \
		--name "$(APP_NAME)" \
		--noupx \
		--icon $(path_asset)/icon.icns \
		--add-binary="pyproject.toml:." \
		--add-data="tidal_dl_ng/ui/default_album_image.png:./tidal_dl_ng/ui" \
		tidal_dl_ng/gui.py

# TODO: macos Signing: https://gist.github.com/txoof/0636835d3cc65245c6288b2374799c43
.PHONY: gui-macos-dmg
gui-macos-dmg: gui-macos ## Package GUI in a *.dmg file
	@poetry run mkdir -p $(app_path_dist)/dmg
	@poetry run mv "$(app_path_dist)/$(APP_NAME).app" $(app_path_dist)/dmg
	@poetry run create-dmg \
                --volname "$(APP_NAME)" \
                --volicon "$(path_asset)/icon.icns" \
                --window-pos 200 120 \
                --window-size 800 600 \
                --icon-size 100 \
                --icon "$(APP_NAME).app" 175 120 \
                --hide-extension "$(APP_NAME).app" \
                --app-drop-link 425 120 \
                "$(app_path_dist)/$(APP_NAME).dmg" \
                "$(app_path_dist)/dmg/"

.DEFAULT_GOAL := help
