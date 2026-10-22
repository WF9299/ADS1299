# ADS1299 ESP32-C5 采集与 SSVEP 实时测试

本仓库包含 ESP32-C5 固件、PC 端 P300 Monitor，以及配套的 SSVEP 刺激与实时 CCA/FBCCA 分析程序。固件当前为 **2.2.0**，SSVEP 程序为 **1.3.0**。

ADS1299 默认采集 8 通道、1000 SPS，通过隔离 USB 串口或 ESP32-C5 原生 Wi-Fi 传给 Monitor；Monitor 负责波形、FFT、质量诊断、记录和 HTTP 转发。SSVEP 程序通过本机 HTTP 接收原始微伏数据，独立进行滤波与分析。

```text
ADS1299 → ESP32-C5 → USB 串口 / Wi-Fi TCP → P300 Monitor
                                             ├─ 波形、FFT、质量、记录
                                             ├─ 本机 HTTP → SSVEP 刺激 + CCA/FBCCA
                                             └─ 校园网 HTTP → SEU 电视端
```

## 目录

| 路径 | 用途 |
|---|---|
| `firmware/` | ESP-IDF 6.0.2 固件工程；在此目录编译、烧录 |
| `firmware/tools/` | Python Monitor、协议与网络工具 |
| `firmware/tests/` | 固件协议、Monitor 与故障恢复测试 |
| `ssvep_realtime_test/` | 独立 SSVEP 程序、说明和测试 |
| `run_ssvep_realtime_test.py` | SSVEP Python 启动入口 |
| `start_ssvep_realtime_test.bat` | Windows 双击启动 SSVEP |
| `docs/`、`memory/` | 设计文档和项目决策记录 |

## 快速开始

在仓库根目录使用 Python 3.11 或更新版本安装两端依赖并启动 Monitor：

```powershell
python -m pip install -r firmware/tools/requirements-p300-tools.txt -r ssvep_realtime_test/requirements.txt
python firmware/tools/p300_monitor.py --host 0.0.0.0 --port 1234
```

1. 在 Monitor 的 **连接/配网** 页选择板子的 CP210x 串口，点击 **USB 连接并配对**。先关闭占用串口的 IDF monitor 等程序。
2. 选择 **USB 串口** 并应用，检查实际 SPS、序号、CRC 和通道质量；需要无线采集时，按[固件与 Monitor 说明](firmware/README.md)配置电脑热点。
3. 双击 `start_ssvep_realtime_test.bat`，或运行 `python run_ssvep_realtime_test.py`。按实际接线选择通道，设置频率、屏幕参数和 50 × 50 mm 尺寸校准。
4. 在 SSVEP 设置页点击 **开始全屏测试** 后，接收器开始监听。Monitor 的 **记录** 页选择下表预设，再点击开始 HTTP 推送。
5. 默认积累 4 秒连续 EEG，每 200 ms 更新一次分析；切频后先排除 0.5 秒过渡，再积累新窗口。按 **ESC** 退出。

| HTTP 预设 | 地址 | 默认路径 |
|---|---|---|
| SSVEP 测试 | `127.0.0.1:25001` | `/api/bci/data` |
| SEU 电视端 | `10.201.1.182:25000` | `/api/bci/data` |

预设只填写地址和端口，保留当前路径；不会自动开始推送。发送中先停止，再切换目的地。SSVEP 设置页尚未监听，退出全屏测试后也会停止接收。

## 当前功能

- **采集与诊断**：USB/Wi-Fi 双传输、Marker、通道质量、阻抗、内部短接、发送分组、记录和原始 EEG HTTP 输出。
- **显示滤波**：原始、工频抑制、P300 0.5–30 Hz、P300 2–30 Hz、SSVEP 6–90 Hz。显示/FFT 滤波不改变原始记录和 HTTP 数据。
- **BIAS 启停**：2.2.0 固件支持正常 EEG 模式下独立关闭/开启 BIAS 放大器，USB 与 Wi-Fi 均可控制，并回读寄存器确认。
- **SSVEP**：黑色全屏、中央 5 cm 方块持续闪烁；MATLAB 参考四谐波 CCA 与 FBCCA 对照；分析候选随当前刺激取 ±4 Hz、0.5 Hz 步进；实时相关系数柱状图和质量/PSD。

BIAS 操作需要先烧录 **2.2.0** 并重启 Monitor。停止记录和 HTTP 后，在 **阻抗 → BIAS / 右腿驱动通道** 修改 **启用 BIAS**，点击 **应用到固件**，以 **设备实际 / VERIFIED** 状态为准。关闭后仍采集正常 EEG；设备重启恢复默认开启。

SSVEP 闪烁期间，**←/→** 以 0.5 Hz 步进切换，**Home** 回初始频率，也可点击底部频率条；**B** 切换 CCA 柱图，**D** 切换质量/PSD。可选的“屏蔽 16.5 Hz 排名”默认关闭，被屏蔽柱仍显示真实相关系数。完整算法和结果解释见 [SSVEP 使用说明](ssvep_realtime_test/README.md)。

## 编译、验证与本地文件

- 固件编译、引脚和网络配置：[firmware/README.md](firmware/README.md)。工程入口是 `firmware/`。
- SSVEP 安装、滤波、快捷键与记录：[ssvep_realtime_test/README.md](ssvep_realtime_test/README.md)。
- 已有固件验证（2026-10-22）：92 项 Python 测试、主机 C 协议测试和 ESP-IDF 6.0.2 编译通过；BIASOUT 实板电气效果尚待验证。
- 整合验证（2026-10-23）：在本仓库目录运行 SSVEP 51 项测试全部通过；两端启动入口、固件范围检查与文档链接检查通过。
- 本次整合只增加独立 SSVEP 程序和中文说明，不修改固件采样与控制代码。软件测试不能替代实际电极质量与刺激时序测量。

本机 `app_config.h`、HTTP/SSVEP 设置、尺寸校准、实验数据及编译产物均不提交。首次编译需从 `app_config.example.h` 复制采集配置；Wi-Fi 名称、密码和 PC 地址通过 Monitor USB 配网写入设备。

GitLab：[ads1299_esp32c5_tong_v1](http://10.201.1.182:1081/zhaoboyuan/ads1299_esp32c5_tong_v1)。
