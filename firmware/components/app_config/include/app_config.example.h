#pragma once

/* Copy to app_config.h and fill in your local network settings before building. */
/* app_config.h is intentionally ignored by Git because it contains credentials. */

/* ADS1299 acquisition */
#define P300_SAMPLE_RATE_HZ                     1000U
#define P300_ADS_CHANNEL_MASK                   0xFFU
#define P300_ADS1299_INTERNAL_TEST              0
#define P300_EEG_ACQ_RING_SIZE                  4096U
#define P300_EEG_ACQ_STATS_INTERVAL_MS          5000U
#define P300_LOG_ACQ_STATS                      1
#define P300_EEG_ACQ_TASK_PRIORITY               12U
#define P300_IMPEDANCE_DC_SETTLE_MS             250U
#define P300_IMPEDANCE_BACKGROUND_SAMPLES       256U
#define P300_IMPEDANCE_SETTLE_SAMPLES           256U
#define P300_IMPEDANCE_MEASURE_SAMPLES          1024U
#define P300_IMPEDANCE_FRAME_TIMEOUT_MS         10U

/* ESP32-C5 native Wi-Fi station. */
#define P300_WIFI_SSID                          "YOUR_WIFI_SSID"
#define P300_WIFI_PASSWORD                      "YOUR_WIFI_PASSWORD"
#define P300_WIFI_STATIC_IP_ENABLE              0
#define P300_WIFI_STATIC_IP                     "0.0.0.0"
#define P300_WIFI_STATIC_GATEWAY                "0.0.0.0"
#define P300_WIFI_STATIC_NETMASK                "0.0.0.0"

/* ESP32 is the TCP client; use the PC's reachable network address here. */
#define P300_TCP_SERVER_IP                      "192.168.137.1"
#define P300_TCP_PORT                           1234U
#define P300_MARKER_PORT                        1235U
#define P300_SNTP_SERVER                        "pool.ntp.org"
#define P300_MIN_UNIX_EPOCH                     1704067200LL

#define P300_HOLDBACK_MS                        60U
#define P300_TCP_SOCKET_TIMEOUT_MS              250U
#define P300_TCP_SEND_RETRY_COUNT               4U
#define P300_TCP_SEND_RETRY_DELAY_MS            2U
#ifndef P300_TCP_COALESCE_FRAMES
#define P300_TCP_COALESCE_FRAMES                1U
#endif
#if P300_TCP_COALESCE_FRAMES != 1U && P300_TCP_COALESCE_FRAMES != 2U
#error "P300_TCP_COALESCE_FRAMES must be 1 or 2"
#endif
#define P300_TCP_COALESCE_MAX_WAIT_MS           25U
#define P300_STREAM_TASK_STACK_SIZE             6144U
#define P300_STREAM_TASK_PRIORITY               8U
#define P300_MARKER_TASK_STACK_SIZE             5120U
#define P300_MARKER_TASK_PRIORITY               9U

/* Marker synchronization policy. */
#define P300_SYNC_STARTUP_HZ                    10U
#define P300_SYNC_STEADY_HZ                     2U
#define P300_SYNC_STEADY_VALID_MEASUREMENTS     16U
#define P300_SYNC_HISTORY                       64U
#define P300_SYNC_MAX_RTT_US                    10000U
#define P300_SYNC_MIN_RTT_SLACK_US              2000U
#define P300_SYNC_MAX_AGE_US                    10000000LL
#define P300_MARKER_MAX_ERROR_US                500U
