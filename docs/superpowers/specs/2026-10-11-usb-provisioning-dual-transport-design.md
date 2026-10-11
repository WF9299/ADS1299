# USB 配网、校园网共享助手与串口/Wi-Fi 双模设计

日期：2026-10-11。项目：`D:\Project\ADS1299_TONG_V2_P`。

状态：用户已确认总体方案并要求继续加入校园网/PC 热点场景；本合并规格供最终审阅，不代表功能已经实现或实板验证通过。

## 1. 目标与边界

- 不在固件固定 Wi-Fi SSID、密码或 PC 地址；通过现有隔离 USB 配置，更换网络/电脑无需修改源码或烧录。
- Monitor 手动选择串口或 Wi-Fi，同一设备同一时刻仅一个 EEG 输出通道。Wi-Fi 模式下 USB 仍可管理，不复制 EEG。
- 保留 8ch/1000SPS、原始采样码、Marker、阻抗、内部短接、BIAS、FFT、显示滤波、CSV/JSON、HTTP 和模拟器；JSON 设备名仍为 `seu`。
- 校园网路线为 PC 正常完成认证并建立 Windows 热点，板子连接热点；本次不增加企业 EAP/证书管理或校园网页登录自动化。
- 不修改 PCB、ADS 引脚或模拟算法，不改变旧 P4 工程；不将本功能视为模拟质量或人体安全认证。

## 2. 硬件、速率与会话

复用 CP2102N 经 TLP2361 接 C5 UART0（TX GPIO11、RX GPIO12），不旁路隔离。现有 528 字节/20 样本、1000SPS 对应 26.4kB/s，8N1 约需 264kbit/s，115200 不够。启动/握手保持 115200，Monitor 协商到默认 921600，提供 460800 选择；两档均需实板验收。

协商先确认并排空旧速率输出，再切速和确认。失败或心跳中断 5 秒回到 115200并释放串口协议会话；正常会话心跳间隔 1 秒。PC 消失时停止串口发送，不静默切 Wi-Fi。现板 BOOT/EN 为手动控制，Monitor 不操作下载模式；与 IDF monitor/烧录工具不能同时占用 COM。

## 3. 启动与运行时配置

USB 管理先于 ADS 初始化和 Wi-Fi 等待启动；ADS 失败仍能配置/查询故障。无网络时不阻塞串口管理和离线采集。公共硬件/算法参数留在源配置，网络及配对参数改用版本化 NVS：最近一次成功 SSID/凭据、Monitor ID/配对密钥、TCP 端口、可选手动地址、首选模式。

候选网络连上 AP 并取得地址后才替换持久配置；失败报告原因，保留旧配置和 USB 通道。取得地址、TCP 到达、时间同步、HTTP 成功分别显示。支持查询非敏感状态、清除网络配置、重新配对；不顺带擦除整个 NVS。

NVS 不等于加密保险库，不宣称已加密保护。密码/配对密钥不回显、不进 Git/普通日志/记录/HTTP。USB 显式配对后可在换 PC 时重新配对，不永久锁死设备；不在用户未操作时导出 Windows 密码。

## 4. Monitor 配网与共享 Wi-Fi 助手

连接区域增加模式、COM 列表/刷新、连接/断开、速率、设备 ID、实际模式、Wi-Fi/IP、目标 Monitor 地址和同步状态；后台执行配置/查询，不能阻塞 GUI。

### 普通个人 Wi-Fi

“同步电脑当前 Wi-Fi”经用户点击授权，只读取当前个人网络配置；Windows 权限不足或不可导出时，提供设备扫描/选择 SSID、手填一次密码的兜底。密码默认遮蔽。企业或网页登录网络不能把账号密码当作 PSK，提示使用 PC 热点或串口。

### 校园网 + PC 热点

PC 保持校园 WLAN 并由用户登录；另建独立热点。校园账号密码留在 PC/认证流程，板子只接收热点凭据。助手提供：

- 区分上行接口/校园地址、热点状态/热点侧地址；接口 Up 或 Wi-Fi Direct 虚拟网卡存在不等于热点正在广播。
- “打开 Windows 热点设置”：打开 `ms-settings:network-mobilehotspot`，用户选择共享 WLAN、通过 Wi-Fi 分享，编辑独立热点名/强密码；首次建议 2.4GHz。
- 运行时探测官方 Tethering API；允许时提供读取配置/开启/关闭。不支持、缺少能力声明/权限、驱动或策略限制时，明确禁用相关自动按钮，保留设置入口。管理员权限不保证解决全部限制，不用旧 hostednetwork 的结果判定现代热点能力。
- 获取/发送热点凭据需显式操作；无法读取时输入一次与系统热点一致的配置。可选记忆仅使用 Windows 用户级受保护存储，不写明文 JSON。启动 Monitor 不自动重置系统热点。
- “USB 下发热点配置”：发送网络/配对信息和实际热点 TCP 端点，验证联网、同步、EEG 到达后才提示切换 Wi-Fi/拔 USB。
- 实际热点 IP 根据接口/路由和设备反馈确定；有歧义时选择接口。`192.168.137.1` 是常见值，不能永久写死。
- 不自动关闭防火墙、改路由优先级或绕过校园策略；显示所需应用/端口许可和失败层次。关闭热点需用户点击，退出 Monitor 不擅自关用户热点。

板子经热点向 PC 发 EEG，Monitor 经校园上行向服务器发 HTTP。板子不要求直接访问校园服务器或公网；即使共享上网受限，只要本地热点通信可用，采集仍应可用，HTTP 独立判断。网卡/政策不允许共享时保留离线串口，不绕过限制。

## 5. Wi-Fi 动态端点

保留设备 TCP 客户端、Monitor TCP 服务器，默认 TCP1234、控制 UDP1235。新增默认 UDP1236 的可配置发现服务，仅找已配对 Monitor：请求/回复有版本、Monitor ID、随机 nonce、TCP 端口及 HMAC-SHA256 配对校验；只使用校验成功回复的实际来源地址，不信任任意广播声明。端口冲突显示错误。

发现受限时允许 USB 更新端点或手动地址，网络重连重新发现，不回退源码硬编码。保留 Wi-Fi EEG_BATCH v1 和既有控制含义，抽离固定 IP/TCP 判断，以统一控制逻辑支持串口。配对发现不等于 EEG 加密；第一版用于可信 AP/热点，不增加 TLS/复杂证书管理，不宣称公共 LAN 端到端安全。

## 6. 串口封装与统一控制

串口外层固定头为 12 字节：magic `A5 C3`（2）、version=1（1）、type（1）、request/sequence ID（4）、payload length（4）；多字节为大端，后跟 payload 和 4 字节 CRC32C（覆盖头和 payload）。负载上限 4096 字节。CRC/长度/版本异常丢弃并搜索下一魔数，不一次性无限申请缓冲。

type：1 握手请求、2 握手反馈、3 管理请求、4 管理反馈、5 STREAM_CONFIG、6 EEG_BATCH、7 控制请求、8 控制反馈、9 结构化日志、10 心跳、11 心跳反馈。握手/管理/配置/日志负载为 UTF-8 JSON 对象；管理操作包括状态、网络扫描/设置/清除、配对、模式/速率协商、Unix 时间锚点。EEG type6 复用完整 528 字节现有帧；type7/8 复用现有控制报文，承载同步、Marker、阻抗、自检、BIAS、发送参数。管理请求独立 request ID、超时与成功/失败状态；重复请求不得重复执行有副作用动作。

UART 一个接收者、一个有序发送入口，各控制客户端经分发共享，不分别打开 COM 或读数据。协议会话期间禁止裸 ESP_LOG/printf 混入，日志封装并限流；启动/故障字节允许解析器重同步。应答/同步优先于后续 EEG，不插入帧中间。控制逻辑使用来源抽象，不以 TCP 是否连接作为串口控制可用条件。

有界队列和溢出计数可见；未连接/绘图慢不阻塞采集。串口模式正常停用 Wi-Fi，不依赖 UDP；扫描仅主动配网时短暂启用并显示状态。Wi-Fi 模式 USB 仅管理。内部短接断开会话时恢复正常配置，保持现有保护。

## 7. 时间、Marker 和切换

共用采样序号/采集 monotonic 时钟、PC 四时间戳映射和最近样本 Marker 匹配。PC 提供 Unix 锚点，无需板子先通过公网 SNTP 才能连接；Wi-Fi 发现/同步先于 TCP 时间门控可用。SNTP 不静默改变在录时间映射。

串口按实际收发测 RTT/偏移/漂移，评估现有门限，不假设与 UDP 延迟相同。不用 PC 接收时间伪造采样时间，不从速率推导 1ms 刺激同步保证。未同步时显示状态、Marker 返回未同步错误，正式输出等待时间就绪。

切换在帧边界结束旧通道、清理过期队列/同步，生成新 stream_id/STREAM_CONFIG 并重新同步；显示实际而非仅请求模式。失败保持/恢复已知状态。记录和 HTTP 在过渡暂停，新流明确分段，不能无标识跨流拼接或重复。内部短接等正式输出限制对两种模式一致。

## 8. 模块与验收

固件新增配置/配对、串口协议/会话和模式层；native_wifi 使用运行时配置，分离 p300_stream 公共批处理/控制/同步与 TCP/UDP。PC 新增串口适配、配网、Windows 网络/热点适配、统一控制和 UI；复用现有数据模型/分析/导出，仅做相关重构。

测试：C/Python 交叉协议、任意分片/粘包/坏帧/长度上限/版本/重复/超时；NVS 失败回滚及秘密泄露检查；无网络/公网/ADS 失败仍可 USB 管理；全套串口控制；1000SPS 长时间吞吐/CRC/序号/队列及 GUI 响应；两个速率实板验证；热点 API 支持/不支持/权限政策的模拟和设置兜底；校园上行+实际热点/地址改变/发现兜底/HTTP；反复模式切换/记录分段/Marker 同步/自检断线恢复；现有协议/工具回归。

构建和模拟测试不代替高速串口、热点/校园策略、模拟质量或安全验证。初期实板测试断开人体电极，遵守现有 RUN/CHARGE/隔离规则。

## 9. 参考

- [Windows 移动热点](https://support.microsoft.com/en-us/windows/experience/connectivity-networking/use-your-windows-device-as-a-mobile-hotspot)
- [TetheringManager 与 wiFiControl](https://learn.microsoft.com/en-us/uwp/api/windows.networking.networkoperators.networkoperatortetheringmanager.createfromconnectionprofile)
- [热点能力限制](https://learn.microsoft.com/en-us/uwp/api/windows.networking.networkoperators.tetheringcapability)
- [Windows Wi-Fi 凭据权限](https://learn.microsoft.com/en-us/windows/win32/api/wlanapi/nf-wlanapi-wlangetprofile)
- [IDF6.0.2 C5 UART](https://docs.espressif.com/projects/esp-idf/en/v6.0.2/esp32c5/api-reference/peripherals/uart.html)
- [CP2102N](https://www.silabs.com/documents/public/data-sheets/cp2102n-datasheet.pdf)
- [TLP2361](https://toshiba.semicon-storage.com/us/semiconductor/product/isolators-solid-state-relays/detail.TLP2361.html)
