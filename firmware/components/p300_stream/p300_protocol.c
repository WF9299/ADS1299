#include "p300_protocol.h"

#include <limits.h>
#include <string.h>

static void put_u16_be(uint8_t *dst, uint16_t value)
{
    dst[0] = (uint8_t)(value >> 8);
    dst[1] = (uint8_t)value;
}

static void put_u32_be(uint8_t *dst, uint32_t value)
{
    dst[0] = (uint8_t)(value >> 24);
    dst[1] = (uint8_t)(value >> 16);
    dst[2] = (uint8_t)(value >> 8);
    dst[3] = (uint8_t)value;
}

static void put_u64_be(uint8_t *dst, uint64_t value)
{
    for (int index = 7; index >= 0; --index) {
        dst[7 - index] = (uint8_t)(value >> (index * 8));
    }
}

static void put_int24_be(uint8_t *dst, int32_t value)
{
    const uint32_t raw = (uint32_t)value & 0x00FFFFFFU;
    dst[0] = (uint8_t)(raw >> 16);
    dst[1] = (uint8_t)(raw >> 8);
    dst[2] = (uint8_t)raw;
}

static uint32_t get_u32_be(const uint8_t *src)
{
    return ((uint32_t)src[0] << 24) | ((uint32_t)src[1] << 16) |
           ((uint32_t)src[2] << 8) | src[3];
}

static uint64_t get_u64_be(const uint8_t *src)
{
    uint64_t value = 0;
    for (uint8_t i = 0; i < 8; ++i) {
        value = (value << 8) | src[i];
    }
    return value;
}

uint32_t p300_crc32c(const uint8_t *data, size_t len)
{
    uint32_t crc = 0xFFFFFFFFU;
    for (size_t i = 0; i < len; ++i) {
        crc ^= data[i];
        for (uint8_t bit = 0; bit < 8; ++bit) {
            crc = (crc >> 1) ^ ((crc & 1U) ? 0x82F63B78U : 0U);
        }
    }
    return ~crc;
}

size_t p300_encode_eeg_batch(uint32_t stream_id,
                              uint16_t config_id,
                              uint64_t unix_offset_us,
                              const p300_sample_t samples[P300_EEG_BATCH_SAMPLES],
                              const uint8_t markers[P300_EEG_BATCH_SAMPLES],
                              uint8_t *frame,
                              size_t frame_size)
{
    if (samples == NULL || markers == NULL || frame == NULL || frame_size < P300_EEG_BATCH_BYTES) {
        return 0;
    }

    memset(frame, 0, P300_EEG_BATCH_BYTES);
    frame[0] = 0xA5;
    frame[1] = 0x5A;
    frame[2] = 0x11;
    frame[3] = 0x03;
    put_u32_be(&frame[4], stream_id);
    put_u16_be(&frame[8], config_id);
    put_u32_be(&frame[10], samples[0].seq);
    put_u64_be(&frame[14], unix_offset_us + samples[0].monotonic_us);
    frame[22] = P300_EEG_BATCH_SAMPLES;
    frame[23] = 0xFF;

    uint8_t *record = &frame[24];
    for (uint8_t sample = 0; sample < P300_EEG_BATCH_SAMPLES; ++sample) {
        for (uint8_t channel = 0; channel < P300_EEG_CHANNELS; ++channel) {
            put_int24_be(record + channel * 3U, samples[sample].channel[channel]);
        }
        record[24] = markers[sample];
        record += P300_EEG_SAMPLE_BYTES;
    }
    put_u32_be(&frame[P300_EEG_BATCH_BYTES - 4],
               p300_crc32c(frame, P300_EEG_BATCH_BYTES - 4));
    return P300_EEG_BATCH_BYTES;
}

static void update_fit(p300_clock_model_t *model)
{
    if (model->count == 0) {
        model->valid = false;
        return;
    }

    double mean_pc = 0.0;
    double mean_offset = 0.0;
    for (uint8_t i = 0; i < model->count; ++i) {
        mean_pc += (double)model->pc_us[i];
        mean_offset += (double)model->esp_minus_pc_us[i];
    }
    mean_pc /= model->count;
    mean_offset /= model->count;

    double covariance = 0.0;
    double variance = 0.0;
    for (uint8_t i = 0; i < model->count; ++i) {
        const double centered_pc = (double)model->pc_us[i] - mean_pc;
        covariance += centered_pc * ((double)model->esp_minus_pc_us[i] - mean_offset);
        variance += centered_pc * centered_pc;
    }
    const double drift = variance > 0.0 ? covariance / variance : 0.0;
    model->scale = 1.0 + drift;
    model->offset_us = (int64_t)(mean_offset - drift * mean_pc);
    model->valid = true;
}

bool p300_clock_add_measurement_limits(p300_clock_model_t *model,
                                uint64_t t1_pc_us,
                                uint64_t t2_esp_us,
                                uint64_t t3_esp_us,
                                uint64_t t4_pc_us, uint32_t max_rtt, uint32_t slack)
{
    if (model == NULL || t4_pc_us < t1_pc_us || t3_esp_us < t2_esp_us) {
        return false;
    }
    const uint64_t round_trip = t4_pc_us - t1_pc_us;
    const uint64_t server_time = t3_esp_us - t2_esp_us;
    if (round_trip < server_time) {
        return false;
    }
    const uint32_t rtt_us = (uint32_t)(round_trip - server_time);
    if (rtt_us > max_rtt ||
        (model->count > 0 && rtt_us > model->min_rtt_us + slack)) {
        return false;
    }
    const int64_t first = (int64_t)t2_esp_us - (int64_t)t1_pc_us;
    const int64_t second = (int64_t)t3_esp_us - (int64_t)t4_pc_us;
    const int64_t offset = first / 2 + second / 2 + ((first & 1) + (second & 1)) / 2;
    const uint8_t index = model->next;
    model->pc_us[index] = (t1_pc_us + t4_pc_us) / 2U;
    model->esp_minus_pc_us[index] = offset;
    model->rtt_us[index] = rtt_us;
    model->next = (uint8_t)((index + 1U) % P300_CLOCK_HISTORY);
    if (model->count < P300_CLOCK_HISTORY) {
        model->count++;
    }
    model->min_rtt_us = model->rtt_us[0];
    for (uint8_t i = 1; i < model->count; ++i) {
        if (model->rtt_us[i] < model->min_rtt_us) {
            model->min_rtt_us = model->rtt_us[i];
        }
    }
    update_fit(model);
    return true;
}

bool p300_clock_add_measurement(p300_clock_model_t *model,
    uint64_t t1, uint64_t t2, uint64_t t3, uint64_t t4)
{
    return p300_clock_add_measurement_limits(model,t1,t2,t3,t4,
                                            P300_SYNC_MAX_RTT_US,P300_SYNC_MIN_RTT_SLACK_US);
}

bool p300_clock_is_valid(const p300_clock_model_t *model)
{
    return model != NULL && model->valid;
}

uint64_t p300_clock_pc_to_esp(const p300_clock_model_t *model, uint64_t pc_us)
{
    if (!p300_clock_is_valid(model)) {
        return 0;
    }
    const double mapped = model->scale * (double)pc_us + (double)model->offset_us;
    return mapped <= 0.0 ? 0U : (uint64_t)(mapped + 0.5);
}

bool p300_find_nearest_sample(const p300_sample_t *samples,
                              size_t sample_count,
                              uint64_t target_us,
                              uint8_t *index,
                              uint32_t *error_us)
{
    if (samples == NULL || sample_count == 0 || sample_count > UINT8_MAX ||
        index == NULL || error_us == NULL) {
        return false;
    }
    uint8_t best_index = 0;
    uint64_t best_error = UINT64_MAX;
    for (size_t i = 0; i < sample_count; ++i) {
        const uint64_t time = samples[i].monotonic_us;
        const uint64_t error = time >= target_us ? time - target_us : target_us - time;
        if (error < best_error) {
            best_error = error;
            best_index = (uint8_t)i;
        }
    }
    *index = best_index;
    *error_us = best_error > UINT32_MAX ? UINT32_MAX : (uint32_t)best_error;
    return true;
}

bool p300_parse_marker_packet(const uint8_t *packet,
                              size_t packet_size,
                              p300_marker_event_t *event)
{
    if (packet == NULL || event == NULL || packet_size != P300_MARKER_PACKET_BYTES ||
        packet[0] != 0xA5 || packet[1] != 0x51 || packet[2] != 1 ||
        packet[3] != P300_UDP_TYPE_MARKER) {
        return false;
    }
    event->event_id = get_u32_be(&packet[4]);
    event->code = packet[8];
    event->pc_monotonic_us = get_u64_be(&packet[12]);
    return true;
}

size_t p300_build_sync_reply(uint32_t sequence,
                             uint64_t t1_pc_us,
                             uint64_t t2_esp_us,
                             uint64_t t3_esp_us,
                             uint8_t *packet,
                             size_t packet_size)
{
    if (packet == NULL || packet_size < P300_SYNC_REPLY_BYTES) {
        return 0;
    }
    packet[0] = 0xA5;
    packet[1] = 0x51;
    packet[2] = 1;
    packet[3] = P300_UDP_TYPE_SYNC_REPLY;
    put_u32_be(&packet[4], sequence);
    put_u64_be(&packet[8], t1_pc_us);
    put_u64_be(&packet[16], t2_esp_us);
    put_u64_be(&packet[24], t3_esp_us);
    return P300_SYNC_REPLY_BYTES;
}

size_t p300_build_marker_ack(uint32_t event_id,
                             p300_marker_status_t status,
                             uint32_t sample_seq,
                             uint32_t error_us,
                             uint8_t *packet,
                             size_t packet_size)
{
    if (packet == NULL || packet_size < P300_MARKER_ACK_BYTES) {
        return 0;
    }
    packet[0] = 0xA5;
    packet[1] = 0x51;
    packet[2] = 1;
    packet[3] = P300_UDP_TYPE_MARKER_ACK;
    put_u32_be(&packet[4], event_id);
    packet[8] = (uint8_t)status;
    packet[9] = 0;
    packet[10] = 0;
    packet[11] = 0;
    put_u32_be(&packet[12], sample_seq);
    put_u32_be(&packet[16], error_us);
    return P300_MARKER_ACK_BYTES;
}

bool p300_parse_sync_result_packet(const uint8_t *packet,
                                   size_t packet_size,
                                   p300_sync_measurement_t *measurement)
{
    if (packet == NULL || measurement == NULL || packet_size != P300_SYNC_RESULT_BYTES ||
        packet[0] != 0xA5 || packet[1] != 0x51 || packet[2] != 1 ||
        packet[3] != P300_UDP_TYPE_SYNC_RESULT) {
        return false;
    }
    measurement->sequence = get_u32_be(&packet[4]);
    measurement->t1_pc_us = get_u64_be(&packet[8]);
    measurement->t2_esp_us = get_u64_be(&packet[16]);
    measurement->t3_esp_us = get_u64_be(&packet[24]);
    measurement->t4_pc_us = get_u64_be(&packet[32]);
    return true;
}

p300_impedance_parse_status_t p300_parse_impedance_request(
    const uint8_t *packet,
    size_t packet_size,
    p300_impedance_request_t *request)
{
    if (packet == NULL || request == NULL || packet_size != P300_IMPEDANCE_REQUEST_BYTES ||
        packet[0] != 0xA5 || packet[1] != 0x51 || packet[2] != 1 ||
        packet[3] != P300_UDP_TYPE_IMPEDANCE_REQUEST) {
        return P300_IMPEDANCE_PARSE_DROP;
    }
    if (get_u32_be(&packet[12]) != p300_crc32c(packet, 12)) {
        return P300_IMPEDANCE_PARSE_DROP;
    }

    request->request_id = get_u32_be(&packet[4]);
    request->channel_mask = packet[8];
    if (request->request_id == 0U || request->channel_mask == 0U ||
        packet[9] != 0U || packet[10] != 0U || packet[11] != 0U) {
        return P300_IMPEDANCE_PARSE_INVALID;
    }
    return P300_IMPEDANCE_PARSE_OK;
}

size_t p300_build_impedance_result(const p300_impedance_result_t *result,
                                   uint8_t *packet,
                                   size_t packet_size)
{
    if (result == NULL || packet == NULL || packet_size < P300_IMPEDANCE_RESULT_BYTES) {
        return 0;
    }

    memset(packet, 0, P300_IMPEDANCE_RESULT_BYTES);
    packet[0] = 0xA5;
    packet[1] = 0x51;
    packet[2] = 1;
    packet[3] = P300_UDP_TYPE_IMPEDANCE_RESULT;
    put_u32_be(&packet[4], result->request_id);
    packet[8] = (uint8_t)result->status;
    packet[9] = result->tested_mask;
    packet[10] = result->open_mask;
    packet[11] = result->flags;
    put_u16_be(&packet[12], result->duration_ms);
    put_u16_be(&packet[14], result->sample_count);
    for (uint8_t channel = 0; channel < P300_EEG_CHANNELS; ++channel) {
        put_u32_be(&packet[16 + channel * 4U], result->nominal_ohm[channel]);
        put_u32_be(&packet[48 + channel * 4U], result->excitation_nv_peak[channel]);
    }
    put_u32_be(&packet[80], p300_crc32c(packet, 80));
    return P300_IMPEDANCE_RESULT_BYTES;
}

p300_self_test_parse_status_t p300_parse_self_test_request(
    const uint8_t *packet,
    size_t packet_size,
    p300_self_test_request_t *request)
{
    if (packet == NULL || request == NULL || packet_size != P300_SELF_TEST_REQUEST_BYTES ||
        packet[0] != 0xA5 || packet[1] != 0x51 || packet[2] != 1 ||
        packet[3] != P300_UDP_TYPE_SELF_TEST_REQUEST) {
        return P300_SELF_TEST_PARSE_DROP;
    }
    if (get_u32_be(&packet[12]) != p300_crc32c(packet, 12)) {
        return P300_SELF_TEST_PARSE_DROP;
    }

    request->request_id = get_u32_be(&packet[4]);
    request->target_config_id = packet[8];
    if (request->request_id == 0U ||
        (request->target_config_id != P300_CONFIG_NORMAL &&
         request->target_config_id != P300_CONFIG_INTERNAL_SHORT) ||
        packet[9] != 0U || packet[10] != 0U || packet[11] != 0U) {
        return P300_SELF_TEST_PARSE_INVALID;
    }
    return P300_SELF_TEST_PARSE_OK;
}

size_t p300_build_self_test_result(const p300_self_test_result_t *result,
                                   uint8_t *packet,
                                   size_t packet_size)
{
    if (result == NULL || packet == NULL || packet_size < P300_SELF_TEST_RESULT_BYTES) {
        return 0;
    }

    memset(packet, 0, P300_SELF_TEST_RESULT_BYTES);
    packet[0] = 0xA5;
    packet[1] = 0x51;
    packet[2] = 1;
    packet[3] = P300_UDP_TYPE_SELF_TEST_RESULT;
    put_u32_be(&packet[4], result->request_id);
    packet[8] = (uint8_t)result->status;
    packet[9] = result->requested_config_id;
    packet[10] = result->actual_config_id;
    packet[11] = result->flags;
    put_u32_be(&packet[12], result->stream_id);
    put_u32_be(&packet[16], p300_crc32c(packet, 16));
    return P300_SELF_TEST_RESULT_BYTES;
}

p300_bias_parse_status_t p300_parse_bias_control_request(
    const uint8_t *packet,
    size_t packet_size,
    p300_bias_control_request_t *request)
{
    if (packet == NULL || request == NULL || packet_size != P300_BIAS_CONTROL_REQUEST_BYTES ||
        packet[0] != 0xA5 || packet[1] != 0x51 || packet[2] != 1 ||
        packet[3] != P300_UDP_TYPE_BIAS_CONTROL_REQUEST) {
        return P300_BIAS_PARSE_DROP;
    }
    if (get_u32_be(&packet[12]) != p300_crc32c(packet, 12)) {
        return P300_BIAS_PARSE_DROP;
    }

    request->request_id = get_u32_be(&packet[4]);
    request->operation = (p300_bias_operation_t)packet[8];
    request->requested_mask = packet[9];
    const bool operation_valid = request->operation == P300_BIAS_OPERATION_QUERY ||
                                 request->operation == P300_BIAS_OPERATION_SET ||
                                 request->operation == P300_BIAS_OPERATION_DISABLE;
    const bool mask_valid =
        (request->operation == P300_BIAS_OPERATION_QUERY && request->requested_mask == 0U) ||
        (request->operation == P300_BIAS_OPERATION_DISABLE && request->requested_mask == 0U) ||
        (request->operation == P300_BIAS_OPERATION_SET && request->requested_mask != 0U &&
         (request->requested_mask & (uint8_t)~P300_ADS_CHANNEL_MASK) == 0U);
    if (request->request_id == 0U || !operation_valid || !mask_valid ||
        packet[10] != 0U || packet[11] != 0U) {
        return P300_BIAS_PARSE_INVALID;
    }
    return P300_BIAS_PARSE_OK;
}

size_t p300_build_bias_control_result(const p300_bias_control_result_t *result,
                                      uint8_t *packet,
                                      size_t packet_size)
{
    if (result == NULL || packet == NULL || packet_size < P300_BIAS_CONTROL_RESULT_BYTES) {
        return 0;
    }

    memset(packet, 0, P300_BIAS_CONTROL_RESULT_BYTES);
    packet[0] = 0xA5;
    packet[1] = 0x51;
    packet[2] = 1;
    packet[3] = P300_UDP_TYPE_BIAS_CONTROL_RESULT;
    put_u32_be(&packet[4], result->request_id);
    packet[8] = (uint8_t)result->status;
    packet[9] = result->requested_mask;
    packet[10] = result->actual_mask;
    packet[11] = result->flags;
    put_u32_be(&packet[12], result->stream_id);
    put_u32_be(&packet[16], p300_crc32c(packet, 16));
    return P300_BIAS_CONTROL_RESULT_BYTES;
}

p300_tx_parse_status_t p300_parse_tx_control_request(
    const uint8_t *packet,
    size_t packet_size,
    p300_tx_control_request_t *request)
{
    if (packet == NULL || request == NULL ||
        packet_size != P300_TX_CONTROL_REQUEST_BYTES ||
        packet[0] != 0xA5 || packet[1] != 0x51 || packet[2] != 1 ||
        packet[3] != P300_UDP_TYPE_TX_CONTROL_REQUEST) {
        return P300_TX_PARSE_DROP;
    }
    if (get_u32_be(&packet[12]) != p300_crc32c(packet, 12)) {
        return P300_TX_PARSE_DROP;
    }

    request->request_id = get_u32_be(&packet[4]);
    request->operation = (p300_tx_operation_t)packet[8];
    request->requested_frames = packet[9];
    const bool operation_valid =
        request->operation == P300_TX_OPERATION_QUERY ||
        request->operation == P300_TX_OPERATION_SET;
    const bool frames_valid =
        (request->operation == P300_TX_OPERATION_QUERY &&
         request->requested_frames == 0U) ||
        (request->operation == P300_TX_OPERATION_SET &&
         (request->requested_frames == 1U ||
          request->requested_frames == 2U));
    if (request->request_id == 0U || !operation_valid || !frames_valid ||
        packet[10] != 0U || packet[11] != 0U) {
        return P300_TX_PARSE_INVALID;
    }
    return P300_TX_PARSE_OK;
}

size_t p300_build_tx_control_result(const p300_tx_control_result_t *result,
                                    uint8_t *packet,
                                    size_t packet_size)
{
    if (result == NULL || packet == NULL ||
        packet_size < P300_TX_CONTROL_RESULT_BYTES) {
        return 0;
    }

    memset(packet, 0, P300_TX_CONTROL_RESULT_BYTES);
    packet[0] = 0xA5;
    packet[1] = 0x51;
    packet[2] = 1;
    packet[3] = P300_UDP_TYPE_TX_CONTROL_RESULT;
    put_u32_be(&packet[4], result->request_id);
    packet[8] = (uint8_t)result->status;
    packet[9] = result->requested_frames;
    packet[10] = result->actual_frames;
    packet[11] = result->flags;
    put_u32_be(&packet[12], result->stream_id);
    put_u32_be(&packet[16], p300_crc32c(packet, 16));
    return P300_TX_CONTROL_RESULT_BYTES;
}

bool p300_gain_valid(uint8_t gain)
{
    return gain == 1 || gain == 2 || gain == 4 || gain == 6 ||
           gain == 8 || gain == 12 || gain == 24;
}

p300_gain_parse_status_t p300_parse_gain_control_request(
    const uint8_t *packet,
    size_t packet_size,
    p300_gain_control_request_t *request)
{
    if (packet == NULL || request == NULL ||
        packet_size != P300_GAIN_CONTROL_REQUEST_BYTES ||
        packet[0] != 0xA5 || packet[1] != 0x51 || packet[2] != 1 ||
        packet[3] != P300_UDP_TYPE_GAIN_CONTROL_REQUEST) {
        return P300_GAIN_PARSE_DROP;
    }
    if (get_u32_be(&packet[12]) != p300_crc32c(packet, 12)) {
        return P300_GAIN_PARSE_DROP;
    }

    request->request_id = get_u32_be(&packet[4]);
    request->operation = (p300_gain_operation_t)packet[8];
    request->requested_gain = packet[9];
    const bool operation_valid =
        request->operation == P300_GAIN_OPERATION_QUERY ||
        request->operation == P300_GAIN_OPERATION_SET;
    const bool gain_valid =
        (request->operation == P300_GAIN_OPERATION_QUERY &&
         request->requested_gain == 0U) ||
        (request->operation == P300_GAIN_OPERATION_SET &&
         p300_gain_valid(request->requested_gain));
    if (request->request_id == 0U || !operation_valid || !gain_valid ||
        packet[10] != 0U || packet[11] != 0U) {
        return P300_GAIN_PARSE_INVALID;
    }
    return P300_GAIN_PARSE_OK;
}

size_t p300_build_gain_control_result(const p300_gain_control_result_t *result,
                                      uint8_t *packet,
                                      size_t packet_size)
{
    if (result == NULL || packet == NULL || packet_size < P300_GAIN_CONTROL_RESULT_BYTES) {
        return 0;
    }

    memset(packet, 0, P300_GAIN_CONTROL_RESULT_BYTES);
    packet[0] = 0xA5;
    packet[1] = 0x51;
    packet[2] = 1;
    packet[3] = P300_UDP_TYPE_GAIN_CONTROL_RESULT;
    put_u32_be(&packet[4], result->request_id);
    packet[8] = (uint8_t)result->status;
    packet[9] = result->requested_gain;
    packet[10] = result->actual_gain;
    packet[11] = result->flags;
    put_u32_be(&packet[12], result->stream_id);
    put_u32_be(&packet[16], p300_crc32c(packet, 16));
    return P300_GAIN_CONTROL_RESULT_BYTES;
}
