"""low_level_joint_control_web.launch.py"""

import os

from ament_index_python.packages import get_package_prefix
from launch import LaunchDescription
from launch.actions import DeclareLaunchArgument, ExecuteProcess
from launch.substitutions import LaunchConfiguration


def generate_launch_description() -> LaunchDescription:
    _exe = os.path.join(
        get_package_prefix('low_level_joint_control_web'),
        'lib', 'low_level_joint_control_web', 'low_level_joint_control_web',
    )

    return LaunchDescription([
        DeclareLaunchArgument('host', default_value='0.0.0.0',
                              description='Web server bind address.'),
        DeclareLaunchArgument('port', default_value='8084',
                              description='Web server port.'),
        DeclareLaunchArgument('network_interface', default_value='',
                              description='Unitree SDK network interface (optional).'),
        ExecuteProcess(
            cmd=[
                _exe,
                '--host', LaunchConfiguration('host'),
                '--port', LaunchConfiguration('port'),
                '--network-interface', LaunchConfiguration('network_interface'),
            ],
            output='screen',
            emulate_tty=True,
        ),
    ])
