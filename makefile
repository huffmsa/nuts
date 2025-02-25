clean:
	rm -r dist

build:
	python -m build

publish:
	python -m twine upload --repository pypi dist/* --verbose
