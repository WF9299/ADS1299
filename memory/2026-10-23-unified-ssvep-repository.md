# C5 与 SSVEP 统一仓库

- 用户要求固件、Monitor和配套SSVEP程序统一发布到现有GitLab：<http://10.201.1.182:1081/zhaoboyuan/ads1299_esp32c5_tong_v1>，README使用中文。
- `firmware/`继续作为ESP-IDF工程，`firmware/tools/`是Monitor；根目录新增`ssvep_realtime_test/`、Python启动器和Windows启动脚本，SSVEP在PC侧经本机HTTP接收原始EEG。
- 独立SSVEP来源是原显示项目中的1.3版本；整合不引入旧P4/Brookesia工程或另一仓库的无关历史。当前统一源码位于本仓库，后续联合修改以此为准。
- 每台电脑重新配置屏幕/通道/尺寸校准；设置、实测数据和生成图均不分发。SSVEP测试预设为127.0.0.1:25001，SEU电视为10.201.1.182:25000，路径通常为/api/bci/data。
- 本次整合不改算法、固件采样或BIAS实现。BIAS仍需2.2固件；SSVEP接收器只在测试开始后监听，Monitor显示滤波不改变原始HTTP输出。
