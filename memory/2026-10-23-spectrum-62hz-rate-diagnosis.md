# 62.81 Hz 谱峰与约 780 SPS 的诊断

- 用户截图：FFT 选择 CH7，原始模式，主峰 62.81 Hz / 80.03 µV；STOPPED、SPS=0、数据年龄约93秒、序号丢样0、事件drop0、有意中断5。
- 用户确认内部短接通过，CH7/8仍未接电极，CH1/2的谱峰同样62.81Hz。上一轮已确认BIAS仅使用CH1–CH6。
- 按要求重新开始采集并稳定约10秒后，用户报告实际SPS稳定约780。该指标是上位机处理样本的速率，不单凭它断言ADC时钟降到了780Hz。
- 代码事实：FFT使用配置采样率（正常1000SPS），频率轴为k*Fs/N；时域按包时间戳加包内标称间隔。显示采样率由received_samples增量/PC时间计算。
- 合成复现：真实50Hz、实际796SPS、1592点，以当前相同的去均值/Hann/FFT并按1000SPS标频，得到62.81Hz。780SPS则对应50*1000/780=64.10Hz；不可声称780与旧图62.81精确吻合。
- 对截图CH1红色曲线做只读像素分析，在约2秒横轴范围识别到99个峰，支持主振荡仍接近50Hz的可能；仅是图像估计，不代替原始数据。
- 固件capture_sample成功读取后才递增seq，DRDY合并/漏采由missed_drdy另计；因此Monitor序号丢样0不排除ADC读出前的丢失。
- 正常固件LOFF/SENSP/SENSN=0并回读；阻抗AC激励为约31.25Hz。不能因为谱峰接近62.5Hz就认定正常模式常开检测信号。
- 当前优先假设是有效采样间隔/缺样与标称1000不一致，源头尚未实测确定。需以连续原始包时间戳、序号、DRDY/acq/missed/spi_fail/ring_drop区分采集漏样、传输/处理积压与配置问题。未找到本次原始记录，旧10月20日数据不用于替代。
- 后续修改应优先恢复连续采集并显式报告采样时基异常；不能简单将FFT或SSVEP的Fs改780，非均匀缺样不能靠重标频轴恢复。仅接收/GUI延迟而样本完整时也不应按吞吐量重标频轴。
- 本轮无产品代码、串口占用、设备配置或固件变更。

证据：firmware/tools/p300_monitor.py 的 EegDataModel、_refresh_spectrum、_refresh_diagnostics；firmware/components/eeg_acq/eeg_acq.c 的 capture_sample/acq_task；https://numpy.org/doc/stable/reference/generated/numpy.fft.rfftfreq.html
