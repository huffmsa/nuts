from setuptools import setup, find_packages


packages = find_packages(exclude=['tests'])

print('packages')
print(packages)

setup(
    name='carte_blanche_path',

    version='1.1.0',

    url='https://github.com/huffmsa/carte-blanche-path',

    license='MIT',

    author='Sam Huffman',

    author_email='huffmsa@gmail.com',

    description='A utility for setting project root directory',

    packages=find_packages(exclude=['tests']),

    long_description=open('README.md').read(),

    zip_safe=False,

    setup_requires=[
        "pytest==8.0.2"

    ],

    install_requires=[],

    test_suite=''
)
