#pragma once

#include <stdbool.h>
#include <stddef.h>
#include <stdint.h>

#include "app_config.h"

#ifdef __cplusplus
extern "C" {
#endif

#define P300_EEG_CHANNELS 8U
#define P300_EEG_BATCH_SAMPLES 20U
#define P300_EEG_SAMPLE_BYTES 25U
#define P300_EEG_BATCH_BYTES 528U
#define P300_CLOCK_HISTORY P300_SYNC_HISTORY
#define P300_MARKER_PACKET_BYTES 24U
#define P300_SYNC_REPLY_BYTES 32U
#define P300_SYNC_RESULT_BYTES 40U
#define P300_MARKER_ACK_BYTES 20U
#define P300_IMPEDANCE_REQUEST_BYTES 16U
#define P300_IMPEDANCE_RESULT_BYTES 84U
#define P300_SELF_TEST_REQUEST_BYTES 16U
#define P300_SELF_TEST_RESULT_BYTES 20U
#define P300_BIAS_CONTROL_REQUEST_BYTES 16U
#define P300_BIAS_CONTROL_RESULT_BYTES 20U
#define P300_TX_CONTROL_REQUEST_BYTES 16U
#define P300_TX_CONTROL_RESULT_BYTES 20U
#define P300_IMPEDANCE_INVALID_VALUE UINT32_MAX

#define P300_CONFIG_NORMAL 1U
#define P300_CONFIG_INTERNAL_SHORT 2U

typedef enum {
    P300_UDP_TYPE_SYNC_REQ = 1,
    P300_UDP_TYPE_SYNC_REPLY = 2,
    P300_UDP_TYPE_MARKER = 3,
    P300_UDP_TYPE_MARKER_ACK = 4,
    P300_UDP_TYPE_SYNC_RESULT = 5,
    P300_UDP_TYPE_IMPEDANCE_REQUEST = 6,
    P300_UDP_TYPE_IMPEDANCE_RESULT = 7,
    P300_UDP_TYPE_SELF_TEST_REQUEST = 8,
    P300_UDP_TYPE_SELF_TEST_RESULT = 9,
    P300_UDP_TYPE_BIAS_CONTROL_REQUEST = 10,
    P300_UDP_TYPE_BIAS_CONTROL_RESULT = 11,
    P300_UDP_TYPE_TX_CONTROL_REQUEST = 12,
    P300_UDP_TYPE_TX_CONTROL_RESULT = 13,
} p300_udp_type_t;

typedef enum {
    P300_MARKER_STATUS_OK = 0,
    P300_MARKER_STATUS_UNSYNCED = 1,
    P300_MARKER_STATUS_EXPIRED = 2,
    P300_MARKER_STATUS_INVALID_CODE = 3,
    P300_MARKER_STATUS_QUEUE_FULL = 4,
    P300_MARKER_STATUS_ACQUISITION_PAUSED = 5,
} p300_marker_status_t;

typedef enum {
    P300_IMPEDANCE_STATUS_OK = 0,
    P300_IMPEDANCE_STATUS_BUSY = 1,
    P300_IMPEDANCE_STATUS_INVALID_REQUEST = 2,
    P300_IMPEDANCE_STATUS_PAUSE_TIMEOUT = 3,
    P300_IMPEDANCE_STATUS_CONFIG_FAILED = 4,
    P300_IMPEDANCE_STATUS_SAMPLE_TIMEOUT = 5,
    P300_IMPEDANCE_STATUS_RESTORE_FAILED = 6,
    P300_IMPEDANCE_STATUS_INTERNAL_ERROR = 7,
} p300_impedance_status_t;

typedef enum {
    P300_IMPEDANCE_PARSE_DROP = 0,
    P300_IMPEDANCE_PARSE_INVALID = 1,
    P300_IMPEDANCE_PARSE_OK = 2,
} p300_impedance_parse_status_t;

typedef enum {
    P300_SELF_TEST_STATUS_OK = 0,
    P300_SELF_TEST_STATUS_BUSY = 1,
    P300_SELF_TEST_STATUS_INVALID_REQUEST = 2,
    P300_SELF_TEST_STATUS_NO_TCP_STREAM = 3,
    P300_SELF_TEST_STATUS_PAUSE_TIMEOUT = 4,
    P300_SELF_TEST_STATUS_CONFIG_FAILED = 5,
    P300_SELF_TEST_STATUS_RESTORE_FAILED = 6,
    P300_SELF_TEST_STATUS_INTERNAL_ERROR = 7,
} p300_self_test_status_t;

typedef enum {
    P300_SELF_TEST_PARSE_DROP = 0,
    P300_SELF_TEST_PARSE_INVALID = 1,
    P300_SELF_TEST_PARSE_OK = 2,
} p300_self_test_parse_status_t;

typedef enum {
    P300_BIAS_OPERATION_QUERY = 0,
    P300_BIAS_OPERATION_SET = 1,
    P300_BIAS_OPERATION_DISABLE = 2,
} p300_bias_operation_t;

typedef enum {
    P300_BIAS_STATUS_OK = 0,
    P300_BIAS_STATUS_BUSY = 1,
    P300_BIAS_STATUS_INVALID_REQUEST = 2,
    P300_BIAS_STATUS_NO_TCP_STREAM = 3,
    P300_BIAS_STATUS_APPLY_FAILED = 4,
    P300_BIAS_STATUS_RESTORE_FAILED = 5,
    P300_BIAS_STATUS_INTERNAL_ERROR = 6,
} p300_bias_status_t;

typedef enum {
    P300_BIAS_PARSE_DROP = 0,
    P300_BIAS_PARSE_INVALID = 1,
    P300_BIAS_PARSE_OK = 2,
} p300_bias_parse_status_t;

typedef enum {
    P300_TX_OPERATION_QUERY = 0,
    P300_TX_OPERATION_SET = 1,
} p300_tx_operation_t;

typedef enum {
    P300_TX_STATUS_OK = 0,
    P300_TX_STATUS_BUSY = 1,
    P300_TX_STATUS_INVALID_REQUEST = 2,
    P300_TX_STATUS_NO_TCP_STREAM = 3,
    P300_TX_STATUS_INTERNAL_ERROR = 4,
} p300_tx_status_t;

typedef enum {
    P300_TX_PARSE_DROP = 0,
    P300_TX_PARSE_INVALID = 1,
    P300_TX_PARSE_OK = 2,
} p300_tx_parse_status_t;

enum {
    P300_IMPEDANCE_FLAG_DC_VALID = 1U << 0,
    P300_IMPEDANCE_FLAG_AC_VALID = 1U << 1,
    P300_IMPEDANCE_FLAG_BACKGROUND_SUBTRACTED = 1U << 2,
    P300_IMPEDANCE_FLAG_CLIPPED = 1U << 3,
    P300_IMPEDANCE_FLAG_CACHED = 1U << 4,
};

enum {
    P300_SELF_TEST_FLAG_CHANGED = 1U << 0,
    P300_SELF_TEST_FLAG_CACHED = 1U << 1,
};

enum {
    P300_BIAS_FLAG_CHANGED = 1U << 0,
    P300_BIAS_FLAG_CACHED = 1U << 1,
    P300_BIAS_FLAG_VERIFIED = 1U << 2,
    P300_BIAS_FLAG_POWER_CONTROL = 1U << 3,
};

enum {
    P300_TX_FLAG_CHANGED = 1U << 0,
    P300_TX_FLAG_CACHED = 1U << 1,
    P300_TX_FLAG_VERIFIED = 1U << 2,
};

typedef struct {
    uint32_t request_id;
    uint8_t channel_mask;
} p300_impedance_request_t;

typedef struct {
    uint32_t request_id;
    p300_impedance_status_t status;
    uint8_t tested_mask;
    uint8_t open_mask;
    uint8_t flags;
    uint16_t duration_ms;
    uint16_t sample_count;
    uint32_t nominal_ohm[P300_EEG_CHANNELS];
    uint32_t excitation_nv_peak[P300_EEG_CHANNELS];
} p300_impedance_result_t;

typedef struct {
    uint32_t request_id;
    uint8_t target_config_id;
} p300_self_test_request_t;

typedef struct {
    uint32_t request_id;
    p300_self_test_status_t status;
    uint8_t requested_config_id;
    uint8_t actual_config_id;
    uint8_t flags;
    uint32_t stream_id;
} p300_self_test_result_t;

typedef struct {
    uint32_t request_id;
    p300_bias_operation_t operation;
    uint8_t requested_mask;
} p300_bias_control_request_t;

typedef struct {
    uint32_t request_id;
    p300_bias_status_t status;
    uint8_t requested_mask;
    uint8_t actual_mask;
    uint8_t flags;
    uint32_t stream_id;
} p300_bias_control_result_t;

typedef struct {
    uint32_t request_id;
    p300_tx_operation_t operation;
    uint8_t requested_frames;
} p300_tx_control_request_t;

typedef struct {
    uint32_t request_id;
    p300_tx_status_t status;
    uint8_t requested_frames;
    uint8_t actual_frames;
    uint8_t flags;
    uint32_t stream_id;
} p300_tx_control_result_t;

typedef struct {
    uint32_t seq;
    uint64_t monotonic_us;
    int32_t channel[P300_EEG_CHANNELS];
} p300_sample_t;

typedef struct {
    uint64_t pc_us[P300_CLOCK_HISTORY];
    int64_t esp_minus_pc_us[P300_CLOCK_HISTORY];
    uint32_t rtt_us[P300_CLOCK_HISTORY];
    uint8_t count;
    uint8_t next;
    uint32_t min_rtt_us;
    int64_t offset_us;
    double scale;
    bool valid;
} p300_clock_model_t;

typedef struct {
    uint32_t event_id;
    uint8_t code;
    uint64_t pc_monotonic_us;
} p300_marker_event_t;

typedef struct {
    uint32_t sequence;
    uint64_t t1_pc_us;
    uint64_t t2_esp_us;
    uint64_t t3_esp_us;
    uint64_t t4_pc_us;
} p300_sync_measurement_t;

uint32_t p300_crc32c(const uint8_t *data, size_t len);
size_t p300_encode_eeg_batch(uint32_t stream_id,
                              uint16_t config_id,
                              uint64_t unix_offset_us,
                              const p300_sample_t samples[P300_EEG_BATCH_SAMPLES],
                              const uint8_t markers[P300_EEG_BATCH_SAMPLES],
                              uint8_t *frame,
                              size_t frame_size);
bool p300_clock_add_measurement(p300_clock_model_t *model,
                                uint64_t t1_pc_us,
                                uint64_t t2_esp_us,
                                uint64_t t3_esp_us,
                                uint64_t t4_pc_us);
bool p300_clock_is_valid(const p300_clock_model_t *model);
bool p300_clock_add_measurement_limits(p300_clock_model_t *model,
    uint64_t t1, uint64_t t2, uint64_t t3, uint64_t t4, uint32_t max_rtt, uint32_t slack);
uint64_t p300_clock_pc_to_esp(const p300_clock_model_t *model, uint64_t pc_us);
bool p300_find_nearest_sample(const p300_sample_t *samples,
                              size_t sample_count,
                              uint64_t target_us,
                              uint8_t *index,
                              uint32_t *error_us);
bool p300_parse_marker_packet(const uint8_t *packet,
                              size_t packet_size,
                              p300_marker_event_t *event);
size_t p300_build_sync_reply(uint32_t sequence,
                             uint64_t t1_pc_us,
                             uint64_t t2_esp_us,
                             uint64_t t3_esp_us,
                             uint8_t *packet,
                             size_t packet_size);
size_t p300_build_marker_ack(uint32_t event_id,
                             p300_marker_status_t status,
                             uint32_t sample_seq,
                             uint32_t error_us,
                             uint8_t *packet,
                             size_t packet_size);
bool p300_parse_sync_result_packet(const uint8_t *packet,
                                   size_t packet_size,
                                   p300_sync_measurement_t *measurement);
p300_impedance_parse_status_t p300_parse_impedance_request(
    const uint8_t *packet,
    size_t packet_size,
    p300_impedance_request_t *request);
size_t p300_build_impedance_result(const p300_impedance_result_t *result,
                                   uint8_t *packet,
                                   size_t packet_size);
p300_self_test_parse_status_t p300_parse_self_test_request(
    const uint8_t *packet,
    size_t packet_size,
    p300_self_test_request_t *request);
size_t p300_build_self_test_result(const p300_self_test_result_t *result,
                                   uint8_t *packet,
                                   size_t packet_size);
p300_bias_parse_status_t p300_parse_bias_control_request(
    const uint8_t *packet,
    size_t packet_size,
    p300_bias_control_request_t *request);
size_t p300_build_bias_control_result(const p300_bias_control_result_t *result,
                                      uint8_t *packet,
                                      size_t packet_size);
p300_tx_parse_status_t p300_parse_tx_control_request(
    const uint8_t *packet,
    size_t packet_size,
    p300_tx_control_request_t *request);
size_t p300_build_tx_control_result(const p300_tx_control_result_t *result,
                                    uint8_t *packet,
                                    size_t packet_size);

#ifdef __cplusplus
}
#endif
