#include <algorithm>
#include <arpa/inet.h>
#include <cmath>
#include <cv_bridge/cv_bridge.h>
#include <deque>
#include <geometry_msgs/msg/pose_stamped.hpp>
#include <netdb.h>
#include <netinet/in.h>
#include <onnxruntime_cxx_api.h>
#include <opencv2/opencv.hpp>
#include <rclcpp/rclcpp.hpp>
#include <sensor_msgs/msg/compressed_image.hpp>
#include <sstream>
#include <std_msgs/msg/string.hpp>
#include <sys/socket.h>
#include <unistd.h>
#include <vector>

class PerceptionNode : public rclcpp::Node {
public:
  PerceptionNode()
      : Node("perception_node"),
        ort_env_(ORT_LOGGING_LEVEL_WARNING, "PerceptionNode") {
    
    Ort::SessionOptions session_options;
    session_options.SetGraphOptimizationLevel(GraphOptimizationLevel::ORT_ENABLE_ALL);
    ort_session_ = std::make_unique<Ort::Session>(ort_env_, "/models/yolov8n-pose.onnx", session_options);

    gaze_pub_ = this->create_publisher<geometry_msgs::msg::PoseStamped>("/human/gaze", 10);
    gesture_pub_ = this->create_publisher<std_msgs::msg::String>("/human/gesture", 10);
    image_sub_ = this->create_subscription<sensor_msgs::msg::CompressedImage>(
        "/human/camera/compressed", 10,
        std::bind(&PerceptionNode::image_callback, this, std::placeholders::_1));

    init_udp_socket();

    // OpenCV coordinate system: X-right, Y-down, Z-away
    model_pts_ = {
        cv::Point3d(0.0, 0.0, 0.0),      // Nose
        cv::Point3d(35.0, -35.0, 25.0),  // Left Eye
        cv::Point3d(-35.0, -35.0, 25.0), // Right Eye
        cv::Point3d(85.0, -25.0, 90.0),  // Left Ear
        cv::Point3d(-85.0, -25.0, 90.0)  // Right Ear
    };

    RCLCPP_INFO(this->get_logger(), "Perception node initialized");
  }

  ~PerceptionNode() {
    if (udp_fd_ >= 0) close(udp_fd_);
  }

private:
  void init_udp_socket() {
    udp_fd_ = socket(AF_INET, SOCK_DGRAM, 0);
    dest_addr_.sin_family = AF_INET;
    dest_addr_.sin_port = htons(9998);
    
    const char *bridge_host = getenv("BRIDGE_HOST");
    if (!bridge_host) bridge_host = "web_input_bridge";

    struct addrinfo hints{}, *res;
    hints.ai_family = AF_INET;
    hints.ai_socktype = SOCK_DGRAM;
    
    if (getaddrinfo(bridge_host, nullptr, &hints, &res) == 0) {
      dest_addr_.sin_addr = ((struct sockaddr_in *)res->ai_addr)->sin_addr;
      freeaddrinfo(res);
    } else {
      inet_pton(AF_INET, "127.0.0.1", &dest_addr_.sin_addr);
    }
  }

  std::vector<Ort::Value> run_inference(const cv::Mat& frame, cv::Mat& blob) {
    constexpr bool bgr_to_rgb {true};
    constexpr bool center_crop {false};
    cv::dnn::blobFromImage(frame, blob, 1.0 / 255.0, cv::Size(640, 640), cv::Scalar(), bgr_to_rgb, center_crop);

    constexpr int batch_size {1};
    constexpr int color_channels {3};
    std::vector<int64_t> input_shape = {batch_size, color_channels, 640, 640};
    auto mem = Ort::MemoryInfo::CreateCpu(OrtDeviceAllocator, OrtMemTypeCPU);
    Ort::Value input_tensor = Ort::Value::CreateTensor<float>(
        mem, reinterpret_cast<float *>(blob.data), blob.total(), input_shape.data(), input_shape.size());

    const char *in_names[] = {"images"};
    const char *out_names[] = {"output0"};
    return ort_session_->Run(Ort::RunOptions{nullptr}, in_names, &input_tensor, 1, out_names, 1);
  }

  int find_best_anchor(const float* data) {
    constexpr int NA = 8400;
    int best = -1;
    float best_conf = 0.5f; // Filters out anything <= 0.5f upfront
    
    for (int i = 0; i < NA; i++) {
      float c = data[4 * NA + i];
      if (c > best_conf) {
        best_conf = c;
        best = i;
      }
    }
    return best;
  }

  void extract_and_smooth_keypoints(const float* d, int best, int orig_w, int orig_h,
                                    std::vector<cv::Point2d>& img_pts, std::vector<cv::Point3d>& obj_pts) {
    constexpr int NA = 8400;
    auto kpx = [&](int kp, int a) { return d[(5 + kp * 3) * NA + a]; };
    auto kpy = [&](int kp, int a) { return d[(5 + kp * 3 + 1) * NA + a]; };
    auto kpv = [&](int kp, int a) { return d[(5 + kp * 3 + 2) * NA + a]; };

    float sx = static_cast<float>(orig_w) / 640.0f;
    float sy = static_cast<float>(orig_h) / 640.0f;
    constexpr double kp_alpha = 0.2;

    for (int k = 0; k < 5; k++) {
      if (kpv(k, best) < 0.3f) {
        kp_init_[k] = false;
        continue;
      }
      
      cv::Point2d raw_pt(kpx(k, best) * sx, kpy(k, best) * sy);
      if (!kp_init_[k]) {
        smoothed_kps_[k] = raw_pt;
        kp_init_[k] = true;
      } else {
        if (cv::norm(raw_pt - smoothed_kps_[k]) > 50.0) {
          smoothed_kps_[k] = raw_pt;
        } else {
          smoothed_kps_[k] = kp_alpha * raw_pt + (1.0 - kp_alpha) * smoothed_kps_[k];
        }
      }
      img_pts.push_back(smoothed_kps_[k]);
      obj_pts.push_back(model_pts_[k]);
    }
  }

  bool estimate_head_pose(const std::vector<cv::Point2d>& img_pts, const std::vector<cv::Point3d>& obj_pts,
                          int orig_w, int orig_h, cv::Mat& rvec, cv::Mat& tvec, cv::Mat& cam, cv::Mat& dist) {
    double fl = static_cast<double>(orig_w);
    cam = (cv::Mat_<double>(3, 3) << fl, 0, orig_w / 2.0, 0, fl, orig_h / 2.0, 0, 0, 1);
    dist = cv::Mat::zeros(4, 1, CV_64F);
    bool solved = false;

    if (pose_initialized_) {
      rvec = smoothed_rvec_.clone();
      tvec = smoothed_tvec_.clone();
      solved = cv::solvePnP(obj_pts, img_pts, cam, dist, rvec, tvec, true, cv::SOLVEPNP_ITERATIVE);
    }
    if (!solved) {
      solved = cv::solvePnP(obj_pts, img_pts, cam, dist, rvec, tvec, false, cv::SOLVEPNP_EPNP);
    }

    if (solved) {
      constexpr double alpha = 0.3;
      if (!pose_initialized_) {
        smoothed_rvec_ = rvec.clone();
        smoothed_tvec_ = tvec.clone();
        pose_initialized_ = true;
      } else {
        smoothed_rvec_ = alpha * rvec + (1.0 - alpha) * smoothed_rvec_;
        smoothed_tvec_ = alpha * tvec + (1.0 - alpha) * smoothed_tvec_;
      }
    }
    return solved;
  }

  void process_and_send_outputs(const cv::Mat& cam, const cv::Mat& dist, const cv::Mat& tvec, 
                                size_t kp_count, geometry_msgs::msg::PoseStamped& gaze) {
    std::vector<cv::Point3d> proj_pts = {
        cv::Point3d(35, -35, 25), cv::Point3d(35, -35, -275),
        cv::Point3d(-35, -35, 25), cv::Point3d(-35, -35, -275)
    };
    std::vector<cv::Point2d> proj2d;
    cv::projectPoints(proj_pts, smoothed_rvec_, smoothed_tvec_, cam, dist, proj2d);

    cv::Mat rmat;
    cv::Rodrigues(smoothed_rvec_, rmat);
    double pitch = atan2(rmat.at<double>(2, 1), rmat.at<double>(2, 2));
    double yaw = atan2(-rmat.at<double>(2, 0), sqrt(rmat.at<double>(2, 1) * rmat.at<double>(2, 1) + rmat.at<double>(2, 2) * rmat.at<double>(2, 2)));
    double roll = atan2(rmat.at<double>(1, 0), rmat.at<double>(0, 0));

    std::ostringstream oss;
    oss << proj2d[0].x << "," << proj2d[0].y << "," << proj2d[1].x << "," << proj2d[1].y << ","
        << proj2d[2].x << "," << proj2d[2].y << "," << proj2d[3].x << "," << proj2d[3].y << ","
        << pitch * 180.0 / M_PI << "," << yaw * 180.0 / M_PI << "," << roll * 180.0 / M_PI;
    
    std::string payload = oss.str();
    sendto(udp_fd_, payload.c_str(), payload.size(), 0, (struct sockaddr *)&dest_addr_, sizeof(dest_addr_));

    RCLCPP_INFO_THROTTLE(
        this->get_logger(), *this->get_clock(), 3000,
        "pose p=%.1f y=%.1f r=%.1f  rvec=[%.2f,%.2f,%.2f]  kps=%d",
        pitch * 180.0 / M_PI, yaw * 180.0 / M_PI, roll * 180.0 / M_PI,
        smoothed_rvec_.at<double>(0), smoothed_rvec_.at<double>(1), smoothed_rvec_.at<double>(2),
        static_cast<int>(kp_count));

    gaze.pose.position.x = proj2d[0].x;
    gaze.pose.position.y = proj2d[0].y;
    gaze.pose.position.z = tvec.at<double>(2);

    double cy2 = cos(yaw * .5), sy2 = sin(yaw * .5);
    double cp = cos(pitch * .5), sp = sin(pitch * .5);
    double cr = cos(roll * .5), sr = sin(roll * .5);
    gaze.pose.orientation.w = cr * cp * cy2 + sr * sp * sy2;
    gaze.pose.orientation.x = sr * cp * cy2 - cr * sp * sy2;
    gaze.pose.orientation.y = cr * sp * cy2 + sr * cp * sy2;
    gaze.pose.orientation.z = cr * cp * sy2 - sr * sp * cy2;
  }

  void image_callback(const sensor_msgs::msg::CompressedImage::SharedPtr msg) {
    cv::Mat frame = cv::imdecode(msg->data, cv::IMREAD_COLOR);
    if (frame.empty()) return;

    geometry_msgs::msg::PoseStamped gaze;
    gaze.header.stamp = this->now();
    gaze.header.frame_id = "camera_optical_frame";
    gaze.pose.orientation.w = 1.0;

    cv::Mat blob;
    auto out_tensors = run_inference(frame, blob);
    float *d = out_tensors.front().GetTensorMutableData<float>();

    int best = find_best_anchor(d);
    if (best >= 0) {
      // ==========================================
      // GESTURE LOGIC: "Hand Raised"
      // ==========================================
      constexpr int NA = 8400;
      auto get_kpy = [&](int kp) { return d[(5 + kp * 3 + 1) * NA + best]; };
      auto get_kpv = [&](int kp) { return d[(5 + kp * 3 + 2) * NA + best]; };

      // COCO keypoints: 5/6 = shoulders, 7/8 = elbows, 9/10 = wrists.
      // A side counts as raised when its wrist OR elbow is above the
      // shoulder; low-res webcam frames often lose the wrist, so the elbow
      // fallback and low confidence thresholds keep detection usable.
      auto raised_side = [&](int shoulder, int elbow, int wrist) {
        if (get_kpv(shoulder) < 0.25f) return false;
        float shoulder_y = get_kpy(shoulder);
        bool wrist_up = get_kpv(wrist) > 0.20f && get_kpy(wrist) < shoulder_y;
        bool elbow_up = get_kpv(elbow) > 0.25f && get_kpy(elbow) < shoulder_y;
        return wrist_up || elbow_up;
      };
      bool raw_raised = raised_side(5, 7, 9) || raised_side(6, 8, 10);

      // Temporal smoothing: majority vote over the last 5 frames removes
      // single-frame flicker that would otherwise reset the gesture
      // debounce in the triage supervisor.
      raised_history_.push_back(raw_raised);
      if (raised_history_.size() > 5) raised_history_.pop_front();
      int raised_count = std::count(raised_history_.begin(), raised_history_.end(), true);
      bool hand_raised = raised_count >= 3;

      std_msgs::msg::String gesture_msg;
      gesture_msg.data = hand_raised ? "HAND_RAISED" : "DEFAULT";
      gesture_pub_->publish(gesture_msg);

      RCLCPP_INFO_THROTTLE(
          this->get_logger(), *this->get_clock(), 1000,
          "Gesture: %s (raw=%d votes=%d L_wr=%.2f R_wr=%.2f L_el=%.2f R_el=%.2f)",
          gesture_msg.data.c_str(), raw_raised, raised_count,
          get_kpv(9), get_kpv(10), get_kpv(7), get_kpv(8));
      // ==========================================
      std::vector<cv::Point2d> img_pts;
      std::vector<cv::Point3d> obj_pts;
      extract_and_smooth_keypoints(d, best, frame.cols, frame.rows, img_pts, obj_pts);

      if (img_pts.size() >= 4) {
        cv::Mat rvec, tvec, cam, dist;
        if (estimate_head_pose(img_pts, obj_pts, frame.cols, frame.rows, rvec, tvec, cam, dist)) {
          process_and_send_outputs(cam, dist, tvec, img_pts.size(), gaze);
        }
      }
    }
    gaze_pub_->publish(gaze);
  }

  Ort::Env ort_env_;
  std::unique_ptr<Ort::Session> ort_session_;
  rclcpp::Publisher<geometry_msgs::msg::PoseStamped>::SharedPtr gaze_pub_;
  rclcpp::Publisher<std_msgs::msg::String>::SharedPtr gesture_pub_;
  rclcpp::Subscription<sensor_msgs::msg::CompressedImage>::SharedPtr image_sub_;
  
  int udp_fd_;
  struct sockaddr_in dest_addr_;
  std::vector<cv::Point3d> model_pts_;

  cv::Mat smoothed_rvec_;
  cv::Mat smoothed_tvec_;
  bool pose_initialized_ = false;

  std::vector<cv::Point2d> smoothed_kps_ = std::vector<cv::Point2d>(5, cv::Point2d(0, 0));
  std::vector<bool> kp_init_ = std::vector<bool>(5, false);
  std::deque<bool> raised_history_;
};

int main(int argc, char *argv[]) {
  rclcpp::init(argc, argv);
  rclcpp::spin(std::make_shared<PerceptionNode>());
  rclcpp::shutdown();
  return 0;
}
