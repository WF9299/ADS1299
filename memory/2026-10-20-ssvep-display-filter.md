# SSVEP显示滤波（2026-10-20）

用户在配套SSVEP项目提供三份MATLAB程序并批准重写CCA及闪烁中切频，同时要求C5 Monitor增加滤波。

- tools/p300_filters.py新增FilterMode.SSVEP：SSVEP 6–90 Hz + 50 Hz窄带阻（实时），Butter2 49.5–50.5Hz带阻后Butter4 6–90Hz带通，连续因果SOS，采样率>180SPS。
- 与MATLAB设计带宽/阶数相同，连续因果输出不等同SSVEP程序逐窗filtfilt；README明确差异。原始HTTP、正式记录、原始质量、内部短接旁路及P300模式保持。
- 枚举自动进入现有Monitor下拉框；本轮没有修改p300_monitor.py或固件。外部写入逐文件哈希检查，原文件备份在SSVEP项目outputs/ssvep_realtime/monitor-ssvep-patch。
- 14项滤波/HTTP回归通过3.358秒，含GUI新模式及原始输出隔离；新增2项SSVEP频响/分包/序号/短接/采样率测试通过。
- 配套D:/Project/260720SSVEP_DISPLAY_JD已升级SSVEP1.2，MATLAB参考CCA四谐波/逐窗6–90Hz双向滤波，初始中心±4Hz/0.5Hz共17点实时切频；45项回归与实际无头HTTP模拟通过。默认分类不再使用旧基线阈值，原标定模式保留。
- 重启Monitor即能看到新模式，无需重烧。没有自动切换用户设备、接管COM、改PGA/BIAS或开启可见闪烁。未进行人体识别或MATLAB运行时逐点验证。
