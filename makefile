clean:
	rm -rf dist build *.egg-info

build: clean
	python -m build

publish: build
	python -m twine check dist/*
	python -m twine upload --repository pypi dist/* --verbose
