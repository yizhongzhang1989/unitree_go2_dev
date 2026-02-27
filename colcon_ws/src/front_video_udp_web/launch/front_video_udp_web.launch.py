"""front_video_udp_web.launch.py — ROS 2 launch file for front_video_udp_web.

Uses ExecuteProcess (not Node) because the executable does not use rclpy —
it is a plain Python process that reads the Go2 UDP multicast stream directly.
Arguments are forwarded as CLI flags to argparse in main.py.
"""

import os

from ament_index_python.packages import get_package_prefix
from launch import LaunchDescription
from launch.actions import DeclareLaunchArgument, ExecuteProcess
from launch.substitutions import LaunchConfiguration


def generate_launch_description() -> LaunchDescription:
    _exe = os.path.join(
        get_package_prefix('front_video_udp_web'),
        'lib', 'front_video_udp_web', 'front_video_udp_web',
    )

    return LaunchDescription([
        DeclareLaunchArgument(
            'network_interface',
            default_value='eth0',
            description=(
                'Network interface connected to the Go2 robot, e.g. '
                'enx606d3cbabf1b.  Must be set to join the UDP multicast group.'
            ),
        ),
        DeclareLaunchArgument(
            'host',
            default_value='0.0.0.0',
            description='Web server bind address.',
        ),
        DeclareLaunchArgument(
            'port',
            default_value='8081',
            description='Web server port.',
        ),
        DeclareLaunchArgument(
            'width',
            default_value='1280',
            description='Output frame width (default: 1280).',
        ),
        DeclareLaunchArgument(
            'height',
            default_value='720',
            description='Output frame height (default: 720).',
        ),
        DeclareLaunchArgument(
            'jpeg_quality',
            default_value='80',
            description='JPEG encoding quality 1-100.',
        ),

        ExecuteProcess(
            cmd=[
                _exe,
                '--network-interface', LaunchConfiguration('network_interface'),
                '--host',              LaunchConfiguration('host'),
                '--port',              LaunchConfiguration('port'),
                '--width',             LaunchConfiguration('width'),
                '--height',            LaunchConfiguration('height'),
                '--jpeg-quality',      LaunchConfiguration('jpeg_quality'),
            ],
            output='screen',
        ),
    ])
