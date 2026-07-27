#include <rclcpp/rclcpp.hpp>
#include <sensor_msgs/msg/compressed_image.hpp>
#include <std_msgs/msg/string.hpp>
#include <sys/socket.h>
#include <netinet/in.h>
#include <arpa/inet.h>
#include <unistd.h>
#include <vector>
#include <thread>

class ImageBridgeNode : public rclcpp::Node {
public:
    ImageBridgeNode() : Node("web_input_bridge_node") {
        publisher_ = this->create_publisher<sensor_msgs::msg::CompressedImage>("/human/camera/compressed", 10);
        reset_pub_ = this->create_publisher<std_msgs::msg::String>("/sim/reset", 10);
        control_pub_ = this->create_publisher<std_msgs::msg::String>("/sys/control", 10);

        status_sub_ = this->create_subscription<std_msgs::msg::String>(
            "/sys/status", 10,
            [this](const std_msgs::msg::String::SharedPtr msg) { forward_status(msg->data); });

        server_fd_ = socket(AF_INET, SOCK_DGRAM, 0); // udp

        sockaddr_in address{};
        address.sin_family = AF_INET;
        address.sin_addr.s_addr = INADDR_ANY;
        address.sin_port = htons(9999); // Local IPC port

        bind(server_fd_, (struct sockaddr*)&address, sizeof(address));

        // outbound socket: forwards supervisor status JSON to the web server
        status_fd_ = socket(AF_INET, SOCK_DGRAM, 0);
        status_addr_.sin_family = AF_INET;
        status_addr_.sin_port = htons(9997);
        inet_pton(AF_INET, "127.0.0.1", &status_addr_.sin_addr);

        // control socket: UI commands (scene reset) from the web server
        control_fd_ = socket(AF_INET, SOCK_DGRAM, 0);
        sockaddr_in control_address{};
        control_address.sin_family = AF_INET;
        control_address.sin_addr.s_addr = INADDR_ANY;
        control_address.sin_port = htons(9996);
        bind(control_fd_, (struct sockaddr*)&control_address, sizeof(control_address));

        RCLCPP_INFO(this->get_logger(), "Bridge Node initialized on port :9999 (frames) / :9996 (control)");

        worker_thread_ = std::thread(&ImageBridgeNode::listen_loop, this);
        control_thread_ = std::thread(&ImageBridgeNode::control_loop, this);
    }

    ~ImageBridgeNode() {
        if (worker_thread_.joinable()) {
            worker_thread_.join();
        }
        if (control_thread_.joinable()) {
            control_thread_.join();
        }
        close(server_fd_);
        close(status_fd_);
        close(control_fd_);
    }

private:
    void forward_status(const std::string& json) {
        sendto(status_fd_, json.c_str(), json.size(), 0,
               (struct sockaddr*)&status_addr_, sizeof(status_addr_));
    }

    void control_loop() {
        std::vector<char> buffer(256);
        while (rclcpp::ok()) {
            ssize_t n = recv(control_fd_, buffer.data(), buffer.size(), 0);
            if (n > 0) {
                std_msgs::msg::String msg;
                msg.data = std::string(buffer.data(), static_cast<size_t>(n));
                // RESET goes to the sim supervisor; everything else (e.g.
                // recording control) goes on the generic control topic so it
                // never trips a scene reset.
                if (msg.data == "RESET")
                    reset_pub_->publish(msg);
                else
                    control_pub_->publish(msg);
                RCLCPP_INFO(this->get_logger(), "Control command forwarded: %s", msg.data.c_str());
            }
        }
    }

    void listen_loop() {
        constexpr uint32_t max_udp_payload_size {65507};
        std::vector<uint8_t> buffer(max_udp_payload_size);

        while (rclcpp::ok()) {
            ssize_t bytes_received = recv(server_fd_, buffer.data(), buffer.size(), 0);

            if (bytes_received > 0) {
                auto msg = sensor_msgs::msg::CompressedImage();
                msg.header.stamp = this->now();
                msg.header.frame_id = "camera_link";
                msg.format = "jpeg";

                msg.data.assign(buffer.begin(), buffer.begin() + bytes_received);

                publisher_->publish(msg);

                constexpr int interval_ms {2000};
                RCLCPP_INFO_THROTTLE(
                    this->get_logger(),
                    *this->get_clock(),
                    interval_ms,
                    "Streaming active. Receiving frames (Latest frame size: %ld bytes)",
                    bytes_received
                );
            }
        }
    }

    rclcpp::Publisher<sensor_msgs::msg::CompressedImage>::SharedPtr publisher_;
    rclcpp::Publisher<std_msgs::msg::String>::SharedPtr reset_pub_;
    rclcpp::Publisher<std_msgs::msg::String>::SharedPtr control_pub_;
    rclcpp::Subscription<std_msgs::msg::String>::SharedPtr status_sub_;
    int server_fd_;
    int status_fd_;
    int control_fd_;
    sockaddr_in status_addr_{};
    std::thread worker_thread_;
    std::thread control_thread_;
};

int main(int argc, char* argv[]) {
    rclcpp::init(argc, argv);
    auto node = std::make_shared<ImageBridgeNode>();
    rclcpp::spin(node);
    rclcpp::shutdown();
    return 0;
}
