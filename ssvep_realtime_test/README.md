# C5 Monitor 配套 SSVEP 实时测试 1.3

黑色全屏、中央50×50mm黑白刺激，持续运行至ESC退出。默认采用用户提供的MATLAB参考CCA方法，支持闪烁中切频，原CCA/FBCCA标定模式保留作对照。

## 使用

1. 重启当前 **ESP32-C5 P300 Monitor**，正常接收USB或Wi-Fi EEG。
2. 首次使用先运行 `python -m pip install -r ssvep_realtime_test/requirements.txt`。双击根目录 `start_ssvep_realtime_test.bat`，或运行 `python run_ssvep_realtime_test.py`。在统一C5仓库中，Monitor入口为 `python firmware/tools/p300_monitor.py`，依赖安装命令为 `python -m pip install -r firmware/tools/requirements-p300-tools.txt`。
3. 选择 **MATLAB CCA · 6–90 Hz · 四谐波 · 逐窗零相位**。初始频率用于底部刺激快捷范围和Home；**CCA分析中心始终跟随当前刺激频率**。新配置默认17Hz；升级保留已有频率、通道、端口、窗长和屏幕尺寸设置。
4. 选择27英寸、2560×1440、210Hz显示器，用静态标尺把白框实测调至50×50mm。按实际接线选择通道，程序不假定CH编号对应O1/Oz/O2。
5. Monitor“记录”页选择HTTP预设 **SSVEP测试 — 127.0.0.1:25001**，路径通常为`/api/bci/data`，点击开始推送。另有 **SEU电视端 — 10.201.1.182:25000** 与自定义。预设只填IP/端口，不自动开始、不改现有路径；推送时锁定，先停止再切换。修改端口时两端一致。
6. 点击“开始全屏测试”。先测量约1秒软件换帧率，随后闪烁。默认模式不采旧版40秒静态基线；过渡0.5秒、积累完整4秒连续EEG后，每200ms输出结果。
7. **←/→** 每次减/加0.5Hz，**Home** 回初始频率，也可点击底部频率条跳转。默认上方显示CCA柱状图；**B** 切换柱图，**D** 切换质量/PSD，**1–8** 切PSD通道，**ESC** 退出。同一个图键再次按下可隐藏面板。

刺激快捷条固定为初始频率±4Hz；CCA候选则以**当前频率±4Hz、0.5Hz步进**动态生成17点。例如初始14Hz快捷条10–18Hz，选择16.5Hz后，CCA分析和图横轴立即改为12.5–20.5Hz，Home仍回14Hz。界面分别显示两个范围，端点不循环。

MATLAB模式要求当前CCA范围最低≥6Hz、最高四次谐波<125Hz，因此当前刺激需 **≥10且<27.25Hz**，实际刺激还须低于实测刷新率一半。不满足的快捷频点禁用，不静默截断CCA范围。

接收器在点击开始后才监听；设置页未监听，退出后停止。Monitor先推送会暂时报连接失败，SSVEP开始后自动恢复。HTTP200表示接收成功，“过渡/积累EEG”是分析进度。SSVEP只接本机HTTP，不接管COM或板子TCP采集。

Monitor“记录”页另有 **特殊：CH1–CH6 + 两路复制** 推送模式：输出前6列保留CH1–CH6，后两列复制从前六路选择的两个不同通道。例如CH7来源选CH3、CH8来源选CH6，接收顺序为`[1,2,3,4,5,6,3,6]`，仍是8列格式，只有6路独立测量。本程序保留`channel_mode/channel_map`到原始记录及特殊模式分析来源信息，映射变化时清窗；旧版无映射包按物理CH1–CH8处理。使用特殊模式时分析通道编号代表输出列，CH7/CH8已不是原物理电极；复制不会新增独立信息。

## MATLAB参考分析

```text
原始uV → 抗混叠FIR重采样250SPS → 连续4秒窗口
→ 49.5–50.5Hz Butterworth二阶带阻，双向滤波
→ 6–90Hz Butterworth四阶带通，双向滤波
→ 每个候选的1–4次谐波sin/cos参考
→ 第一典型相关系数 → 最大者为CCA估计频率
```

对应 `CCA_acc_test.m`、`cca_no_downsample.m`、`filter_eeg.m` 的实际代码。主脚本“7–19Hz”注释与函数不符，按6–90Hz实码。阶数指Butterworth设计参数，带通/带阻变换后阶数翻倍，双向处理使幅频响应平方。

连续FIR用Kaiserβ=5、20×降采样倍数+1个系数，保留跨包状态；等待对称滤波器未来半段后输出对齐样本，不按HTTP包单独重采样。1000SPS时新增约40ms滤波等待。逐窗分别做带阻和带通，采用显式奇对称延拓，长度12/24个250SPS样本。

参考时间按MATLAB`(1:N)/fs`，完整保留四谐波，不按带宽悄悄删去高次参考。CCA先去均值，以SVD子空间计算最大典型相关系数，按有效秩处理重复/相关通道。内部完整精度，显示四位小数。

本机未找到MATLAB运行时。已验证独立QR/SVD、直接形式双向滤波和FIR多相对照，**未声称不同MATLAB版本逐点完全相同**；重采样系数和边界处理可能产生边缘差异。

## 结果与质量

上方默认CCA实时柱图，纵轴固定0–1，每个结果更新时缓存绘制，不在每个刺激帧重绘图像。青色为当前刺激、橙色为最大有效候选、绿色表示两者一致。切频/断流/进程停止时清空旧柱；灰色“屏蔽”柱仍显示真实ρ。

设置增加默认关闭的 **屏蔽16.5 Hz排名**；命令行可用`--exclude-165`/`--include-165`覆盖。它同时影响CCA与FBCCA赢家选择，所有原始ρ/score和EEG不变。当前刺激本身为16.5Hz且开启屏蔽时，会明确提示不能识别该目标。日志保存`excluded_frequencies`、`eligible_candidates`、`target_excluded`、未屏蔽前赢家`cca_raw_frequency`/`fbcca_raw_frequency`和动态`candidate_center`。

2026-10-20真实记录的离线消融表明：16.5Hz偏向主要由三次谐波49.5Hz匹配驱动，原四谐波在39个不重叠窗有37次选择16.5Hz，去三次谐波/去48–52Hz参考或宽带阻均降为0次；其基频相关性中位0.122，单49.5Hz为0.438。窄带阻在49.5Hz边界双向仅约-6.02dB，残余工频/有限窗行为可进入CCA参考匹配。物理噪声来源尚未由硬件测量定位；消融或屏蔽后的新赢家不代表检出成功。本轮仍保留MATLAB参考滤波/四谐波默认供对照。详细图与统计位于`outputs/ssvep_realtime/review_165hz/`。

显示CCA估计频率、最大ρ、目标ρ、第一/第二名差值，并保留FBCCA独立对照。FBCCA使用6–90、14–90、22–90Hz三子带，子带内谐波参考；它不是所给单带MATLAB代码的一部分，也不再作为新模式CCA输出的前置条件。

`score(f)=Σ(k^(-1.25)+0.25)×ρ_k(f)^2`，k=1,2,3。score可大于1，ρ/score不是概率。新模式每个有效窗都有最大候选；“匹配当前刺激”只表示赢家等于刺激频率，**不等于证明检出神经响应**。日志用`target_match`，默认`detected=false`，不从重叠滑窗计算准确率/ITR。

保留原始质量门控：恒定/非有限通道剔除；至少5%样本达到98%FS，或至少0.1%达到99.99%FS，则剔除。量程优先用Monitor的`vref_v/pga_gain×10^6 uV`，旧Monitor无元信息时回退并提示。ADC削顶不能靠数字滤波恢复。

质量/PSD取滤波前原始数据，显示DC、RMS、%FS、削顶及局部SNR。原始PSD有50Hz峰不代表CCA未陷波。断流、时间跳变、重复/乱序或来源/量程变化会清窗，无效时隐藏旧分数。

## 切频和记录

首个新频率画面实际换帧后才提交频率、实验段号和epoch。旧分数立即撤销；新频率继续闪烁，前0.5秒过渡不分析，之后积累完整新窗。切换前EEG和排队旧结果不混入新分析；快速按键可合并未呈现请求。

闪烁停顿超过max(100ms,5帧周期)重新积累；旧标定模式静态灰块超过1秒才重采。软件时序日志不等于光电实测。

每次会话保存于 `outputs/ssvep_realtime/时间戳/`：

- `settings.json`：初始设置、固定中心、尺寸校准。
- `frames.csv`：换帧时钟/间隔、白黑灰状态、相位、估计漏帧、`target_frequency/trial_id`。
- `analysis.jsonl`：全候选结果、质量、频率/段号/epoch/窗口时间，以及`frequency_change/trial_start`事件。
- `raw_eeg.jsonl`：原始微伏、首样本时间、标记、量程、接收时控制信息，未改输入值。
- `logging_status.json`、`raw_recording_status.json`：队列与写入状态。
- `calibration.json`：仅旧模式实际完成标定时生成。

原始包可能跨边界或迟到，此时`trial_id/target_frequency`为null。`control_trial_id/control_frequency`只表示接收时控制状态；应按`stimulus_onset`与呈现事件逐样本分段，按`analysis_epoch`剔除过渡，不能把整包控制标签当所有样本真实标签。非有限样本保留Python JSON可读NaN/Infinity供诊断。

## Monitor新模式

统一C5仓库中的显示滤波实现位于 `firmware/tools/p300_filters.py`。Monitor提供 **SSVEP 6–90 Hz + 50 Hz窄带阻（实时）**：二阶Butter49.5–50.5Hz带阻，再四阶Butter6–90Hz带通，要求采样率>180SPS。

Monitor是连续因果SOS，SSVEP是逐窗双向滤波，二者相位/幅频响应不能视为逐点相同。新模式影响波形/FFT，原始HTTP、正式记录、原始质量不变，内部短接继续旁路。P3000.5–30/2–30Hz保持；无需重烧固件，不自动改PGA、BIAS或网络。

## 旧模式与设置迁移

可选6–45Hz两谐波或6–90Hz三谐波旧连续因果模式。候选可自定义，开始前标定可用，R重标定；默认静态10独立窗、响应3独立窗，约40+12秒，再重新采测试数据。

旧模式保留通道SNR增强筛选和基线拒识：CCA/FBCCA同时匹配当前刺激，ρ/score/SNR/候选差距超冻结阈值并持续0.8秒才通过。旧模型按模式、频率和来源等校验，不复用于MATLAB模式；其经验阈值未被当成人体检出准确率验证。

设置现为schema4，旧配置保留采集/显示设置，屏蔽默认关闭；旧schema1/2在兼容频率范围内采用MATLAB默认。超范围旧高频继续原模式，避免阻止打开设置页。尺寸校准绑定屏幕、分辨率和缩放，换屏/模式后失效。

27英寸16:9估算597.7×336.2mm，50mm约214×214物理像素；物理尺寸以尺验为准。刺激使用垂直同步时间相位采样50%占空比黑白波形，不把210/17简单取整。软件换帧不能替代真实亮度时序测量。

## 命令行与验证

```powershell
python run_ssvep_realtime_test.py --frequency 17 --channels 3,4,6,8 --profile matlab --window 4
python run_ssvep_realtime_test.py --preview outputs/ssvep_realtime/preview.png
python run_ssvep_realtime_test.py --headless --port 25101 --duration 20
# 另开终端，仅向上面专用测试端口发送合成数据
python -m ssvep_realtime_test.simulator --port 25101 --frequency 17 --duration 10
python -m unittest discover -s ssvep_realtime_test/tests -v
```

预览/无头模式不闪烁。上述示例显式指定专用测试端口25101；模拟器自身默认端口仍为25001，测试时应使用独立空闲端口，不与真实Monitor混发。`--no-dialog`会直接全屏闪烁，日常保留设置页。旧模式可用`--profile focused --calibrate`；`--candidates`只用于旧模式。独立程序依赖Python3.10+、NumPy、SciPy、PyQt5；与当前Monitor共用环境时使用Python3.11+。

`outputs/ssvep_realtime/` 中的会话数据和前述离线诊断图均为本机生成文件，不随仓库分发。个人设置 `ssvep_realtime_test_settings.json` 与 `ssvep_realtime_calibration.json` 同样不提交；新克隆需重新选择屏幕、通道及尺寸校准。

方法：[MathWorks canoncorr](https://www.mathworks.com/help/stats/canoncorr.html)、[filtfilt](https://www.mathworks.com/help/signal/ref/filtfilt.html)、[resample](https://www.mathworks.com/help/signal/ref/resample.html)、[Chen等FBCCA原论文](https://pubmed.ncbi.nlm.nih.gov/26035476/)。实际人体识别、5cm尺验和光电时序需实测确认。
