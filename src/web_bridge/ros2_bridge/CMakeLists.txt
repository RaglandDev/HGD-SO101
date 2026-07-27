cmake_minimum_required(VERSION 3.8)
project(ros2_bridge)

if(CMAKE_COMPILER_IS_GNUCXX OR CMAKE_CXX_COMPILER_ID MATCHES "Clang")
  add_compile_options(-Wall -Wextra -Wpedantic)
endif()

find_package(ament_cmake REQUIRED)
find_package(rclcpp REQUIRED)
find_package(sensor_msgs REQUIRED)
find_package(std_msgs REQUIRED)

add_executable(image_bridge_node src/image_bridge_node.cpp)

ament_target_dependencies(image_bridge_node rclcpp sensor_msgs std_msgs)

# Install the executable binary so entrypoint.sh can find it at /workspace/install/...
install(TARGETS
  image_bridge_node
  DESTINATION lib/${PROJECT_NAME}
)

ament_package()
