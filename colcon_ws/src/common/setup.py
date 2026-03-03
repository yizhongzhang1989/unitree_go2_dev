from setuptools import find_packages, setup

package_name = 'common'

setup(
    name=package_name,
    version='0.1.0',
    packages=find_packages(exclude=['test']),
    data_files=[
        ('share/ament_index/resource_index/packages', ['resource/' + package_name]),
        ('share/' + package_name, ['package.xml']),
    ],
    install_requires=['setuptools'],
    zip_safe=True,
    maintainer='developer',
    maintainer_email='dev@example.com',
    description='Shared utilities for Unitree Go2 ROS2 web packages',
    license='Apache-2.0',
    entry_points={
        'console_scripts': [],
    },
)
