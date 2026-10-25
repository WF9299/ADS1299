# GitHub 项目发布

- 用户明确指定 https://github.com/WF9299/ADS1299.git 为当前统一项目的GitHub目标。
- 权威源码仍为 D:/Project/ADS1299_TONG_V2_P，含firmware/与ssvep_realtime_test/；不上传旧P4工程。
- GitHub远程命名github，空仓库初始发布使用main；既有GitLab origin和本地codex/usb-provisioning-dual-transport分支保留。
- 包含此前HTTP六路来源复制为八列、SSVEP映射兼容、0–120Hz频谱及中文文档。
- 发布检查Monitor97项、SSVEP53项回归均通过；未烧录、接管设备或改变采集算法。
- app_config.h、运行设置、校准/实验数据和构建产物继续忽略；GitHub上传保留既有Git历史。
- 本机GitHub直连不可用，使用用户现有系统代理的命令级http.proxy参数；不要修改全局Git或系统网络设置。
- 100Hz干扰根因及曾出现的780SPS问题仍未确定；用户后来报告重启恢复1000SPS、BIAS实际0x3F，发布不代表解决这些硬件诊断问题。
