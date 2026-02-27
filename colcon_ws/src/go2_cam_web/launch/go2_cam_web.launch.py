"""go2_cam_web.launch.py — ROS 2 launch file for go2_cam_web.

Uses ExecuteProcess instead of Node because the executable avoids rclpy
to prevent a CycloneDDS domain conflict with the Unitree SDK.
Arguments are forwarded as plain CLI flags.
"""

import os

from ament_index_python.packages import get_package_prefix
from launch import LaunchDescription
from launch.actions import DeclareLaunchArgument, ExecuteProcess
from launch.substitutions import LaunchConfiguration


def generate_launch_description() -> LaunchDescription:
    # Resolve the installed executable path so it works regardless of PATH
    _exe = os.path.join(
        get_package_prefix('go2_cam_web'), 'lib', 'go2_cam_web', 'go2_cam_web'
    )

    return LaunchDescription([
        # ---------------------------------------------------------------
        # Launch arguments
        # ---------------------------------------------------------------
        DeclareLaunchArgument(
            'network_interface',
            default_value='',
            description=(
                'Network interface connected to the Go2 robot (e.g. eth0). '
                'Leave empty to use the SDK default.'
            ),
        ),
        DeclareLaunchArgument(
            'host',
            default_value='0.0.0.0',
            description='Host address the web server will bind to.',
        ),
        DeclareLaunchArgument(
            'port',
            default_value='8080',
            description='TCP port the web server will listen on.',
        ),
        DeclareLaunchArgument(
            'fps',
            default_value='30',
            description='Target capture rate in frames per second.',
        ),
        DeclareLaunchArgument(
            'jpeg_quality',
            default_value='80',
            description='JPEG encoding quality (1–100).',
        ),

        # ---------------------------------------------------------------
        # Process (not a ROS node — avoids CycloneDDS domain conflict)
        # ---------------------------------------------------------------
        ExecuteProcess(
            cmd=[
                _exe,
                '--network-interface', LaunchConfiguration('network_interface'),
                '--host',              LaunchConfiguration('host'),
                '--port',              LaunchConfiguration('port'),
                '--fps',               LaunchConfiguration('fps'),
                '--jpeg-quality',      LaunchConfiguration('jpeg_quality'),
            ],
            output='screen',
            emulate_tty=True,
        ),
    ])
