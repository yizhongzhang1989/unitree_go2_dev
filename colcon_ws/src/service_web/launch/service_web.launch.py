from launch import LaunchDescription
from launch.actions import DeclareLaunchArgument
from launch.substitutions import LaunchConfiguration
from launch_ros.actions import Node


def generate_launch_description():
    return LaunchDescription([
        DeclareLaunchArgument('host',              default_value='0.0.0.0'),
        DeclareLaunchArgument('port',              default_value='8085'),
        DeclareLaunchArgument('network_interface', default_value=''),

        Node(
            package='service_web',
            executable='service_web',
            name='service_web',
            output='screen',
            arguments=[
                '--host',              LaunchConfiguration('host'),
                '--port',              LaunchConfiguration('port'),
                '--network-interface', LaunchConfiguration('network_interface'),
            ],
        ),
    ])
