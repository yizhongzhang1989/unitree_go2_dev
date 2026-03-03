from setuptools import find_packages, setup

package_name = 'service_web'

setup(
    name=package_name,
    version='0.1.0',
    packages=find_packages(exclude=['test']),
    data_files=[
        ('share/ament_index/resource_index/packages', ['resource/' + package_name]),
        ('share/' + package_name, ['package.xml']),
        ('share/' + package_name + '/launch', ['launch/service_web.launch.py']),
    ],
    install_requires=['setuptools'],
    zip_safe=True,
    maintainer='developer',
    maintainer_email='dev@example.com',
    description='Unitree Go2 service manager web dashboard',
    license='Apache-2.0',
    entry_points={
        'console_scripts': [
            'service_web = service_web.main:main',
        ],
    },
)
