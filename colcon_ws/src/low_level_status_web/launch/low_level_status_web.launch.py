"""low_level_status_web.launch.py — ROS 2 launch file for low_level_status_web.

Subscribes to /lowstate via rclpy (no Unitree SDK DDS subprocess needed).

Usage:
    ros2 launch low_level_status_web low_level_status_web.launch.py port:=8082
"""

import os

from ament_index_python.packages import get_package_prefix
from launch import LaunchDescription
from launch.actions import DeclareLaunchArgument, ExecuteProcess
from launch.substitutions import LaunchConfiguration


def generate_launch_description() -> LaunchDescription:
    _exe = os.path.join(
        get_package_prefix('low_level_status_web'),
        'lib', 'low_level_status_web', 'low_level_status_web',
    )

    return LaunchDescription([
        DeclareLaunchArgument(
            'host',
            default_value='0.0.0.0',
            description='Host address the web server will bind to.',
        ),
        DeclareLaunchArgument(
            'port',
            default_value='8082',
            description='TCP port the web server will listen on.',
        ),

        ExecuteProcess(
            cmd=[
                _exe,
                '--host', LaunchConfiguration('host'),
                '--port', LaunchConfiguration('port'),
            ],
            output='screen',
            emulate_tty=True,
        ),
    ])
