# 2026-10-13 项目只读核对

- 用户从旧P4目录明确改指向 D:\Project\ADS1299_TONG_V2_P\firmware；后续C5固件和上位机分析以此工作区为准。
- 已核对当前源码是独立C5版本2.1.0（STREAM_CONFIG=P300_DUAL_V1），ESP-IDF6.0.2、原生Wi-Fi；不能沿用旧P4+C5 Hosted架构或固定SSID/IP的结论。
- 固件main/main.c先启动NVS与device_manager/USB管理，再初始化ADS和p300_stream。runtime_config、serial_protocol、uart_link、device_manager实现USB配网/配对和双模；上位机入口tools/p300_monitor.py，新增连接页tools/p300_connection_ui.py。
- 当前Monitor有六页签与发送分组控制；旧P4 Monitor曾删除该按钮的结论不适用于这里。显示参考/滤波与原始记录/HTTP数据分离，模式切换主动关闭记录/HTTP，由用户确认新流后重新开启。
- 工作区处于codex/usb-provisioning-dual-transport，2.1.0改动尚未提交；本轮只读业务源码，未覆盖已有修改、未编译或烧录。
- 既有记录的软件测试/构建通过与实板验收应区分：高速串口、校园热点、动态发现、反复切换及刺激同步仍需真实设备验证；本轮不把历史测试描述当作重新验证结果。
