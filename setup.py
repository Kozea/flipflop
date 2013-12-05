# Bootstrap setuptools

from distutils.core import setup

setup(
    name='flipflop',
    version='1.0',
    py_modules=['flipflop'],
    provides=['flipflop'],
    author='Guillaume Ayoub',
    author_email='guillaume.ayoub@kozea.fr',
    description='FastCGI wrapper for WSGI applications',
    url='https://github.com/Kozea/flipflop',
    license='BSD',
    classifiers=[
        "Development Status :: 4 - Beta",
        'Environment :: Web Environment',
        'Intended Audience :: Developers',
        'License :: OSI Approved :: BSD License',
        'Operating System :: POSIX :: Linux',
        'Programming Language :: Python :: 3',
        'Programming Language :: Python :: 3.3',
        'Programming Language :: Python',
        'Topic :: Internet :: WWW/HTTP :: WSGI :: Server',
        'Topic :: Software Development :: Libraries :: Python Modules',
    ],
)
