#include "p300_stream.h"

#include <errno.h>
#include <inttypes.h>
#include <stdio.h>
#include <string.h>
#include <sys/time.h>

#include "eeg_acq.h"
#include "esp_log.h"
#include "esp_mac.h"
#include "esp_random.h"
#include "esp_timer.h"
#include "freertos/FreeRTOS.h"
#include "freertos/task.h"
#include "freertos/queue.h"
#include "runtime_config.h"
#include "uart_link.h"
#include "native_wifi.h"
#include "lwip/inet.h"
#include "lwip/sockets.h"
#include "lwip/tcp.h"
#include "p300_bias_state.h"
#include "p300_gain_state.h"
#include "p300_protocol.h"
#include "p300_tx_control.h"
#include "p300_tx_group.h"
#include "app_config.h"

static const char *TAG = "p300_stream";

#define P300_BATCH_FIFO_CAPACITY 8U
#define P300_PENDING_CAPACITY 8U
#define P300_DEDUP_CAPACITY 16U
#define P300_RECONNECT_MS 1000U
#define P300_PENDING_TIMEOUT_US 80000LL

typedef struct {
    p300_sample_t samples[P300_EEG_BATCH_SAMPLES];
    uint8_t markers[P300_EEG_BATCH_SAMPLES];
    uint64_t first_us;
    uint8_t config_id;
} p300_batch_t;

typedef struct {
    bool used;
    p300_marker_event_t event;
    uint64_t target_esp_us;
    int64_t received_us;
    struct sockaddr_in source;
    socklen_t source_len;
} pending_marker_t;

typedef struct {
    bool used;
    uint32_t event_id;
    p300_marker_status_t status;
    uint32_t sample_seq;
    uint32_t error_us;
} marker_dedup_t;

typedef struct {
    bool pending;
    bool active;
    p300_impedance_request_t request;
    int64_t received_us;
    struct sockaddr_in source;
    socklen_t source_len;
} pending_impedance_t;

typedef struct {
    bool pending;
    bool active;
    p300_self_test_request_t request;
    struct sockaddr_in source;
    socklen_t source_len;
} pending_self_test_t;

typedef struct {
    p300_bias_control_request_t request;
    struct sockaddr_in source;
    socklen_t source_len;
} active_bias_request_t;

typedef struct {
    p300_gain_control_request_t request;
    struct sockaddr_in source;
    socklen_t source_len;
} active_gain_request_t;

typedef struct {
    p300_tx_control_request_t request;
    struct sockaddr_in source;
    socklen_t source_len;
} active_tx_request_t;

static TaskHandle_t s_stream_task;
static TaskHandle_t s_marker_task;
static volatile bool s_stop_requested;
static int s_tcp_sock = -1;
static int s_marker_sock = -1;
static uint32_t s_stream_id;
static p300_batch_t s_batches[P300_BATCH_FIFO_CAPACITY];
static uint8_t s_batch_head;
static uint8_t s_batch_tail;
static uint8_t s_batch_count;
static pending_marker_t s_pending[P300_PENDING_CAPACITY];
static marker_dedup_t s_dedup[P300_DEDUP_CAPACITY];
static uint8_t s_dedup_next;
static p300_clock_model_t s_clock;
static pending_impedance_t s_impedance;
static p300_impedance_result_t s_impedance_cache;
static bool s_impedance_cache_valid;
static pending_self_test_t s_self_test;
static p300_self_test_result_t s_self_test_cache;
static bool s_self_test_cache_valid;
static p300_bias_state_t s_bias_state;
static struct sockaddr_in s_bias_source;
static socklen_t s_bias_source_len;
static p300_gain_state_t s_gain_state;
static struct sockaddr_in s_gain_source;
static socklen_t s_gain_source_len;
static p300_tx_control_state_t s_tx_control_state;
static struct sockaddr_in s_tx_control_source;
static socklen_t s_tx_control_source_len;
static volatile bool s_restore_normal_requested;
static bool s_ads_fault;
static bool s_fault_recovery_exhausted;
static int64_t s_clock_last_update_us;
static int64_t s_unix_offset_us;
static bool s_unix_ready;
static p300_stream_stats_t s_stats;
static p300_tx_group_t s_tx_group;
static portMUX_TYPE s_mux = portMUX_INITIALIZER_UNLOCKED;
typedef struct { uint32_t generation; size_t size; uint8_t packet[256]; } serial_control_t;
static QueueHandle_t s_controls;
static volatile int s_requested_mode = -1;
static volatile bool s_wifi_mode;
static bool s_serial_connected;
static volatile bool s_serial_paused;
static uint32_t s_serial_generation, s_serial_config_stream;
static volatile uint64_t s_pending_unix, s_pending_pc;

static bool data_connected(void)
{ return s_wifi_mode ? s_tcp_sock >= 0 : s_serial_connected && uart_link_active(); }

static int reply_packet(const struct sockaddr_in *source, socklen_t source_len,
                        const uint8_t *packet, size_t length)
{
    if (source->sin_family == AF_UNSPEC) {
        if (source->sin_addr.s_addr != uart_link_generation()) return -1;
        uint32_t id = length>=8 ? ((uint32_t)packet[4]<<24 | (uint32_t)packet[5]<<16 |
                                   (uint32_t)packet[6]<<8 | packet[7]) : 0;
        return uart_link_send(SERIAL_CONTROL_REPLY,id,packet,length) ? (int)length : -1;
    }
    if (s_marker_sock < 0) return -1;
    return sendto(s_marker_sock,packet,length,0,(const struct sockaddr *)source,source_len);
}

static uint32_t read_u32_be(const uint8_t *src)
{
    return ((uint32_t)src[0] << 24) | ((uint32_t)src[1] << 16) |
           ((uint32_t)src[2] << 8) | src[3];
}

static uint64_t read_u64_be(const uint8_t *src)
{
    uint64_t value = 0;
    for (uint8_t i = 0; i < 8; ++i) {
        value = (value << 8) | src[i];
    }
    return value;
}

static void put_u32_be(uint8_t *dst, uint32_t value)
{
    dst[0] = (uint8_t)(value >> 24);
    dst[1] = (uint8_t)(value >> 16);
    dst[2] = (uint8_t)(value >> 8);
    dst[3] = (uint8_t)value;
}

static bool valid_tcp_target(struct sockaddr_in *target)
{
    memset(target, 0, sizeof(*target));
    char host[16]; uint16_t port;
    if (!runtime_config_endpoint(host,&port)) return false;
    target->sin_family = AF_INET;
    target->sin_port = htons(port);
    return inet_pton(AF_INET, host, &target->sin_addr) == 1;
}

static bool is_transient_send_errno(int error)
{
    return error == EAGAIN || error == EWOULDBLOCK || error == ENOMEM;
}

static void close_tcp_socket(void)
{
    const bool was_open = s_tcp_sock >= 0;
    p300_tx_group_clear(&s_tx_group);
    if (s_tcp_sock >= 0) {
        shutdown(s_tcp_sock, 0);
        close(s_tcp_sock);
        s_tcp_sock = -1;
    }
    portENTER_CRITICAL(&s_mux);
    s_stats.connected = 0;
    p300_bias_state_cancel_pending(&s_bias_state);
    p300_gain_state_cancel_pending(&s_gain_state);
    p300_tx_control_state_cancel_pending(&s_tx_control_state);
    portEXIT_CRITICAL(&s_mux);
    if (was_open && eeg_acq_get_config_id() == EEG_ACQ_CONFIG_INTERNAL_SHORT) {
        s_restore_normal_requested = true;
    }
}

static bool source_is_tcp_target(const struct sockaddr_in *source)
{
    if (source && source->sin_family == AF_UNSPEC)
        return !s_wifi_mode && uart_link_active() && source->sin_addr.s_addr == uart_link_generation();
    struct in_addr target_addr = {0}; char host[16]; uint16_t port;
    return source != NULL && source->sin_family == AF_INET &&
           s_wifi_mode && runtime_config_endpoint(host,&port) &&
           inet_pton(AF_INET, host, &target_addr) == 1 &&
           source->sin_addr.s_addr == target_addr.s_addr;
}

static void renew_stream_id(void)
{
    uart_link_new_stream();
    const uint32_t previous = s_stream_id;
    do {
        s_stream_id = esp_random();
    } while (s_stream_id == 0U || s_stream_id == previous);
    portENTER_CRITICAL(&s_mux);
    s_stats.intentional_interrupts++;
    portEXIT_CRITICAL(&s_mux);
}

static bool send_all(const uint8_t *payload, size_t length)
{
    size_t sent = 0;
    uint8_t retries = 0;
    while (sent < length) {
        const int result = send(s_tcp_sock, payload + sent, length - sent, 0);
        if (result > 0) {
            sent += (size_t)result;
            retries = 0;
            continue;
        }
        if (result < 0 && is_transient_send_errno(errno) &&
            retries++ < P300_TCP_SEND_RETRY_COUNT) {
            vTaskDelay(pdMS_TO_TICKS(P300_TCP_SEND_RETRY_DELAY_MS));
            continue;
        }
        ESP_LOGW(TAG, "TCP send failed errno=%d sent=%u/%u", errno,
                 (unsigned)sent, (unsigned)length);
        close_tcp_socket();
        portENTER_CRITICAL(&s_mux);
        s_stats.tcp_fail++;
        portEXIT_CRITICAL(&s_mux);
        return false;
    }
    return true;
}

static bool flush_tx_group(void)
{
    if (p300_tx_group_empty(&s_tx_group)) {
        return true;
    }
    const uint8_t frame_count = p300_tx_group_frame_count(&s_tx_group);
    const size_t byte_count = p300_tx_group_size(&s_tx_group);
    if (!data_connected()) {
        p300_tx_group_clear(&s_tx_group);
        return false;
    }
    bool sent=true;
    if (s_wifi_mode) sent=send_all(p300_tx_group_data(&s_tx_group),byte_count);
    else for (uint8_t i=0;i<frame_count;i++) {
        const uint8_t *frame=p300_tx_group_data(&s_tx_group)+(size_t)i*P300_EEG_BATCH_BYTES;
        if (!uart_link_send(SERIAL_EEG,read_u32_be(frame+10),frame,P300_EEG_BATCH_BYTES)) sent=false;
    }
    if (sent) {
        portENTER_CRITICAL(&s_mux);
        s_stats.frames_sent += (uint32_t)frame_count * P300_EEG_BATCH_SAMPLES;
        portEXIT_CRITICAL(&s_mux);
    }
    p300_tx_group_clear(&s_tx_group);
    return sent;
}

static uint8_t active_tx_group_frames(void)
{
    uint8_t frames;
    portENTER_CRITICAL(&s_mux);
    frames = p300_tx_control_state_active_frames(&s_tx_control_state);
    portEXIT_CRITICAL(&s_mux);
    return frames;
}

static bool queue_tx_frame(const p300_batch_t *batch,
                           const uint8_t frame[P300_EEG_BATCH_BYTES],
                           int64_t now_us)
{
    const uint32_t start_seq = batch->samples[0].seq;
    if (!p300_tx_group_can_append(&s_tx_group, s_stream_id,
                                   batch->config_id, start_seq) &&
        !flush_tx_group()) {
        return false;
    }
    if (!p300_tx_group_append(&s_tx_group, frame, s_stream_id,
                              batch->config_id, start_seq, now_us)) {
        ESP_LOGE(TAG, "TCP TX group rejected a complete EEG frame");
        p300_tx_group_clear(&s_tx_group);
        return false;
    }
    return !p300_tx_group_full(&s_tx_group, active_tx_group_frames()) ||
           flush_tx_group();
}

static bool send_stream_config(void)
{
    uint8_t mac[6] = {0};
    esp_read_mac(mac, ESP_MAC_WIFI_STA);
    char config[768] = {0};
    const uint8_t tx_group_frames = active_tx_group_frames();
    const int length = snprintf(
        config, sizeof(config),
        "{\"type\":\"STREAM_CONFIG\",\"device_id\":\"P300_%02X%02X%02X%02X%02X%02X\","
        "\"firmware_version\":\"2.3.0\",\"stream_id\":%" PRIu32 ",\"config_id\":%u,"
        "\"sample_rate_hz\":%u,\"channel_mask\":255,\"vref_v\":4.5,\"pga_gain\":%u,\"gain_control\":true,\"gain_verified\":%s,"
        "\"reference\":\"ADS1299 internal reference\",\"bias\":\"%s\","
        "\"bias_mask\":%u,\"bias_verified\":%s,\"bias_power_control\":true,"
        "\"marker_codes\":{\"0\":\"none\",\"1\":\"p300\"},\"sync\":\"pc_four_timestamp\","
        "\"tx_group_frames\":%u,\"tx_group_period_ms\":%u}",
        mac[0], mac[1], mac[2], mac[3], mac[4], mac[5], s_stream_id,
        (unsigned)eeg_acq_get_config_id(),
        (unsigned)P300_SAMPLE_RATE_HZ,
        (unsigned)eeg_acq_get_gain(), eeg_acq_gain_is_verified() ? "true" : "false",
        !eeg_acq_normal_bias_is_verified() ? "unknown" :
            (eeg_acq_get_config_id() == EEG_ACQ_CONFIG_NORMAL &&
             eeg_acq_get_normal_bias_mask() != 0U ? "enabled" : "disabled"),
        (unsigned)(eeg_acq_get_config_id() == EEG_ACQ_CONFIG_NORMAL ?
                   eeg_acq_get_normal_bias_mask() : 0U),
        eeg_acq_normal_bias_is_verified() ? "true" : "false",
        (unsigned)tx_group_frames,
        (unsigned)(P300_EEG_BATCH_SAMPLES * 1000U *
                   tx_group_frames / P300_SAMPLE_RATE_HZ));
    if (length <= 0 || length >= (int)sizeof(config)) {
        return false;
    }
    uint8_t prefix[4];
    if (!s_wifi_mode) return uart_link_send(SERIAL_CONFIG,0,config,(size_t)length);
    put_u32_be(prefix, (uint32_t)length);
    return send_all(prefix, sizeof(prefix)) && send_all((const uint8_t *)config, (size_t)length);
}

static bool connect_tcp(const struct sockaddr_in *target)
{
    if (!native_wifi_is_connected() || !s_unix_ready) {
        return false;
    }
    s_tcp_sock = socket(AF_INET, SOCK_STREAM, IPPROTO_IP);
    if (s_tcp_sock < 0) {
        return false;
    }
    struct timeval timeout = {.tv_sec = 0, .tv_usec = P300_TCP_SOCKET_TIMEOUT_MS * 1000};
    setsockopt(s_tcp_sock, SOL_SOCKET, SO_SNDTIMEO, &timeout, sizeof(timeout));
    setsockopt(s_tcp_sock, SOL_SOCKET, SO_RCVTIMEO, &timeout, sizeof(timeout));
    int enabled = 1;
    setsockopt(s_tcp_sock, IPPROTO_TCP, TCP_NODELAY, &enabled, sizeof(enabled));
    if (connect(s_tcp_sock, (const struct sockaddr *)target, sizeof(*target)) != 0 || !send_stream_config()) {
        close_tcp_socket();
        return false;
    }
    portENTER_CRITICAL(&s_mux);
    s_stats.connected = 1;
    s_stats.reconnects++;
    portEXIT_CRITICAL(&s_mux);
    ESP_LOGI(TAG, "P300 TCP connected: %s:%u", inet_ntoa(target->sin_addr),
             (unsigned)ntohs(target->sin_port));
    return true;
}

static void queue_batch(const p300_batch_t *batch)
{
    portENTER_CRITICAL(&s_mux);
    if (s_batch_count == P300_BATCH_FIFO_CAPACITY) {
        s_batch_head = (uint8_t)((s_batch_head + 1U) % P300_BATCH_FIFO_CAPACITY);
        s_batch_count--;
        s_stats.dropped_no_connection += P300_EEG_BATCH_SAMPLES;
    }
    s_batches[s_batch_tail] = *batch;
    s_batch_tail = (uint8_t)((s_batch_tail + 1U) % P300_BATCH_FIFO_CAPACITY);
    s_batch_count++;
    if (s_batch_count > s_stats.max_buffered) {
        s_stats.max_buffered = s_batch_count;
    }
    portEXIT_CRITICAL(&s_mux);
}

static bool take_ready_batch(p300_batch_t *batch, int64_t now_us)
{
    bool ready = false;
    portENTER_CRITICAL(&s_mux);
    if (s_batch_count > 0 && now_us - (int64_t)s_batches[s_batch_head].first_us >=
            P300_HOLDBACK_MS * 1000LL) {
        *batch = s_batches[s_batch_head];
        s_batch_head = (uint8_t)((s_batch_head + 1U) % P300_BATCH_FIFO_CAPACITY);
        s_batch_count--;
        ready = true;
    }
    portEXIT_CRITICAL(&s_mux);
    return ready;
}

static void send_marker_ack(const pending_marker_t *pending,
                            p300_marker_status_t status,
                            uint32_t sample_seq,
                            uint32_t error_us)
{
    uint8_t packet[P300_MARKER_ACK_BYTES];
    if (pending == NULL) {
        return;
    }
    const size_t length = p300_build_marker_ack(pending->event.event_id, status, sample_seq,
                                                 error_us, packet, sizeof(packet));
    const int sent = reply_packet(&pending->source, pending->source_len, packet, length);
    portENTER_CRITICAL(&s_mux);
    if (sent == (int)length) {
        s_stats.udp_packets_sent++;
    } else {
        s_stats.udp_fail++;
    }
    portEXIT_CRITICAL(&s_mux);
}

static p300_impedance_result_t impedance_status_result(uint32_t request_id,
                                                       p300_impedance_status_t status)
{
    p300_impedance_result_t result = {
        .request_id = request_id,
        .status = status,
    };
    for (uint8_t channel = 0; channel < P300_EEG_CHANNELS; ++channel) {
        result.nominal_ohm[channel] = P300_IMPEDANCE_INVALID_VALUE;
        result.excitation_nv_peak[channel] = P300_IMPEDANCE_INVALID_VALUE;
    }
    return result;
}

static void send_impedance_result(const struct sockaddr_in *source,
                                  socklen_t source_len,
                                  const p300_impedance_result_t *result,
                                  bool cached)
{
    if (source == NULL || result == NULL) {
        return;
    }
    p300_impedance_result_t reply = *result;
    if (cached) {
        reply.flags |= P300_IMPEDANCE_FLAG_CACHED;
    }
    uint8_t packet[P300_IMPEDANCE_RESULT_BYTES];
    const size_t length = p300_build_impedance_result(&reply, packet, sizeof(packet));
    const int sent = reply_packet(source, source_len, packet, length);
    portENTER_CRITICAL(&s_mux);
    if (sent == (int)length) {
        s_stats.udp_packets_sent++;
    } else {
        s_stats.udp_fail++;
    }
    portEXIT_CRITICAL(&s_mux);
}

static p300_self_test_result_t self_test_status_result(
    uint32_t request_id,
    p300_self_test_status_t status,
    uint8_t requested_config_id)
{
    const p300_self_test_result_t result = {
        .request_id = request_id,
        .status = status,
        .requested_config_id = requested_config_id,
        .actual_config_id = eeg_acq_get_config_id(),
        .stream_id = s_stream_id,
    };
    return result;
}

static void send_self_test_result(const struct sockaddr_in *source,
                                  socklen_t source_len,
                                  const p300_self_test_result_t *result,
                                  bool cached)
{
    if (source == NULL || result == NULL) {
        return;
    }
    p300_self_test_result_t reply = *result;
    if (cached) {
        reply.flags |= P300_SELF_TEST_FLAG_CACHED;
    }
    uint8_t packet[P300_SELF_TEST_RESULT_BYTES];
    const size_t length = p300_build_self_test_result(&reply, packet, sizeof(packet));
    const int sent = reply_packet(source, source_len, packet, length);
    portENTER_CRITICAL(&s_mux);
    if (sent == (int)length) {
        s_stats.udp_packets_sent++;
    } else {
        s_stats.udp_fail++;
    }
    portEXIT_CRITICAL(&s_mux);
}

static p300_bias_control_result_t bias_status_result(
    uint32_t request_id,
    p300_bias_status_t status,
    uint8_t requested_mask)
{
    const bool verified = eeg_acq_normal_bias_is_verified();
    const p300_bias_control_result_t result = {
        .request_id = request_id,
        .status = status,
        .requested_mask = requested_mask,
        .actual_mask = verified ? eeg_acq_get_normal_bias_mask() : 0U,
        .flags = P300_BIAS_FLAG_POWER_CONTROL |
                 (verified ? P300_BIAS_FLAG_VERIFIED : 0U),
        .stream_id = verified ? s_stream_id : 0U,
    };
    return result;
}

static p300_gain_control_result_t gain_status_result(
    uint32_t request_id,
    p300_gain_status_t status,
    uint8_t requested_gain)
{
    const bool verified = eeg_acq_gain_is_verified();
    const p300_gain_control_result_t result = {
        .request_id = request_id,
        .status = status,
        .requested_gain = requested_gain,
        .actual_gain = verified ? eeg_acq_get_gain() : 0U,
        .flags = verified ? P300_GAIN_FLAG_VERIFIED : 0U,
        .stream_id = verified ? s_stream_id : 0U,
    };
    return result;
}

static void send_bias_result(const struct sockaddr_in *source,
                             socklen_t source_len,
                             const p300_bias_control_result_t *result)
{
    if (source == NULL || result == NULL) {
        return;
    }
    uint8_t packet[P300_BIAS_CONTROL_RESULT_BYTES];
    const size_t length = p300_build_bias_control_result(result, packet, sizeof(packet));
    const int sent = reply_packet(source, source_len, packet, length);
    portENTER_CRITICAL(&s_mux);
    if (sent == (int)length) {
        s_stats.udp_packets_sent++;
    } else {
        s_stats.udp_fail++;
    }
    portEXIT_CRITICAL(&s_mux);
}

static void send_gain_result(const struct sockaddr_in *source,
                             socklen_t source_len,
                             const p300_gain_control_result_t *result)
{
    if (source == NULL || result == NULL) {
        return;
    }
    uint8_t packet[P300_GAIN_CONTROL_RESULT_BYTES];
    const size_t length = p300_build_gain_control_result(result, packet, sizeof(packet));
    const int sent = reply_packet(source, source_len, packet, length);
    portENTER_CRITICAL(&s_mux);
    if (sent == (int)length) {
        s_stats.udp_packets_sent++;
    } else {
        s_stats.udp_fail++;
    }
    portEXIT_CRITICAL(&s_mux);
}

static p300_tx_control_result_t tx_control_status_result(
    uint32_t request_id,
    p300_tx_status_t status,
    uint8_t requested_frames)
{
    const uint8_t actual_frames = active_tx_group_frames();
    const p300_tx_control_result_t result = {
        .request_id = request_id,
        .status = status,
        .requested_frames = requested_frames,
        .actual_frames = actual_frames,
        .flags = P300_TX_FLAG_VERIFIED,
        .stream_id = data_connected() ? s_stream_id : 0U,
    };
    return result;
}

static void send_tx_control_result(const struct sockaddr_in *source,
                                   socklen_t source_len,
                                   const p300_tx_control_result_t *result)
{
    if (source == NULL || result == NULL) {
        return;
    }
    uint8_t packet[P300_TX_CONTROL_RESULT_BYTES];
    const size_t length =
        p300_build_tx_control_result(result, packet, sizeof(packet));
    const int sent = reply_packet(source, source_len, packet, length);
    portENTER_CRITICAL(&s_mux);
    if (sent == (int)length) {
        s_stats.udp_packets_sent++;
    } else {
        s_stats.udp_fail++;
    }
    portEXIT_CRITICAL(&s_mux);
}

static bool control_blocks_markers(void)
{
    bool blocked;
    portENTER_CRITICAL(&s_mux);
    blocked = s_impedance.active || s_self_test.active || s_self_test.pending ||
              s_bias_state.pending || s_bias_state.active ||
              s_gain_state.pending || s_gain_state.active;
    portEXIT_CRITICAL(&s_mux);
    return blocked || s_ads_fault || eeg_acq_is_settling() ||
           eeg_acq_get_config_id() == EEG_ACQ_CONFIG_INTERNAL_SHORT;
}

static bool dedup_lookup(uint32_t event_id, marker_dedup_t *result)
{
    for (uint8_t i = 0; i < P300_DEDUP_CAPACITY; ++i) {
        if (s_dedup[i].used && s_dedup[i].event_id == event_id) {
            *result = s_dedup[i];
            return true;
        }
    }
    return false;
}

static void dedup_store(uint32_t event_id, p300_marker_status_t status,
                        uint32_t sample_seq, uint32_t error_us)
{
    for (uint8_t i = 0; i < P300_DEDUP_CAPACITY; ++i) {
        if (s_dedup[i].used && s_dedup[i].event_id == event_id) {
            s_dedup[i].status = status;
            s_dedup[i].sample_seq = sample_seq;
            s_dedup[i].error_us = error_us;
            return;
        }
    }
    s_dedup[s_dedup_next] = (marker_dedup_t) {
        .used = true, .event_id = event_id, .status = status,
        .sample_seq = sample_seq, .error_us = error_us,
    };
    s_dedup_next = (uint8_t)((s_dedup_next + 1U) % P300_DEDUP_CAPACITY);
}

static void process_pending_markers(void)
{
    const int64_t now_us = esp_timer_get_time();
    const bool acquisition_paused = control_blocks_markers();
    for (uint8_t pending_index = 0; pending_index < P300_PENDING_CAPACITY; ++pending_index) {
        if (!s_pending[pending_index].used) {
            continue;
        }
        pending_marker_t *pending = &s_pending[pending_index];
        if (acquisition_paused) {
            send_marker_ack(pending, P300_MARKER_STATUS_ACQUISITION_PAUSED, 0, 0);
            dedup_store(pending->event.event_id,
                        P300_MARKER_STATUS_ACQUISITION_PAUSED, 0, 0);
            pending->used = false;
            continue;
        }
        uint8_t best_batch = 0;
        uint8_t best_sample = 0;
        uint32_t best_error = UINT32_MAX;
        uint32_t best_seq = 0;
        bool found = false;

        portENTER_CRITICAL(&s_mux);
        for (uint8_t offset = 0; offset < s_batch_count; ++offset) {
            const uint8_t slot = (uint8_t)((s_batch_head + offset) % P300_BATCH_FIFO_CAPACITY);
            uint8_t sample_index = 0;
            uint32_t error = 0;
            if (p300_find_nearest_sample(s_batches[slot].samples, P300_EEG_BATCH_SAMPLES,
                                         pending->target_esp_us, &sample_index, &error) &&
                error < best_error) {
                best_batch = slot;
                best_sample = sample_index;
                best_error = error;
                found = true;
            }
        }
        if (found && best_error <= P300_MARKER_MAX_ERROR_US) {
            s_batches[best_batch].markers[best_sample] = pending->event.code;
            best_seq = s_batches[best_batch].samples[best_sample].seq;
        }
        portEXIT_CRITICAL(&s_mux);

        if (found && best_error <= P300_MARKER_MAX_ERROR_US) {
            send_marker_ack(pending, P300_MARKER_STATUS_OK, best_seq, best_error);
            dedup_store(pending->event.event_id, P300_MARKER_STATUS_OK, best_seq, best_error);
            pending->used = false;
        } else if (now_us - pending->received_us > P300_PENDING_TIMEOUT_US) {
            send_marker_ack(pending, P300_MARKER_STATUS_EXPIRED, 0, 0);
            dedup_store(pending->event.event_id, P300_MARKER_STATUS_EXPIRED, 0, 0);
            pending->used = false;
        }
    }
}

static bool enqueue_marker(const p300_marker_event_t *event,
                           const struct sockaddr_in *source, socklen_t source_len)
{
    marker_dedup_t old = {0};
    if (dedup_lookup(event->event_id, &old)) {
        pending_marker_t reply = {.event = *event, .source = *source, .source_len = source_len};
        send_marker_ack(&reply, old.status, old.sample_seq, old.error_us);
        return true;
    }
    if (control_blocks_markers()) {
        pending_marker_t reply = {.event = *event, .source = *source, .source_len = source_len};
        send_marker_ack(&reply, P300_MARKER_STATUS_ACQUISITION_PAUSED, 0, 0);
        dedup_store(event->event_id, P300_MARKER_STATUS_ACQUISITION_PAUSED, 0, 0);
        return false;
    }
    for (uint8_t i = 0; i < P300_PENDING_CAPACITY; ++i) {
        if (s_pending[i].used && s_pending[i].event.event_id == event->event_id) {
            s_pending[i].source = *source;
            s_pending[i].source_len = source_len;
            return true;
        }
    }
    if (event->code != 1U) {
        pending_marker_t reply = {.event = *event, .source = *source, .source_len = source_len};
        send_marker_ack(&reply, P300_MARKER_STATUS_INVALID_CODE, 0, 0);
        dedup_store(event->event_id, P300_MARKER_STATUS_INVALID_CODE, 0, 0);
        return false;
    }
    const p300_clock_model_t clock = s_clock;
    const bool current = clock.valid &&
                         esp_timer_get_time() - s_clock_last_update_us <= P300_SYNC_MAX_AGE_US;
    if (!current) {
        pending_marker_t reply = {.event = *event, .source = *source, .source_len = source_len};
        send_marker_ack(&reply, P300_MARKER_STATUS_UNSYNCED, 0, 0);
        return false;
    }
    for (uint8_t i = 0; i < P300_PENDING_CAPACITY; ++i) {
        if (!s_pending[i].used) {
            s_pending[i] = (pending_marker_t) {
                .used = true, .event = *event,
                .target_esp_us = p300_clock_pc_to_esp(&clock, event->pc_monotonic_us),
                .received_us = esp_timer_get_time(), .source = *source, .source_len = source_len,
            };
            return true;
        }
    }
    pending_marker_t reply = {.event = *event, .source = *source, .source_len = source_len};
    send_marker_ack(&reply, P300_MARKER_STATUS_QUEUE_FULL, 0, 0);
    return false;
}

static void handle_bias_control_request(const uint8_t *packet,
                                        size_t packet_size,
                                        const struct sockaddr_in *source,
                                        socklen_t source_len)
{
    p300_bias_control_request_t request = {0};
    const p300_bias_parse_status_t parse_status =
        p300_parse_bias_control_request(packet, packet_size, &request);
    if (parse_status == P300_BIAS_PARSE_DROP) {
        return;
    }

    const bool tcp_authorized = data_connected() && source_is_tcp_target(source);
    if (parse_status == P300_BIAS_PARSE_INVALID) {
        const p300_bias_control_result_t invalid = bias_status_result(
            request.request_id,
            tcp_authorized ? P300_BIAS_STATUS_INVALID_REQUEST
                           : P300_BIAS_STATUS_NO_TCP_STREAM,
            request.requested_mask);
        send_bias_result(source, source_len, &invalid);
        return;
    }

    p300_bias_control_result_t reply = {0};
    bool cached = false;
    p300_bias_action_t action;
    portENTER_CRITICAL(&s_mux);
    if (!s_bias_state.fault) {
        s_bias_state.normal_mask = eeg_acq_get_normal_bias_mask();
        s_bias_state.verified = eeg_acq_normal_bias_is_verified();
    }
    const bool control_busy = s_gain_state.pending || s_gain_state.active ||
                              s_impedance.active || s_impedance.pending ||
                              s_self_test.active || s_self_test.pending ||
                              s_tx_control_state.pending ||
                              s_tx_control_state.active ||
                              eeg_acq_is_settling() ||
                              eeg_acq_get_config_id() != EEG_ACQ_CONFIG_NORMAL;
    action = p300_bias_state_handle(&s_bias_state, &request, tcp_authorized,
                                    control_busy, s_stream_id, &reply, &cached);
    if (action == P300_BIAS_ACTION_QUEUED || action == P300_BIAS_ACTION_WAIT) {
        s_bias_source = *source;
        s_bias_source_len = source_len;
    }
    portEXIT_CRITICAL(&s_mux);

    if (action == P300_BIAS_ACTION_REPLY) {
        send_bias_result(source, source_len, &reply);
    }
}

static void handle_gain_control_request(const uint8_t *packet,
                                        size_t packet_size,
                                        const struct sockaddr_in *source,
                                        socklen_t source_len)
{
    p300_gain_control_request_t request = {0};
    const p300_gain_parse_status_t parse_status =
        p300_parse_gain_control_request(packet, packet_size, &request);
    if (parse_status == P300_GAIN_PARSE_DROP) {
        return;
    }

    const bool tcp_authorized = data_connected() && source_is_tcp_target(source);
    if (parse_status == P300_GAIN_PARSE_INVALID) {
        const p300_gain_control_result_t invalid = gain_status_result(
            request.request_id,
            tcp_authorized ? P300_GAIN_STATUS_INVALID_REQUEST
                           : P300_GAIN_STATUS_NO_TCP_STREAM,
            request.requested_gain);
        send_gain_result(source, source_len, &invalid);
        return;
    }

    p300_gain_control_result_t reply = {0};
    bool cached = false;
    p300_gain_action_t action;
    portENTER_CRITICAL(&s_mux);
    if (!s_gain_state.fault) {
        s_gain_state.normal_gain = eeg_acq_get_normal_gain();
        s_gain_state.verified = eeg_acq_gain_is_verified() &&
            eeg_acq_get_config_id() == EEG_ACQ_CONFIG_NORMAL &&
            !s_impedance.active && !s_self_test.active;
    }
    const bool control_busy = s_bias_state.pending || s_bias_state.active ||
                              s_impedance.active || s_impedance.pending ||
                              s_self_test.active || s_self_test.pending ||
                              s_tx_control_state.pending ||
                              s_tx_control_state.active ||
                              eeg_acq_is_settling() ||
                              eeg_acq_get_config_id() != EEG_ACQ_CONFIG_NORMAL;
    action = p300_gain_state_handle(&s_gain_state, &request, tcp_authorized,
                                    control_busy, s_stream_id, &reply, &cached);
    if (action == P300_GAIN_ACTION_QUEUED || action == P300_GAIN_ACTION_WAIT) {
        s_gain_source = *source;
        s_gain_source_len = source_len;
    }
    portEXIT_CRITICAL(&s_mux);

    if (action == P300_GAIN_ACTION_REPLY) {
        send_gain_result(source, source_len, &reply);
    }
}

static void handle_tx_control_request(const uint8_t *packet,
                                      size_t packet_size,
                                      const struct sockaddr_in *source,
                                      socklen_t source_len)
{
    p300_tx_control_request_t request = {0};
    const p300_tx_parse_status_t parse_status =
        p300_parse_tx_control_request(packet, packet_size, &request);
    if (parse_status == P300_TX_PARSE_DROP) {
        return;
    }

    const bool tcp_authorized =
        data_connected() && source_is_tcp_target(source);
    if (parse_status == P300_TX_PARSE_INVALID) {
        const p300_tx_control_result_t invalid = tx_control_status_result(
            request.request_id,
            tcp_authorized ? P300_TX_STATUS_INVALID_REQUEST
                           : P300_TX_STATUS_NO_TCP_STREAM,
            request.requested_frames);
        send_tx_control_result(source, source_len, &invalid);
        return;
    }

    p300_tx_control_result_t reply = {0};
    bool cached = false;
    p300_tx_action_t action;
    portENTER_CRITICAL(&s_mux);
    const bool control_busy =
        s_impedance.active || s_impedance.pending ||
        s_self_test.active || s_self_test.pending ||
        s_bias_state.pending || s_bias_state.active ||
              s_gain_state.pending || s_gain_state.active ||
        s_ads_fault || eeg_acq_is_settling();
    action = p300_tx_control_state_handle(
        &s_tx_control_state, &request, tcp_authorized, control_busy,
        s_stream_id, &reply, &cached);
    if (action == P300_TX_ACTION_QUEUED ||
        action == P300_TX_ACTION_WAIT) {
        s_tx_control_source = *source;
        s_tx_control_source_len = source_len;
    }
    portEXIT_CRITICAL(&s_mux);

    if (action == P300_TX_ACTION_REPLY) {
        send_tx_control_result(source, source_len, &reply);
    }
}

static void handle_impedance_request(const uint8_t *packet,
                                     size_t packet_size,
                                     const struct sockaddr_in *source,
                                     socklen_t source_len)
{
    p300_impedance_request_t request = {0};
    const p300_impedance_parse_status_t parse_status =
        p300_parse_impedance_request(packet, packet_size, &request);
    if (parse_status == P300_IMPEDANCE_PARSE_DROP) {
        return;
    }
    if (parse_status == P300_IMPEDANCE_PARSE_INVALID) {
        const p300_impedance_result_t invalid = impedance_status_result(
            request.request_id, P300_IMPEDANCE_STATUS_INVALID_REQUEST);
        send_impedance_result(source, source_len, &invalid, false);
        return;
    }

    bool send_busy = false;
    bool send_cached = false;
    p300_impedance_result_t immediate = {0};
    portENTER_CRITICAL(&s_mux);
    s_stats.impedance_requests++;
    if (s_impedance.active || s_impedance.pending) {
        if (s_impedance.request.request_id == request.request_id) {
            s_impedance.source = *source;
            s_impedance.source_len = source_len;
        } else {
            immediate = impedance_status_result(request.request_id,
                                                P300_IMPEDANCE_STATUS_BUSY);
            send_busy = true;
        }
    } else if (s_self_test.active || s_self_test.pending || s_ads_fault ||
               s_bias_state.pending || s_bias_state.active ||
              s_gain_state.pending || s_gain_state.active ||
               s_tx_control_state.pending || s_tx_control_state.active ||
               eeg_acq_is_settling() ||
               eeg_acq_get_config_id() != EEG_ACQ_CONFIG_NORMAL) {
        immediate = impedance_status_result(request.request_id,
                                            P300_IMPEDANCE_STATUS_BUSY);
        send_busy = true;
    } else if (s_impedance_cache_valid &&
               s_impedance_cache.request_id == request.request_id) {
        immediate = s_impedance_cache;
        send_cached = true;
    } else {
        s_impedance.pending = true;
        s_impedance.request = request;
        s_impedance.received_us = esp_timer_get_time();
        s_impedance.source = *source;
        s_impedance.source_len = source_len;
    }
    portEXIT_CRITICAL(&s_mux);

    if (send_busy) {
        send_impedance_result(source, source_len, &immediate, false);
    } else if (send_cached) {
        send_impedance_result(source, source_len, &immediate, true);
    }
}

static bool take_impedance_request(int64_t now_us, pending_impedance_t *request)
{
    bool ready = false;
    portENTER_CRITICAL(&s_mux);
    if (s_impedance.pending && !s_impedance.active &&
        now_us - s_impedance.received_us >= P300_HOLDBACK_MS * 1000LL) {
        s_impedance.pending = false;
        s_impedance.active = true;
        s_stats.impedance_active = 1;
        *request = s_impedance;
        ready = true;
    }
    portEXIT_CRITICAL(&s_mux);
    return ready;
}

static void clear_batch_fifo(void)
{
    portENTER_CRITICAL(&s_mux);
    s_batch_head = 0;
    s_batch_tail = 0;
    s_batch_count = 0;
    memset(s_batches, 0, sizeof(s_batches));
    portEXIT_CRITICAL(&s_mux);
}

static p300_impedance_result_t run_impedance_measurement(const pending_impedance_t *request)
{
    p300_impedance_result_t result = impedance_status_result(
        request->request.request_id, P300_IMPEDANCE_STATUS_INTERNAL_ERROR);
    eeg_acq_impedance_result_t measured = {0};
    const esp_err_t err = eeg_acq_measure_impedance(request->request.channel_mask, &measured);
    if (err != ESP_OK) {
        result.status = err == ESP_ERR_INVALID_ARG ? P300_IMPEDANCE_STATUS_INVALID_REQUEST
                                                   : P300_IMPEDANCE_STATUS_INTERNAL_ERROR;
        return result;
    }

    result.status = (p300_impedance_status_t)measured.status;
    result.tested_mask = measured.tested_mask;
    result.open_mask = measured.open_mask;
    result.flags = measured.flags;
    result.duration_ms = measured.duration_ms;
    result.sample_count = measured.sample_count;
    memcpy(result.nominal_ohm, measured.nominal_ohm, sizeof(result.nominal_ohm));
    memcpy(result.excitation_nv_peak, measured.excitation_nv_peak,
           sizeof(result.excitation_nv_peak));

    if (measured.tested_mask != 0U &&
        measured.status != EEG_ACQ_IMPEDANCE_STATUS_RESTORE_FAILED) {
        renew_stream_id();
    }
    return result;
}

static void complete_impedance_request(const pending_impedance_t *request,
                                       const p300_impedance_result_t *result)
{
    portENTER_CRITICAL(&s_mux);
    s_impedance_cache = *result;
    s_impedance_cache_valid = true;
    s_impedance.active = false;
    s_stats.impedance_active = 0;
    s_stats.impedance_runs++;
    portEXIT_CRITICAL(&s_mux);
    send_impedance_result(&request->source, request->source_len, result, false);
}

static void handle_self_test_request(const uint8_t *packet,
                                     size_t packet_size,
                                     const struct sockaddr_in *source,
                                     socklen_t source_len)
{
    if (!source_is_tcp_target(source)) {
        return;
    }

    p300_self_test_request_t request = {0};
    const p300_self_test_parse_status_t parse_status =
        p300_parse_self_test_request(packet, packet_size, &request);
    if (parse_status == P300_SELF_TEST_PARSE_DROP) {
        return;
    }
    if (parse_status == P300_SELF_TEST_PARSE_INVALID) {
        const p300_self_test_result_t invalid = self_test_status_result(
            request.request_id,
            P300_SELF_TEST_STATUS_INVALID_REQUEST,
            request.target_config_id);
        send_self_test_result(source, source_len, &invalid, false);
        return;
    }
    if (!data_connected()) {
        const p300_self_test_result_t unavailable = self_test_status_result(
            request.request_id,
            P300_SELF_TEST_STATUS_NO_TCP_STREAM,
            request.target_config_id);
        send_self_test_result(source, source_len, &unavailable, false);
        return;
    }

    bool send_busy = false;
    bool send_cached = false;
    p300_self_test_result_t immediate = {0};
    portENTER_CRITICAL(&s_mux);
    if (s_self_test.active || s_self_test.pending) {
        if (s_self_test.request.request_id == request.request_id) {
            s_self_test.source = *source;
            s_self_test.source_len = source_len;
        } else {
            immediate = self_test_status_result(request.request_id,
                                                P300_SELF_TEST_STATUS_BUSY,
                                                request.target_config_id);
            send_busy = true;
        }
    } else if (s_self_test_cache_valid &&
               s_self_test_cache.request_id == request.request_id) {
        immediate = s_self_test_cache;
        send_cached = true;
    } else if (s_impedance.active || s_impedance.pending || s_ads_fault ||
               s_bias_state.pending || s_bias_state.active ||
              s_gain_state.pending || s_gain_state.active ||
               s_tx_control_state.pending || s_tx_control_state.active ||
               eeg_acq_is_settling()) {
        immediate = self_test_status_result(
            request.request_id,
            s_ads_fault ? P300_SELF_TEST_STATUS_INTERNAL_ERROR
                        : P300_SELF_TEST_STATUS_BUSY,
            request.target_config_id);
        send_busy = true;
    } else {
        s_self_test.pending = true;
        s_self_test.request = request;
        s_self_test.source = *source;
        s_self_test.source_len = source_len;
    }
    portEXIT_CRITICAL(&s_mux);

    if (send_busy) {
        send_self_test_result(source, source_len, &immediate, false);
    } else if (send_cached) {
        send_self_test_result(source, source_len, &immediate, true);
    }
}

static bool take_self_test_request(pending_self_test_t *request)
{
    bool ready = false;
    portENTER_CRITICAL(&s_mux);
    if (s_self_test.pending && !s_self_test.active) {
        s_self_test.pending = false;
        s_self_test.active = true;
        *request = s_self_test;
        ready = true;
    }
    portEXIT_CRITICAL(&s_mux);
    return ready;
}

static bool take_bias_request(active_bias_request_t *request)
{
    bool ready = false;
    portENTER_CRITICAL(&s_mux);
    if (!s_impedance.active && !s_impedance.pending &&
        !s_self_test.active && !s_self_test.pending && !s_ads_fault &&
        !s_tx_control_state.pending && !s_tx_control_state.active &&
        !s_gain_state.pending && !s_gain_state.active &&
        p300_bias_state_take_pending(&s_bias_state, &request->request)) {
        request->source = s_bias_source;
        request->source_len = s_bias_source_len;
        ready = true;
    }
    portEXIT_CRITICAL(&s_mux);
    return ready;
}

static bool take_gain_request(active_gain_request_t *request)
{
    bool ready = false;
    portENTER_CRITICAL(&s_mux);
    if (!s_impedance.active && !s_impedance.pending &&
        !s_self_test.active && !s_self_test.pending && !s_ads_fault &&
        !s_tx_control_state.pending && !s_tx_control_state.active &&
        !s_bias_state.pending && !s_bias_state.active &&
        p300_gain_state_take_pending(&s_gain_state, &request->request)) {
        request->source = s_gain_source;
        request->source_len = s_gain_source_len;
        ready = true;
    }
    portEXIT_CRITICAL(&s_mux);
    return ready;
}

static bool take_tx_control_request(active_tx_request_t *request)
{
    bool ready = false;
    portENTER_CRITICAL(&s_mux);
    if (!s_impedance.active && !s_impedance.pending &&
        !s_self_test.active && !s_self_test.pending &&
        !s_bias_state.pending && !s_bias_state.active &&
        !s_gain_state.pending && !s_gain_state.active &&
        !s_ads_fault &&
        p300_tx_control_state_take_pending(&s_tx_control_state,
                                            &request->request)) {
        request->source = s_tx_control_source;
        request->source_len = s_tx_control_source_len;
        ready = true;
    }
    portEXIT_CRITICAL(&s_mux);
    return ready;
}

static void run_and_complete_tx_control_request(
    const active_tx_request_t *request)
{
    const uint8_t old_frames = active_tx_group_frames();
    const bool flushed = data_connected() && flush_tx_group();
    p300_tx_control_result_t result = {0};
    portENTER_CRITICAL(&s_mux);
    if (flushed) {
        p300_tx_control_state_complete(
            &s_tx_control_state, P300_TX_STATUS_OK,
            request->request.requested_frames, s_stream_id,
            true, true, &result);
    } else {
        p300_tx_control_state_complete(
            &s_tx_control_state, P300_TX_STATUS_NO_TCP_STREAM,
            old_frames, 0U, true, false, &result);
    }
    portEXIT_CRITICAL(&s_mux);
    send_tx_control_result(&request->source, request->source_len, &result);
}

static p300_bias_status_t map_bias_switch_status(
    eeg_acq_bias_switch_status_t status)
{
    switch (status) {
    case EEG_ACQ_BIAS_SWITCH_OK:
        return P300_BIAS_STATUS_OK;
    case EEG_ACQ_BIAS_SWITCH_INVALID_REQUEST:
        return P300_BIAS_STATUS_INVALID_REQUEST;
    case EEG_ACQ_BIAS_SWITCH_PAUSE_TIMEOUT:
    case EEG_ACQ_BIAS_SWITCH_APPLY_FAILED:
        return P300_BIAS_STATUS_APPLY_FAILED;
    case EEG_ACQ_BIAS_SWITCH_RESTORE_FAILED:
        return P300_BIAS_STATUS_RESTORE_FAILED;
    default:
        return P300_BIAS_STATUS_INTERNAL_ERROR;
    }
}

static void run_and_complete_bias_request(const active_bias_request_t *request)
{
    const eeg_acq_bias_switch_status_t switch_status =
        eeg_acq_set_normal_bias_mask(request->request.requested_mask);
    const p300_bias_status_t status = map_bias_switch_status(switch_status);
    if (status == P300_BIAS_STATUS_OK || status == P300_BIAS_STATUS_APPLY_FAILED) {
        renew_stream_id();
    }

    const bool verified = status != P300_BIAS_STATUS_RESTORE_FAILED &&
                          eeg_acq_normal_bias_is_verified();
    p300_bias_control_result_t result = {0};
    portENTER_CRITICAL(&s_mux);
    p300_bias_state_complete(&s_bias_state, status,
                             eeg_acq_get_normal_bias_mask(),
                             status == P300_BIAS_STATUS_RESTORE_FAILED ? 0U : s_stream_id,
                             verified, &result);
    if (status == P300_BIAS_STATUS_RESTORE_FAILED) {
        s_ads_fault = true;
        s_fault_recovery_exhausted = true;
    }
    portEXIT_CRITICAL(&s_mux);

    send_bias_result(&request->source, request->source_len, &result);
    if (status == P300_BIAS_STATUS_OK || status == P300_BIAS_STATUS_APPLY_FAILED) {
        /* TCP STREAM_CONFIG is connection-scoped; reconnect to publish the new
         * verified BIAS state before any next segment. USB uses typed CONFIG. */
        if (s_wifi_mode) {
            close_tcp_socket();
        } else {
            s_serial_connected = false;
        }
    } else if (status == P300_BIAS_STATUS_RESTORE_FAILED) {
        close_tcp_socket();
    }
}

static void run_and_complete_gain_request(const active_gain_request_t *request)
{
    const eeg_acq_gain_switch_status_t switch_status =
        eeg_acq_set_normal_gain(request->request.requested_gain);
    const p300_gain_status_t status = (p300_gain_status_t)switch_status;
    if (status == P300_GAIN_STATUS_OK || status == P300_GAIN_STATUS_APPLY_FAILED) {
        renew_stream_id();
    }

    const bool verified = status != P300_GAIN_STATUS_RESTORE_FAILED &&
                          eeg_acq_gain_is_verified();
    p300_gain_control_result_t result = {0};
    portENTER_CRITICAL(&s_mux);
    p300_gain_state_complete(&s_gain_state, status,
                             eeg_acq_get_normal_gain(),
                             status == P300_GAIN_STATUS_RESTORE_FAILED ? 0U : s_stream_id,
                             verified, &result);
    if (status == P300_GAIN_STATUS_RESTORE_FAILED) {
        s_ads_fault = true;
        s_fault_recovery_exhausted = true;
    }
    portEXIT_CRITICAL(&s_mux);

    send_gain_result(&request->source, request->source_len, &result);
    if (status == P300_GAIN_STATUS_OK || status == P300_GAIN_STATUS_APPLY_FAILED) {
        /* TCP STREAM_CONFIG is connection-scoped; reconnect to publish the new
         * verified gain and scale before any next segment. USB uses typed CONFIG. */
        if (s_wifi_mode) {
            close_tcp_socket();
        } else {
            s_serial_connected = false;
        }
    } else if (status == P300_GAIN_STATUS_RESTORE_FAILED) {
        s_serial_connected = false;
        close_tcp_socket();
    }
}

static p300_self_test_status_t map_mode_switch_status(
    eeg_acq_mode_switch_status_t status)
{
    switch (status) {
    case EEG_ACQ_MODE_SWITCH_OK:
        return P300_SELF_TEST_STATUS_OK;
    case EEG_ACQ_MODE_SWITCH_INVALID_REQUEST:
        return P300_SELF_TEST_STATUS_INVALID_REQUEST;
    case EEG_ACQ_MODE_SWITCH_PAUSE_TIMEOUT:
        return P300_SELF_TEST_STATUS_PAUSE_TIMEOUT;
    case EEG_ACQ_MODE_SWITCH_CONFIG_FAILED:
        return P300_SELF_TEST_STATUS_CONFIG_FAILED;
    case EEG_ACQ_MODE_SWITCH_RESTORE_FAILED:
        return P300_SELF_TEST_STATUS_RESTORE_FAILED;
    default:
        return P300_SELF_TEST_STATUS_INTERNAL_ERROR;
    }
}

static bool recover_normal_config(void)
{
    for (uint8_t attempt = 0; attempt < 3U; ++attempt) {
        vTaskDelay(pdMS_TO_TICKS(1000));
        ESP_LOGW(TAG, "ADS1299 NORMAL fault recovery attempt %u/3",
                 (unsigned)(attempt + 1U));
        if (eeg_acq_set_config_id(EEG_ACQ_CONFIG_NORMAL) == EEG_ACQ_MODE_SWITCH_OK &&
            eeg_acq_get_config_id() == EEG_ACQ_CONFIG_NORMAL &&
            eeg_acq_normal_bias_is_verified()) {
            s_ads_fault = false;
            s_fault_recovery_exhausted = false;
            portENTER_CRITICAL(&s_mux);
            p300_gain_state_clear_fault(&s_gain_state, eeg_acq_get_normal_gain(), eeg_acq_gain_is_verified());
            p300_bias_state_clear_fault(&s_bias_state,
                                        eeg_acq_get_normal_bias_mask(), true);
            portEXIT_CRITICAL(&s_mux);
            renew_stream_id();
            return true;
        }
    }
    s_ads_fault = true;
    s_fault_recovery_exhausted = true;
    ESP_LOGE(TAG, "ADS1299 recovery exhausted; TCP sample streaming halted until reboot");
    return false;
}

static p300_self_test_result_t run_self_test_switch(
    const pending_self_test_t *request)
{
    const uint8_t previous_config_id = eeg_acq_get_config_id();
    const eeg_acq_mode_switch_status_t switch_status =
        eeg_acq_set_config_id(request->request.target_config_id);
    p300_self_test_result_t result = self_test_status_result(
        request->request.request_id,
        map_mode_switch_status(switch_status),
        request->request.target_config_id);

    if (switch_status == EEG_ACQ_MODE_SWITCH_RESTORE_FAILED) {
        recover_normal_config();
    }
    result.actual_config_id = eeg_acq_get_config_id();
    if (switch_status == EEG_ACQ_MODE_SWITCH_OK &&
        result.actual_config_id != previous_config_id) {
        renew_stream_id();
        result.flags |= P300_SELF_TEST_FLAG_CHANGED;
    } else if (switch_status == EEG_ACQ_MODE_SWITCH_CONFIG_FAILED) {
        renew_stream_id();
    }
    if (result.actual_config_id != previous_config_id) {
        result.flags |= P300_SELF_TEST_FLAG_CHANGED;
    }
    result.stream_id = s_stream_id;
    return result;
}

static void complete_self_test_request(const pending_self_test_t *request,
                                       const p300_self_test_result_t *result)
{
    portENTER_CRITICAL(&s_mux);
    s_self_test_cache = *result;
    s_self_test_cache_valid = true;
    s_self_test.active = false;
    portEXIT_CRITICAL(&s_mux);
    send_self_test_result(&request->source, request->source_len, result, false);
    if (result->status == P300_SELF_TEST_STATUS_OK ||
        result->status == P300_SELF_TEST_STATUS_CONFIG_FAILED) {
        /* The diagnostic gain is 12 even when NORMAL uses another gain.
         * Publish that scale before any diagnostic samples. This reconnect is
         * intentional and must not undo the requested INTERNAL_SHORT mode. */
        if (s_wifi_mode) {
            close_tcp_socket();
            s_restore_normal_requested = false;
        } else {
            s_serial_connected = false;
        }
    }
}

static void restore_normal_after_disconnect(void)
{
    s_restore_normal_requested = false;
    if (eeg_acq_get_config_id() == EEG_ACQ_CONFIG_NORMAL) {
        return;
    }
    clear_batch_fifo();
    ESP_LOGW(TAG, "TCP disconnected during internal short; restoring NORMAL");
    const eeg_acq_mode_switch_status_t status =
        eeg_acq_set_config_id(EEG_ACQ_CONFIG_NORMAL);
    if (status == EEG_ACQ_MODE_SWITCH_OK) {
        renew_stream_id();
    } else if (status == EEG_ACQ_MODE_SWITCH_RESTORE_FAILED) {
        recover_normal_config();
    } else {
        ESP_LOGE(TAG, "disconnect NORMAL restore failed: status=%u", (unsigned)status);
        recover_normal_config();
    }
}

static void dispatch_control(const uint8_t *packet, size_t received,
                             const struct sockaddr_in *source, socklen_t source_len)
{
    if (received<4 || packet[0]!=0xa5 || packet[1]!=0x51 || packet[2]!=1 ||
        !source_is_tcp_target(source)) return;
    if (packet[3]==P300_UDP_TYPE_SYNC_REQ && received==16) {
        uint8_t reply[P300_SYNC_REPLY_BYTES];
        uint64_t t2=esp_timer_get_time();
        size_t size=p300_build_sync_reply(read_u32_be(packet+4),read_u64_be(packet+8),
                                         t2,esp_timer_get_time(),reply,sizeof(reply));
        reply_packet(source,source_len,reply,size);
    } else if (packet[3]==P300_UDP_TYPE_SYNC_RESULT) {
        p300_sync_measurement_t m={0};
        if (p300_parse_sync_result_packet(packet,received,&m)) {
            portENTER_CRITICAL(&s_mux);
            if (s_clock_last_update_us>0 && esp_timer_get_time()-s_clock_last_update_us>P300_SYNC_MAX_AGE_US)
                memset(&s_clock,0,sizeof(s_clock));
            bool serial=source->sin_family==AF_UNSPEC;
            if (p300_clock_add_measurement_limits(&s_clock,m.t1_pc_us,m.t2_esp_us,m.t3_esp_us,m.t4_pc_us,
                serial?50000U:P300_SYNC_MAX_RTT_US, serial?5000U:P300_SYNC_MIN_RTT_SLACK_US))
                s_clock_last_update_us=esp_timer_get_time();
            portEXIT_CRITICAL(&s_mux);
        }
    } else if (packet[3]==P300_UDP_TYPE_TIME_ANCHOR && received==28 && p300_crc32c(packet,24)==read_u32_be(packet+24)) {
        p300_stream_set_unix_time(read_u64_be(packet+8),read_u64_be(packet+16));
    } else if (packet[3]==P300_UDP_TYPE_MARKER) {
        p300_marker_event_t event={0};
        if (p300_parse_marker_packet(packet,received,&event)) enqueue_marker(&event,source,source_len);
    } else if (packet[3]==P300_UDP_TYPE_IMPEDANCE_REQUEST) handle_impedance_request(packet,received,source,source_len);
    else if (packet[3]==P300_UDP_TYPE_SELF_TEST_REQUEST) handle_self_test_request(packet,received,source,source_len);
    else if (packet[3]==P300_UDP_TYPE_GAIN_CONTROL_REQUEST) handle_gain_control_request(packet,received,source,source_len);
    else if (packet[3]==P300_UDP_TYPE_BIAS_CONTROL_REQUEST) handle_bias_control_request(packet,received,source,source_len);
    else if (packet[3]==P300_UDP_TYPE_TX_CONTROL_REQUEST) handle_tx_control_request(packet,received,source,source_len);
}

static void marker_task(void *arg)
{
    (void)arg;
    s_marker_sock = socket(AF_INET, SOCK_DGRAM, IPPROTO_IP);
    if (s_marker_sock < 0) {
        vTaskDelete(NULL);
    }
    struct sockaddr_in listen_addr = {.sin_family = AF_INET, .sin_port = htons(P300_MARKER_PORT),
                                      .sin_addr.s_addr = htonl(INADDR_ANY)};
    if (bind(s_marker_sock, (const struct sockaddr *)&listen_addr, sizeof(listen_addr)) != 0) {
        close(s_marker_sock);
        s_marker_sock = -1;
        vTaskDelete(NULL);
    }
    portENTER_CRITICAL(&s_mux);
    s_stats.udp_ready = 1;
    portEXIT_CRITICAL(&s_mux);
    struct timeval timeout = {.tv_sec = 0, .tv_usec = 5000};
    setsockopt(s_marker_sock, SOL_SOCKET, SO_RCVTIMEO, &timeout, sizeof(timeout));
    uint8_t packet[64];
    while (!s_stop_requested) {
        struct sockaddr_in source = {0};
        socklen_t source_len = sizeof(source);
        const int received = recvfrom(s_marker_sock, packet, sizeof(packet), 0,
                                      (struct sockaddr *)&source, &source_len);
        if (received>0) dispatch_control(packet,(size_t)received,&source,source_len);
        serial_control_t usb;
        while (xQueueReceive(s_controls,&usb,0)==pdTRUE) {
            struct sockaddr_in serial={.sin_family=AF_UNSPEC,.sin_addr.s_addr=usb.generation};
            dispatch_control(usb.packet,usb.size,&serial,0);
        }
        process_pending_markers();
    }
    close(s_marker_sock);
    s_marker_sock = -1;
    portENTER_CRITICAL(&s_mux);
    s_stats.udp_ready = 0;
    portEXIT_CRITICAL(&s_mux);
    s_marker_task = NULL;
    vTaskDelete(NULL);
}

static void stream_task(void *arg)
{
    (void)arg;
    struct sockaddr_in target = {0};
    p300_batch_t building = {0};
    uint8_t building_count = 0;
    int64_t last_connect_us = 0;
    while (!s_stop_requested) {
        const int64_t loop_now_us = esp_timer_get_time();
        if (s_requested_mode>=0) {
            close_tcp_socket(); s_serial_connected=false;
            building_count=0; memset(&building,0,sizeof(building)); clear_batch_fifo();
            if (eeg_acq_get_config_id()!=EEG_ACQ_CONFIG_NORMAL) restore_normal_after_disconnect();
            portENTER_CRITICAL(&s_mux);
            memset(&s_pending,0,sizeof(s_pending)); memset(&s_dedup,0,sizeof(s_dedup));
            memset(&s_clock,0,sizeof(s_clock)); s_clock_last_update_us=0;
            s_unix_ready=false; s_pending_unix=0;
            portEXIT_CRITICAL(&s_mux);
            s_wifi_mode=s_requested_mode==1; runtime_config_set_mode(s_wifi_mode);
            s_serial_paused=false;
            renew_stream_id(); s_serial_config_stream=0; s_requested_mode=-1;
        }
        bool serial_alive=uart_link_active();
        if (!s_wifi_mode && s_serial_connected && (!serial_alive || s_serial_generation!=uart_link_generation())) {
            s_serial_connected=false; s_unix_ready=false; clear_batch_fifo();
            s_restore_normal_requested=true;
            portENTER_CRITICAL(&s_mux); memset(&s_clock,0,sizeof(s_clock));
            s_clock_last_update_us=0; portEXIT_CRITICAL(&s_mux);
            renew_stream_id();
        }
        if (p300_tx_group_due(&s_tx_group, loop_now_us,
                              P300_TCP_COALESCE_MAX_WAIT_MS)) {
            flush_tx_group();
        }
        if (s_restore_normal_requested) {
            flush_tx_group();
            memset(&building, 0, sizeof(building));
            building_count = 0;
            restore_normal_after_disconnect();
        }
        if (s_ads_fault) {
            flush_tx_group();
            if (!s_fault_recovery_exhausted) {
                recover_normal_config();
            }
            vTaskDelay(pdMS_TO_TICKS(10));
            continue;
        }
        portENTER_CRITICAL(&s_mux);
        if (!s_unix_ready && s_pending_unix) {
            s_unix_offset_us=(int64_t)s_pending_unix-(int64_t)s_pending_pc;
            s_unix_ready=true; s_pending_unix=0;
        }
        portEXIT_CRITICAL(&s_mux);
        if (!s_wifi_mode && !s_serial_paused && serial_alive && s_unix_ready &&
            (!s_serial_connected || s_serial_config_stream!=s_stream_id)) {
            s_serial_connected=send_stream_config();
            if (s_serial_connected) { s_serial_config_stream=s_stream_id; s_serial_generation=uart_link_generation(); }
        }
        eeg_acq_sample_t source;
        while (building_count < P300_EEG_BATCH_SAMPLES && eeg_acq_pop(&source)) {
            p300_sample_t *sample = &building.samples[building_count];
            sample->seq = source.seq;
            sample->monotonic_us = source.timestamp_us;
            memcpy(sample->channel, source.channel, sizeof(sample->channel));
            if (building_count == 0) {
                building.first_us = sample->monotonic_us;
                building.config_id = eeg_acq_get_config_id();
            }
            building_count++;
        }
        if (building_count == P300_EEG_BATCH_SAMPLES) {
            queue_batch(&building);
            memset(&building, 0, sizeof(building));
            building_count = 0;
        }
        const int64_t now_us = esp_timer_get_time();
        pending_self_test_t self_test_request = {0};
        if (building_count == 0U && take_self_test_request(&self_test_request)) {
            flush_tx_group();
            if (self_test_request.request.target_config_id != eeg_acq_get_config_id()) {
                clear_batch_fifo();
            }
            const p300_self_test_result_t self_test_result =
                run_self_test_switch(&self_test_request);
            complete_self_test_request(&self_test_request, &self_test_result);
            memset(&building, 0, sizeof(building));
            continue;
        }
        pending_impedance_t impedance_request = {0};
        if (building_count == 0U && take_impedance_request(now_us, &impedance_request)) {
            flush_tx_group();
            clear_batch_fifo();
            const p300_impedance_result_t impedance_result =
                run_impedance_measurement(&impedance_request);
            complete_impedance_request(&impedance_request, &impedance_result);
            memset(&building, 0, sizeof(building));
            continue;
        }
        active_gain_request_t gain_request = {0};
        if (building_count == 0U && take_gain_request(&gain_request)) {
            flush_tx_group();
            clear_batch_fifo();
            run_and_complete_gain_request(&gain_request);
            memset(&building, 0, sizeof(building));
            continue;
        }
        active_bias_request_t bias_request = {0};
        if (building_count == 0U && take_bias_request(&bias_request)) {
            flush_tx_group();
            clear_batch_fifo();
            run_and_complete_bias_request(&bias_request);
            memset(&building, 0, sizeof(building));
            continue;
        }
        active_tx_request_t tx_control_request = {0};
        if (building_count == 0U &&
            take_tx_control_request(&tx_control_request)) {
            run_and_complete_tx_control_request(&tx_control_request);
            continue;
        }
        if (s_wifi_mode && s_tcp_sock < 0 && now_us - last_connect_us >= P300_RECONNECT_MS * 1000LL) {
            last_connect_us = now_us;
            if (valid_tcp_target(&target)) connect_tcp(&target);
        }
        p300_batch_t batch = {0};
        if (data_connected() && take_ready_batch(&batch, now_us)) {
            uint8_t frame[P300_EEG_BATCH_BYTES];
            const size_t length = p300_encode_eeg_batch(s_stream_id, batch.config_id,
                                                         (uint64_t)s_unix_offset_us,
                                                         batch.samples, batch.markers, frame, sizeof(frame));
            if (length == P300_EEG_BATCH_BYTES) {
                queue_tx_frame(&batch, frame, now_us);
            }
        } else {
            vTaskDelay(pdMS_TO_TICKS(1));
        }
    }
    flush_tx_group();
    close_tcp_socket();
    s_stream_task = NULL;
    vTaskDelete(NULL);
}

esp_err_t p300_stream_start(void)
{
    if (s_stream_task != NULL) {
        return ESP_OK;
    }
    s_stop_requested = false;
    s_wifi_mode=runtime_config_wifi_mode();
    s_controls=xQueueCreate(12,sizeof(serial_control_t));
    if (!s_controls) return ESP_ERR_NO_MEM;
    do {
        s_stream_id = esp_random();
    } while (s_stream_id == 0U);
    memset(&s_batches, 0, sizeof(s_batches));
    memset(&s_pending, 0, sizeof(s_pending));
    memset(&s_dedup, 0, sizeof(s_dedup));
    memset(&s_clock, 0, sizeof(s_clock));
    memset(&s_impedance, 0, sizeof(s_impedance));
    memset(&s_impedance_cache, 0, sizeof(s_impedance_cache));
    memset(&s_self_test, 0, sizeof(s_self_test));
    memset(&s_self_test_cache, 0, sizeof(s_self_test_cache));
    memset(&s_bias_source, 0, sizeof(s_bias_source));
    memset(&s_tx_control_source, 0, sizeof(s_tx_control_source));
    memset(&s_stats, 0, sizeof(s_stats));
    p300_tx_group_init(&s_tx_group);
    s_batch_head = s_batch_tail = s_batch_count = s_dedup_next = 0;
    s_unix_ready = false;
    s_impedance_cache_valid = false;
    s_self_test_cache_valid = false;
    s_restore_normal_requested = false;
    s_ads_fault = false;
    s_fault_recovery_exhausted = false;
    p300_gain_state_init(&s_gain_state, eeg_acq_get_normal_gain(), eeg_acq_gain_is_verified());
    p300_bias_state_init(&s_bias_state,
                         eeg_acq_get_normal_bias_mask(),
                         eeg_acq_normal_bias_is_verified());
    p300_tx_control_state_init(&s_tx_control_state,
                               P300_TCP_COALESCE_FRAMES, true);
    if (xTaskCreate(marker_task, "p300_marker", P300_MARKER_TASK_STACK_SIZE,
                    NULL, P300_MARKER_TASK_PRIORITY, &s_marker_task) != pdPASS) {
        return ESP_ERR_NO_MEM;
    }
    if (xTaskCreate(stream_task, "p300_tcp", P300_STREAM_TASK_STACK_SIZE,
                    NULL, P300_STREAM_TASK_PRIORITY, &s_stream_task) != pdPASS) {
        s_stop_requested = true;
        return ESP_ERR_NO_MEM;
    }
    return ESP_OK;
}

void p300_stream_stop(void)
{
    s_stop_requested = true;
    close_tcp_socket();
}

bool p300_stream_is_running(void)
{
    return s_stream_task != NULL;
}

void p300_stream_get_stats(p300_stream_stats_t *stats)
{
    if (stats == NULL) {
        return;
    }
    portENTER_CRITICAL(&s_mux);
    *stats = s_stats;
    stats->connected=data_connected();
    portEXIT_CRITICAL(&s_mux);
}

bool p300_stream_serial_control(const uint8_t *packet,size_t size)
{
    if (!s_controls || size>256) return false;
    serial_control_t command={.generation=uart_link_generation(),.size=size};
    memcpy(command.packet,packet,size);
    return xQueueSend(s_controls,&command,0)==pdTRUE;
}
esp_err_t p300_stream_set_unix_time(uint64_t unix_us,uint64_t pc_us)
{
    if (unix_us<(uint64_t)P300_MIN_UNIX_EPOCH*1000000ULL) return ESP_ERR_INVALID_ARG;
    portENTER_CRITICAL(&s_mux);
    bool valid=p300_clock_is_valid(&s_clock) && esp_timer_get_time()-s_clock_last_update_us<P300_SYNC_MAX_AGE_US;
    if (valid && !s_unix_ready) { s_pending_pc=p300_clock_pc_to_esp(&s_clock,pc_us); s_pending_unix=unix_us; }
    portEXIT_CRITICAL(&s_mux);
    return valid?ESP_OK:ESP_ERR_INVALID_STATE;
}
esp_err_t p300_stream_set_mode(bool wifi)
{
    if (!s_stream_task) return ESP_ERR_INVALID_STATE;
    if (wifi==s_wifi_mode) return ESP_OK;
    portENTER_CRITICAL(&s_mux);
    bool busy=s_requested_mode>=0 || s_impedance.active || s_impedance.pending ||
        s_self_test.active || s_self_test.pending || s_bias_state.active || s_bias_state.pending || s_gain_state.active || s_gain_state.pending;
    if (!busy) s_requested_mode=wifi?1:0;
    portEXIT_CRITICAL(&s_mux);
    if (busy) return ESP_ERR_INVALID_STATE;
    for (int i=0;i<200 && s_requested_mode>=0;i++) vTaskDelay(pdMS_TO_TICKS(10));
    if (s_requested_mode>=0) { s_requested_mode=-1; return ESP_ERR_TIMEOUT; }
    return s_wifi_mode==wifi?ESP_OK:ESP_FAIL;
}
bool p300_stream_wifi_mode(void) { return s_wifi_mode; }
bool p300_stream_time_ready(void) { return s_unix_ready; }
void p300_stream_pause_serial(void)
{
    if (!s_wifi_mode) {
        s_serial_paused=true; s_serial_connected=false; s_restore_normal_requested=true;
        uart_link_new_stream();
    }
}
esp_err_t p300_stream_restart(void)
{
    if (!s_stream_task) return ESP_ERR_INVALID_STATE;
    portENTER_CRITICAL(&s_mux);
    bool busy=s_requested_mode>=0 || s_impedance.active || s_impedance.pending ||
        s_self_test.active || s_self_test.pending || s_bias_state.active || s_bias_state.pending || s_gain_state.active || s_gain_state.pending;
    if (!busy) s_requested_mode=s_wifi_mode?1:0;
    portEXIT_CRITICAL(&s_mux);
    if (busy) return ESP_ERR_INVALID_STATE;
    for (int i=0;i<200 && s_requested_mode>=0;i++) vTaskDelay(pdMS_TO_TICKS(10));
    if (s_requested_mode>=0) { s_requested_mode=-1; return ESP_ERR_TIMEOUT; }
    return ESP_OK;
}
