from setuptools import setup, find_packages


packages = find_packages(exclude=['tests'])

print('packages')
print(packages)

setup(
    name='nuts',

    version='0.1.0',

    url='https://github.com/huffmsa/nuts',

    license='MIT',

    author='Sam Huffman',

    author_email='huffmsa@gmail.com',

    description='A redis backed job scheduling and execution framework',

    packages=find_packages(exclude=['tests']),

    long_description=open('README.md').read(),

    zip_safe=False,

    setup_requires=[
        "pytest==8.0.2"

    ],

    install_requires=[
        "python-dateutil==2.9.0.post0",
        "redis==5.2.1"
    ],

    test_suite=''
)
