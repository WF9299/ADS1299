# ESP32-C5 ADS1299 固件与 P300 Monitor

适用于 R8 板、ESP32-C5-WROOM-1U-N8R8，基于 **ESP-IDF 6.0.2**。当前固件 **2.2.0**，默认 ADS1299 **8 通道 / 1000 SPS**，支持 USB 配网、USB 串口 EEG、原生 Wi-Fi EEG，以及正常采集时的 BIAS 放大器启停。

SSVEP 刺激和 CCA/FBCCA 在 PC 上运行，入口及源码位于仓库根目录，见[SSVEP 说明](../ssvep_realtime_test/README.md)。

## 硬件与采集配置

| ADS1299 信号 | ESP32-C5 GPIO |
|---|---:|
| START | 0 |
| PWDN# | 1 |
| SCLK | 6 |
| DIN / MOSI | 8 |
| DOUT / MISO | 9 |
| CS# | 10 |
| DRDY# | 23 |
| RESET# | 24 |

使用 SPI2、模式 1、1 MHz，事务结束后 CS 继续保持至少 3 μs。默认 PGA 为 12、内部参考电压 4.5 V；公共 REF 经 SRB1 输入，BIAS 电极由 ADS1299 反馈路径驱动。R8 输入网络为 4.99 kΩ / 4.7 nF，阻抗补偿模型已包含该网络；附带校准文件仍需按实际板子标定。

C5 为单核，采集任务优先级高于 TCP/UDP 应用任务；关闭 Wi-Fi 省电以减少发送和校时延迟。模块配置为 8 MB Flash / 8 MB PSRAM，PSRAM 使用 40 MHz。

上电检查：TP5 为 +2.5 V、TP2 为 +3.3 V、TP7 为 −2.5 V，均相对系统 GND。JP1/JP2/JP3 分别连接各自电源的源端和负载端；RESET/PWDN 应为高电平，CLKSEL 经 R28 上拉使用内部时钟。R25 连接 GPIO1 与 PWDN。ADS ID 或寄存器检查失败会停止 ADS 启动，USB 管理仍可用。

2026-10-10 实板曾确认 ADS ID `0x3e`、寄存器回读及约 1000 SPS 的 DRDY，修复过 R25 ADS 侧虚焊。这些检查不能替代电极信号质量和电气隔离验证。硬件来源与 R8 审核记录保留在仓库 `memory/`。

## 编译与烧录

在 **ESP-IDF 6.0.2 PowerShell 环境**中，从仓库根目录执行：

```powershell
cd firmware
if (-not (Test-Path components/app_config/include/app_config.h)) {
    Copy-Item components/app_config/include/app_config.example.h components/app_config/include/app_config.h
}
idf.py build
idf.py -p COM7 flash
```

将 `COM7` 替换为实际串口。目标 `esp32c5`、Flash/PSRAM 和分区配置已在 `sdkconfig.defaults` 中指定。`app_config.h` 保存本机采集配置并被 Git 忽略；共享配置修改应同步到 `app_config.example.h`。旧版头文件中的 Wi-Fi 名称、密码和固定 PC 地址宏已不再作为实际网络配置来源。

隔离 UART0 使用手动 BOOT/EN。若无法自动进入下载模式，按住 BOOT，按下并松开 EN，再松开 BOOT 后烧录；完成后释放 BOOT 并按 EN 启动。使用 `idf.py flash` 烧录匹配的 bootloader、分区表和应用镜像。正常启动串口为 115200，Monitor 会协商到 921600 或 460800。连接 GUI 前退出 IDF monitor，串口不能同时占用；保留板上隔离链路。

## Monitor 安装与连接

在 `firmware/` 目录执行：

```powershell
python -m pip install -r tools/requirements-p300-tools.txt
python tools/p300_monitor.py --host 0.0.0.0 --port 1234
```

默认网络端口如下：

| 端口 | 作用 |
|---|---|
| TCP 1234 | Monitor 监听，接收板子发起的 EEG 连接 |
| UDP 1235 | Wi-Fi 校时、Marker、阻抗、内部短接、BIAS 和发送分组控制 |
| UDP 1236 | 配对设备发现，包含 HMAC 校验 |
| HTTP 25001 | 本机 SSVEP 程序接收 Monitor 转发 |
| HTTP 25000 | SEU 电视端接收 Monitor 转发 |

### USB EEG

1. 打开 **连接/配网**，选择 CP210x 串口和速率，点击 **USB 连接并配对**。
2. 选择 **USB 串口（关闭 Wi-Fi）**，点击 **应用数据模式**。
3. 确认 ADS 运行、校时就绪、EEG STREAM、实际 SPS 和序号/CRC。USB 同时承载数据和控制。

配对后立即重连可能需要等待前一次约 5 秒租约过期。握手失败或拔线会释放串口。串口 EEG 断开时停止记录和 HTTP；Wi-Fi 采集中仅断开 USB 管理线，不会主动停止 Wi-Fi EEG。

### 电脑热点 / 校园网

1. 电脑先连接校园 WLAN 并完成正常登录，再开启独立 Windows 移动热点；为兼容性可先使用 2.4 GHz。
2. Monitor 中点击 **检测网络/热点能力**，填写热点名称、热点密码，以及电脑的**热点侧 IPv4 地址**。例如本地连接的 `192.168.137.1`；不要填校园 WLAN、Tailscale、VMware 或板子自身地址。
3. 保持 USB 连接，点击 **通过 USB 下发并验证网络**。关联和 DHCP 成功后保存到设备 `p300_network` NVS，随后回到串口模式并停止 Wi-Fi。
4. 再点击 **启用 Wi-Fi 数据模式**，或选择 Wi-Fi 并应用；确认设备发现、校时和 TCP EEG 后再拔 USB。

**配网验证成功后出现 `state: run -> init` 可以是主动停止 Wi-Fi 的正常行为。** 下发验证不自动切换采集模式，也不代表 EEG/HTTP 已验证。电脑负责校园网认证和 HTTP 上行，板子只连接电脑热点，不接收校园账号密码。

可选 Windows 热点接口依赖 `tools/requirements-p300-windows-hotspot.txt` 及系统/驱动能力。不可用时使用 Windows 设置和手工填写；安装依赖或管理员运行不保证具备热点控制权限。读取个人 Wi-Fi 信息需用户授权，PC 端可选用户级 DPAPI 保存；设备 NVS 持久化不等于加密。程序不自动关闭防火墙或重置用户热点。

设备发现同时尝试配置地址和广播，均需正确配对响应。网络禁用广播时，可使用已配置的电脑地址进行单播发现。更改发现端口需同时设置固件编译参数 `-DP300_DISCOVERY_PORT=1237` 和 Monitor `--discovery-port 1237`。

## HTTP 与 SSVEP

Monitor 的 **记录** 页提供：

| 预设 | 目的地址 | 常用路径 |
|---|---|---|
| SSVEP 测试 | `127.0.0.1:25001` | `/api/bci/data` |
| SEU 电视端 | `10.201.1.182:25000` | `/api/bci/data` |
| 自定义 | 手动填写 | 保留用户设置 |

选择预设仅填充 IP/端口，不改变当前路径、不自动推送。推送时锁定目的地；停止后再切换。切换采集输入同样会停止记录/HTTP，新流确认后需主动重新开始。

从仓库根目录运行 `python run_ssvep_realtime_test.py`，或者双击 `start_ssvep_realtime_test.bat`。**SSVEP 点击“开始全屏测试”后才监听 HTTP**，设置页或退出后不监听；提前推送出现连接失败时，先确认接收器已开始、端口和路径一致。

HTTP 发送原始 EEG 微伏值和采集时间信息，附带可用的量程、设备及 BIAS 状态。SSVEP 端独立重采样与滤波，无需先把 Monitor 波形切到 SSVEP 模式。采集时间映射不使用 USB 到达时刻代替；网络或串口速率本身不能保证 1 ms 的刺激同步。

## 显示与 FFT 滤波

| 模式 | 处理 |
|---|---|
| 原始 | 波形仅去直流，FFT 使用原始 EEG |
| 工频抑制 | 48–52 Hz 带阻 |
| P300 0.5–30 Hz | 0.5–30 Hz 带通及工频抑制 |
| P300 2–30 Hz | 2–30 Hz 带通及工频抑制 |
| SSVEP 6–90 Hz | 49.5–50.5 Hz 二阶 Butterworth 带阻，再 6–90 Hz 四阶带通 |

以上为连续因果 SOS 滤波，保留跨包状态。SSVEP 模式要求采样率大于 180 SPS；内部短接时旁路显示滤波。正式记录、原始质量统计和 HTTP 输出不随显示滤波变化。

配套 SSVEP 默认采用逐窗双向零相位滤波及四谐波 CCA；与 Monitor 同带宽/设计阶数不代表逐点相同。相关系数不是概率，最大候选与刺激一致也不能单独证明检出生理 SSVEP，详见[分析与诊断说明](../ssvep_realtime_test/README.md)。

## BIAS 独立启停（2.2.0）

1. 烧录 2.2.0 并重启 Monitor，连接 USB 或 Wi-Fi。
2. 停止正式记录和 HTTP，打开 **阻抗 → BIAS / 右腿驱动通道**。
3. 修改 **启用 BIAS**，点击 **应用到固件**，等待 **设备实际 / VERIFIED** 回读。复选框本身仅表示待应用设置。
4. 确认新数据流后，按需重新开始记录和 HTTP。

关闭时 `CONFIG3=0xE8`、`BIAS_SENSP/N=0`，保持正常 EEG 输入、SRB1、ADC 参考、PGA 和采样率；开启时 `CONFIG3=0xEC`，使用选定的非零 BIAS 通道掩码。三个寄存器均回读校验。关闭期间保留 GUI 通道选择，全部取消通道不替代独立电源开关。

BIAS 状态只在运行时生效，不写永久配置，设备重启默认开启。阻抗/内部短接退出、采集恢复和传输切换恢复当前正常模式 BIAS 状态。切换期间暂停采集并隔离稳定样本；失败时恢复并验证之前状态，回滚也失败则显示未知、保持采集暂停，需重启设备。

Wi-Fi 切换后短暂重连并发布新 `STREAM_CONFIG`，USB 发布新 `CONFIG`。数据段元信息带有 `bias`、`bias_mask`、`bias_verified` 等状态，原始 EEG 数值不因元信息改变。旧固件仍支持非零通道选择，但启停控件不可用并提示升级。

协议保持 v1、16 字节请求 / 20 字节结果与 CRC32C：`QUERY=0`、非零掩码 `SET=1`，新增 `DISABLE=2` 且掩码必须为 0；`POWER_CONTROL=0x08` 表示支持启停。只有 `VERIFIED` 且实际掩码为 0 才表示确认关闭；未验证的零掩码表示未知。

## 测试与排查

在 `firmware/` 目录运行：

```powershell
.\tests\check_firmware_scope.ps1
.\tests\run_p300_protocol_tests.ps1
.\tests\run_p300_tools_tests.ps1
```

SSVEP 测试从仓库根目录运行：

```powershell
python -m unittest discover -s ssvep_realtime_test/tests -v
python run_ssvep_realtime_test.py --help
```

2026-10-22：92 项 Python 测试（包括生产 C 函数故障注入）、主机 C 协议测试及 ESP-IDF 6.0.2 编译通过。2.2.0 应用镜像为 `build/ads1299_c5_p300_firmware.bin`，1,050,880 字节，SHA256：

```text
f2cb76802a279d75709eec7214ee9c352233447be229674bdc7723b83a81be4c
```

2026-10-23 同仓库整合验证：SSVEP 51 项测试全部通过（15.842 秒），两个程序 `--help` 入口、固件范围检查、UTF-8 文档和相对链接检查通过。迁移源码与原版本逐字节一致，仅更新说明。

尚未执行本版本实板 BIASOUT 电气验收。长期串口吞吐、切换/拔线恢复、实际校园热点与刺激时序需在目标设备验证。

- **USB 无法连接**：查看实际错误，关闭占用串口程序并检查端口；早期“请退出 IDF monitor”默认提示不是占用检测结果。
- **连上 USB 但无 EEG**：先看 ADS 是否运行及 ID/寄存器/DRDY 日志；USB 管理连通不等于 ADS 启动成功。
- **部分通道零线**：查看质量页原始 DC、峰值 %FS 和削顶；去直流后的零线也可能来自满量程恒定输入，不能仅凭波形判断丢通道。
- **Wi-Fi 获得 IP 后停止**：区分配网验证主动停止与采集异常；完成下发后还需应用 Wi-Fi 数据模式。
- **HTTP 连接失败**：检查接收程序已监听、IP/端口/路径一致，再查看 Monitor 错误；本机 SSVEP 无需经过校园网。

构建产物、下载的组件、本机配置和实验数据不提交。共享配置使用 `sdkconfig.defaults`、`dependencies.lock` 和 `app_config.example.h`；Wi-Fi 密码不在设备状态中返回，也不写入仓库。
