"""lowcmd_recording.launch.py — Launch the LowCmd/LowState DDS recorder.

Usage:
    ros2 launch lowcmd_recording lowcmd_recording.launch.py port:=8085
"""

import os

from ament_index_python.packages import get_package_prefix
from launch import LaunchDescription
from launch.actions import DeclareLaunchArgument, ExecuteProcess
from launch.substitutions import LaunchConfiguration


def generate_launch_description() -> LaunchDescription:
    _exe = os.path.join(
        get_package_prefix('lowcmd_recording'),
        'lib', 'lowcmd_recording', 'lowcmd_recording',
    )

    return LaunchDescription([
        DeclareLaunchArgument(
            'host', default_value='0.0.0.0',
            description='Web server bind address.',
        ),
        DeclareLaunchArgument(
            'port', default_value='8085',
            description='Web server port.',
        ),
        DeclareLaunchArgument(
            'network_interface', default_value='',
            description='Network interface for DDS (e.g. eth0). Leave empty for auto-detect.',
        ),

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
