# 2026-10-22 BIAS修改前Git基线

用户要求先提交当前版再改BIAS。当前本地快照429c50c，分支codex/usb-provisioning-dual-transport；包含配网恢复、滤波、HTTP预设及已有日志，新增忽略本机HTTP设置。未push、烧录或改设备状态。Python83项回归和C协议主机测试通过。

配套SSVEP项目快照ebf6741，51项测试通过。具体BIAS规格在D:/Project/260720SSVEP_DISPLAY_JD/docs/superpowers/specs/2026-10-22-c5-bias-power-control-design.md，文档提交20d9709，等待用户确认具体行为，尚无BIAS实现改动。

拟增加独立启停，USB CONTROL与Wi-Fi UDP共用；关闭CONFIG3=0xE8并清BIAS求和输入，正常EEG设置保持。默认上电开启，运行时关闭不写NVS；回读/失败回滚/自检阻抗后恢复当前状态，沿用记录和HTTP互锁。协议增加明确DISABLE与能力标志，未知和已关闭必须区分，旧固件不能假装关闭。详细决策与后续状态以配套项目日志为准。
