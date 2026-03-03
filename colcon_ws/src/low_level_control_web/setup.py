from setuptools import find_packages, setup
import os
from glob import glob

package_name = 'low_level_control_web'

setup(
    name=package_name,
    version='0.1.0',
    packages=find_packages(exclude=['test']),
    package_data={
        package_name: ['static/*.html'],
    },
    data_files=[
        ('share/ament_index/resource_index/packages', ['resource/' + package_name]),
        ('share/' + package_name, ['package.xml']),
        (os.path.join('share', package_name, 'launch'), glob('launch/*.py')),
    ],
    install_requires=[
        'setuptools',
        'fastapi',
        'uvicorn[standard]',
    ],
    zip_safe=True,
    maintainer='developer',
    maintainer_email='dev@example.com',
    description='Unitree Go2 real-time low-level motor status web monitor',
    license='Apache-2.0',
    tests_require=['pytest'],
    entry_points={
        'console_scripts': [
            'low_level_control_web = low_level_control_web.main:main',
        ],
    },
)
