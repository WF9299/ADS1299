# 配套SSVEP HTTP量程元信息（2026-10-15）

MonitorWindow._submit_http_batch在聚合包上附带units=uV；只有model.config已有vref_v与pga_gain时同时附带这对字段，避免默认量程伪装成已知设备信息。无此字段的旧接口继续兼容，SSVEP明确回退设置量程。未改原始EEG数据或固件/PGA。

SSVEP项目为D:/Project/260720SSVEP_DISPLAY_JD，新增质量/PSD、4秒窗、6–45Hz两谐波、基线与通道标定/拒识，完整说明见该项目ssvep_realtime_test/README.md。两端需重启生效。C5的9项HTTP回归通过，SSVEP33项通过；人体实测仍待进行。
