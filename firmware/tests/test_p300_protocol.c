#include <assert.h>
#include <math.h>
#include <stdint.h>
#include <string.h>

#include "eeg_drdy_health.h"
#include "eeg_impedance_math.h"
#include "p300_bias_state.h"
#include "p300_protocol.h"
#include "p300_tx_control.h"
#include "p300_tx_group.h"

static uint16_t read_u16_be(const uint8_t *src)
{
    return (uint16_t)(((uint16_t)src[0] << 8) | src[1]);
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

static void put_u64_be(uint8_t *dst, uint64_t value)
{
    for (int i = 7; i >= 0; --i) {
        dst[7 - i] = (uint8_t)(value >> (i * 8));
    }
}

static void test_crc32c_standard_vector(void)
{
    assert(p300_crc32c((const uint8_t *)"123456789", 9) == 0xE3069283U);
}

static void test_batch_has_protocol_layout_and_raw_int24(void)
{
    p300_sample_t samples[P300_EEG_BATCH_SAMPLES] = {0};
    uint8_t markers[P300_EEG_BATCH_SAMPLES] = {0};
    uint8_t frame[P300_EEG_BATCH_BYTES] = {0};
    for (uint8_t sample = 0; sample < P300_EEG_BATCH_SAMPLES; ++sample) {
        samples[sample].seq = 1000U + sample;
        samples[sample].monotonic_us = 5000000ULL + (uint64_t)sample * 1000ULL;
        for (uint8_t channel = 0; channel < P300_EEG_CHANNELS; ++channel) {
            samples[sample].channel[channel] = channel == 0 ? -2 : (int32_t)(sample * 100 + channel);
        }
    }
    markers[13] = 1;

    assert(p300_encode_eeg_batch(0x10203040U, 1U, 1700000000000000ULL,
                                 samples, markers, frame, sizeof(frame)) == P300_EEG_BATCH_BYTES);
    assert(frame[0] == 0xA5 && frame[1] == 0x5A && frame[2] == 0x11 && frame[3] == 0x03);
    assert(read_u32_be(&frame[4]) == 0x10203040U);
    assert(read_u32_be(&frame[10]) == 1000U);
    assert(read_u64_be(&frame[14]) == 1700000005000000ULL);
    assert(frame[22] == P300_EEG_BATCH_SAMPLES && frame[23] == 0xFF);
    assert(frame[24] == 0xFF && frame[25] == 0xFF && frame[26] == 0xFE);
    assert(frame[24 + 13 * P300_EEG_SAMPLE_BYTES + 24] == 1);
    assert(read_u32_be(&frame[P300_EEG_BATCH_BYTES - 4]) ==
           p300_crc32c(frame, P300_EEG_BATCH_BYTES - 4));
}

static void test_clock_mapping_and_nearest_sample(void)
{
    p300_clock_model_t clock = {0};
    p300_sample_t samples[P300_EEG_BATCH_SAMPLES] = {0};
    uint8_t index = 0;
    uint32_t error_us = 0;

    assert(p300_clock_add_measurement(&clock, 1000000ULL, 5010010ULL,
                                      5010020ULL, 1000030ULL));
    assert(p300_clock_is_valid(&clock));
    assert(p300_clock_pc_to_esp(&clock, 1005000ULL) == 5015000ULL);

    for (uint8_t i = 0; i < P300_EEG_BATCH_SAMPLES; ++i) {
        samples[i].seq = 200U + i;
        samples[i].monotonic_us = 5010000ULL + (uint64_t)i * 1000ULL;
    }
    assert(p300_find_nearest_sample(samples, P300_EEG_BATCH_SAMPLES,
                                    5015300ULL, &index, &error_us));
    assert(index == 5 && error_us == 300U);
}

static void test_clock_history_is_64_and_tracks_window_minimum_rtt(void)
{
    p300_clock_model_t clock = {0};
    assert(P300_CLOCK_HISTORY == 64U);
    for (uint32_t i = 0; i < P300_CLOCK_HISTORY; ++i) {
        const uint64_t t1 = 1000000ULL + (uint64_t)i * 10000ULL;
        const uint32_t rtt = i == 0 ? 100U : 150U;
        assert(p300_clock_add_measurement(&clock, t1, t1 + 4000000ULL,
                                          t1 + 4000020ULL, t1 + 20ULL + rtt));
    }
    assert(clock.count == P300_CLOCK_HISTORY && clock.next == 0U);
    assert(clock.min_rtt_us == 100U);

    /* Overwrite the oldest/minimum RTT entry and verify the 64-point window
     * recomputes its minimum rather than retaining an obsolete value. */
    assert(p300_clock_add_measurement(&clock, 2000000ULL, 6000000ULL,
                                      6000020ULL, 2002120ULL));
    assert(clock.min_rtt_us == 150U && clock.next == 1U);
    assert(!p300_clock_add_measurement(&clock, 2010000ULL, 6010000ULL,
                                       6010020ULL, 2012171ULL));
}

static void test_marker_and_sync_wire_packets(void)
{
    uint8_t marker[P300_MARKER_PACKET_BYTES] = {0xA5, 0x51, 1, P300_UDP_TYPE_MARKER};
    uint8_t reply[P300_SYNC_REPLY_BYTES] = {0};
    uint8_t ack[P300_MARKER_ACK_BYTES] = {0};
    p300_marker_event_t event = {0};

    put_u32_be(&marker[4], 77U);
    marker[8] = 1;
    put_u64_be(&marker[12], 1005000ULL);
    assert(p300_parse_marker_packet(marker, sizeof(marker), &event));
    assert(event.event_id == 77U && event.code == 1 && event.pc_monotonic_us == 1005000ULL);
    marker[8] = 0;
    assert(p300_parse_marker_packet(marker, sizeof(marker), &event));
    assert(event.code == 0);

    assert(p300_build_sync_reply(9U, 1234ULL, 5678ULL, 5680ULL,
                                 reply, sizeof(reply)) == P300_SYNC_REPLY_BYTES);
    assert(reply[0] == 0xA5 && reply[1] == 0x51 && reply[3] == P300_UDP_TYPE_SYNC_REPLY);
    assert(read_u32_be(&reply[4]) == 9U && read_u64_be(&reply[8]) == 1234ULL);
    assert(read_u64_be(&reply[16]) == 5678ULL && read_u64_be(&reply[24]) == 5680ULL);

    assert(p300_build_marker_ack(77U, P300_MARKER_STATUS_OK, 205U, 300U,
                                 ack, sizeof(ack)) == P300_MARKER_ACK_BYTES);
    assert(ack[3] == P300_UDP_TYPE_MARKER_ACK && read_u32_be(&ack[4]) == 77U);
    assert(ack[8] == P300_MARKER_STATUS_OK && read_u32_be(&ack[12]) == 205U);
    assert(read_u32_be(&ack[16]) == 300U);
}

static void test_sync_result_returns_all_four_timestamps(void)
{
    uint8_t packet[P300_SYNC_RESULT_BYTES] = {0xA5, 0x51, 1, P300_UDP_TYPE_SYNC_RESULT};
    p300_sync_measurement_t measurement = {0};
    put_u32_be(&packet[4], 12U);
    put_u64_be(&packet[8], 1000000ULL);
    put_u64_be(&packet[16], 5010010ULL);
    put_u64_be(&packet[24], 5010020ULL);
    put_u64_be(&packet[32], 1000030ULL);
    assert(p300_parse_sync_result_packet(packet, sizeof(packet), &measurement));
    assert(measurement.sequence == 12U && measurement.t1_pc_us == 1000000ULL);
    assert(measurement.t2_esp_us == 5010010ULL && measurement.t3_esp_us == 5010020ULL);
    assert(measurement.t4_pc_us == 1000030ULL);
}

static void test_impedance_request_and_result_wire_packets(void)
{
    uint8_t request[P300_IMPEDANCE_REQUEST_BYTES] = {
        0xA5, 0x51, 1, P300_UDP_TYPE_IMPEDANCE_REQUEST,
    };
    p300_impedance_request_t parsed = {0};
    put_u32_be(&request[4], 0x10203040U);
    request[8] = 0xFF;
    put_u32_be(&request[12], p300_crc32c(request, 12));
    assert(p300_parse_impedance_request(request, sizeof(request), &parsed) ==
           P300_IMPEDANCE_PARSE_OK);
    assert(parsed.request_id == 0x10203040U && parsed.channel_mask == 0xFFU);

    request[9] = 1;
    put_u32_be(&request[12], p300_crc32c(request, 12));
    assert(p300_parse_impedance_request(request, sizeof(request), &parsed) ==
           P300_IMPEDANCE_PARSE_INVALID);
    request[12] ^= 1U;
    assert(p300_parse_impedance_request(request, sizeof(request), &parsed) ==
           P300_IMPEDANCE_PARSE_DROP);

    p300_impedance_result_t result = {
        .request_id = 0x10203040U,
        .status = P300_IMPEDANCE_STATUS_OK,
        .tested_mask = 0xFF,
        .open_mask = 0x02,
        .flags = P300_IMPEDANCE_FLAG_DC_VALID | P300_IMPEDANCE_FLAG_AC_VALID,
        .duration_ms = 1875,
        .sample_count = 1024,
    };
    for (uint8_t channel = 0; channel < P300_EEG_CHANNELS; ++channel) {
        result.nominal_ohm[channel] = channel == 1 ? P300_IMPEDANCE_INVALID_VALUE
                                                   : (uint32_t)(10000U + channel * 1000U);
        result.excitation_nv_peak[channel] = (uint32_t)(300000U + channel * 1000U);
    }
    uint8_t packet[P300_IMPEDANCE_RESULT_BYTES] = {0};
    assert(p300_build_impedance_result(&result, packet, sizeof(packet)) ==
           P300_IMPEDANCE_RESULT_BYTES);
    assert(packet[0] == 0xA5 && packet[1] == 0x51 && packet[2] == 1 &&
           packet[3] == P300_UDP_TYPE_IMPEDANCE_RESULT);
    assert(read_u32_be(&packet[4]) == result.request_id);
    assert(packet[8] == P300_IMPEDANCE_STATUS_OK && packet[10] == 0x02);
    assert(read_u16_be(&packet[12]) == 1875U && read_u16_be(&packet[14]) == 1024U);
    assert(read_u32_be(&packet[16]) == 10000U);
    assert(read_u32_be(&packet[20]) == P300_IMPEDANCE_INVALID_VALUE);
    assert(read_u32_be(&packet[48]) == 300000U);
    assert(read_u32_be(&packet[80]) == p300_crc32c(packet, 80));
}

static void test_self_test_mode_request_and_result_wire_packets(void)
{
    uint8_t request[P300_SELF_TEST_REQUEST_BYTES] = {
        0xA5, 0x51, 1, P300_UDP_TYPE_SELF_TEST_REQUEST,
    };
    p300_self_test_request_t parsed = {0};
    put_u32_be(&request[4], 0x55667788U);
    request[8] = P300_CONFIG_INTERNAL_SHORT;
    put_u32_be(&request[12], p300_crc32c(request, 12));

    assert(p300_parse_self_test_request(request, sizeof(request), &parsed) ==
           P300_SELF_TEST_PARSE_OK);
    assert(parsed.request_id == 0x55667788U);
    assert(parsed.target_config_id == P300_CONFIG_INTERNAL_SHORT);

    request[8] = 3U;
    put_u32_be(&request[12], p300_crc32c(request, 12));
    assert(p300_parse_self_test_request(request, sizeof(request), &parsed) ==
           P300_SELF_TEST_PARSE_INVALID);
    request[8] = P300_CONFIG_NORMAL;
    request[9] = 1U;
    put_u32_be(&request[12], p300_crc32c(request, 12));
    assert(p300_parse_self_test_request(request, sizeof(request), &parsed) ==
           P300_SELF_TEST_PARSE_INVALID);
    request[12] ^= 1U;
    assert(p300_parse_self_test_request(request, sizeof(request), &parsed) ==
           P300_SELF_TEST_PARSE_DROP);

    const p300_self_test_result_t result = {
        .request_id = 0x55667788U,
        .status = P300_SELF_TEST_STATUS_OK,
        .requested_config_id = P300_CONFIG_INTERNAL_SHORT,
        .actual_config_id = P300_CONFIG_INTERNAL_SHORT,
        .flags = P300_SELF_TEST_FLAG_CHANGED,
        .stream_id = 0x10203040U,
    };
    uint8_t packet[P300_SELF_TEST_RESULT_BYTES] = {0};
    assert(p300_build_self_test_result(&result, packet, sizeof(packet)) ==
           P300_SELF_TEST_RESULT_BYTES);
    assert(packet[0] == 0xA5 && packet[1] == 0x51 && packet[2] == 1 &&
           packet[3] == P300_UDP_TYPE_SELF_TEST_RESULT);
    assert(read_u32_be(&packet[4]) == result.request_id);
    assert(packet[8] == P300_SELF_TEST_STATUS_OK);
    assert(packet[9] == P300_CONFIG_INTERNAL_SHORT);
    assert(packet[10] == P300_CONFIG_INTERNAL_SHORT);
    assert(packet[11] == P300_SELF_TEST_FLAG_CHANGED);
    assert(read_u32_be(&packet[12]) == result.stream_id);
    assert(read_u32_be(&packet[16]) == p300_crc32c(packet, 16));
}

static void test_bias_control_request_and_result_wire_packets(void)
{
    uint8_t request[P300_BIAS_CONTROL_REQUEST_BYTES] = {
        0xA5, 0x51, 1, P300_UDP_TYPE_BIAS_CONTROL_REQUEST,
    };
    p300_bias_control_request_t parsed = {0};
    put_u32_be(&request[4], 0x89ABCDEFU);
    request[8] = P300_BIAS_OPERATION_QUERY;
    request[9] = 0U;
    put_u32_be(&request[12], p300_crc32c(request, 12));

    assert(p300_parse_bias_control_request(request, sizeof(request), &parsed) ==
           P300_BIAS_PARSE_OK);
    assert(parsed.request_id == 0x89ABCDEFU);
    assert(parsed.operation == P300_BIAS_OPERATION_QUERY);
    assert(parsed.requested_mask == 0U);

    request[8] = P300_BIAS_OPERATION_SET;
    request[9] = 0x35U;
    put_u32_be(&request[12], p300_crc32c(request, 12));
    assert(p300_parse_bias_control_request(request, sizeof(request), &parsed) ==
           P300_BIAS_PARSE_OK);
    assert(parsed.operation == P300_BIAS_OPERATION_SET && parsed.requested_mask == 0x35U);

    request[8] = 2U;
    put_u32_be(&request[12], p300_crc32c(request, 12));
    assert(p300_parse_bias_control_request(request, sizeof(request), &parsed) ==
           P300_BIAS_PARSE_INVALID);
    request[8] = P300_BIAS_OPERATION_QUERY;
    put_u32_be(&request[12], p300_crc32c(request, 12));
    assert(p300_parse_bias_control_request(request, sizeof(request), &parsed) ==
           P300_BIAS_PARSE_INVALID);
    request[8] = P300_BIAS_OPERATION_SET;
    request[9] = 0U;
    put_u32_be(&request[12], p300_crc32c(request, 12));
    assert(p300_parse_bias_control_request(request, sizeof(request), &parsed) ==
           P300_BIAS_PARSE_INVALID);
    request[8] = P300_BIAS_OPERATION_DISABLE;
    put_u32_be(&request[12], p300_crc32c(request, 12));
    assert(p300_parse_bias_control_request(request, sizeof(request), &parsed) ==
           P300_BIAS_PARSE_OK);
    assert(parsed.operation == P300_BIAS_OPERATION_DISABLE && parsed.requested_mask == 0U);
    request[8] = P300_BIAS_OPERATION_SET;
    request[9] = 0x35U;
    request[10] = 1U;
    put_u32_be(&request[12], p300_crc32c(request, 12));
    assert(p300_parse_bias_control_request(request, sizeof(request), &parsed) ==
           P300_BIAS_PARSE_INVALID);
    request[10] = 0U;
    put_u32_be(&request[4], 0U);
    put_u32_be(&request[12], p300_crc32c(request, 12));
    assert(p300_parse_bias_control_request(request, sizeof(request), &parsed) ==
           P300_BIAS_PARSE_INVALID);
    put_u32_be(&request[4], 0x89ABCDEFU);
    put_u32_be(&request[12], p300_crc32c(request, 12));
    request[12] ^= 1U;
    assert(p300_parse_bias_control_request(request, sizeof(request), &parsed) ==
           P300_BIAS_PARSE_DROP);
    assert(p300_parse_bias_control_request(request, sizeof(request) - 1U, &parsed) ==
           P300_BIAS_PARSE_DROP);

    const p300_bias_control_result_t result = {
        .request_id = 0x89ABCDEFU,
        .status = P300_BIAS_STATUS_APPLY_FAILED,
        .requested_mask = 0x35U,
        .actual_mask = 0xFFU,
        .flags = P300_BIAS_FLAG_CHANGED | P300_BIAS_FLAG_VERIFIED,
        .stream_id = 0x10203040U,
    };
    uint8_t packet[P300_BIAS_CONTROL_RESULT_BYTES] = {0};
    assert(p300_build_bias_control_result(&result, packet, sizeof(packet)) ==
           P300_BIAS_CONTROL_RESULT_BYTES);
    assert(packet[0] == 0xA5 && packet[1] == 0x51 && packet[2] == 1 &&
           packet[3] == P300_UDP_TYPE_BIAS_CONTROL_RESULT);
    assert(read_u32_be(&packet[4]) == result.request_id);
    assert(packet[8] == P300_BIAS_STATUS_APPLY_FAILED);
    assert(packet[9] == 0x35U && packet[10] == 0xFFU);
    assert(packet[11] == (P300_BIAS_FLAG_CHANGED | P300_BIAS_FLAG_VERIFIED));
    assert(read_u32_be(&packet[12]) == result.stream_id);
    assert(read_u32_be(&packet[16]) == p300_crc32c(packet, 16));
    assert(p300_build_bias_control_result(&result, packet,
                                          sizeof(packet) - 1U) == 0U);
}

static void test_bias_runtime_query_set_idempotency_and_failure(void)
{
    p300_bias_state_t state = {0};
    p300_bias_control_result_t reply = {0};
    bool cached = false;
    p300_bias_state_init(&state, 0xFFU, true);

    const p300_bias_control_request_t query = {
        .request_id = 1U,
        .operation = P300_BIAS_OPERATION_QUERY,
        .requested_mask = 0U,
    };
    assert(p300_bias_state_handle(&state, &query, true, false, 100U,
                                  &reply, &cached) == P300_BIAS_ACTION_REPLY);
    assert(reply.status == P300_BIAS_STATUS_OK && reply.actual_mask == 0xFFU);
    assert(reply.flags == (P300_BIAS_FLAG_VERIFIED | P300_BIAS_FLAG_POWER_CONTROL) && !cached);

    const p300_bias_control_request_t same = {
        .request_id = 2U,
        .operation = P300_BIAS_OPERATION_SET,
        .requested_mask = 0xFFU,
    };
    assert(p300_bias_state_handle(&state, &same, true, false, 100U,
                                  &reply, &cached) == P300_BIAS_ACTION_REPLY);
    assert(reply.status == P300_BIAS_STATUS_OK && reply.stream_id == 100U);
    assert((reply.flags & P300_BIAS_FLAG_CHANGED) == 0U);

    const p300_bias_control_request_t change = {
        .request_id = 3U,
        .operation = P300_BIAS_OPERATION_SET,
        .requested_mask = 0x35U,
    };
    assert(p300_bias_state_handle(&state, &change, true, false, 100U,
                                  &reply, &cached) == P300_BIAS_ACTION_QUEUED);
    assert(p300_bias_state_handle(&state, &change, true, false, 100U,
                                  &reply, &cached) == P300_BIAS_ACTION_WAIT);
    const p300_bias_control_request_t competing = {
        .request_id = 4U,
        .operation = P300_BIAS_OPERATION_SET,
        .requested_mask = 0x0FU,
    };
    assert(p300_bias_state_handle(&state, &competing, true, false, 100U,
                                  &reply, &cached) == P300_BIAS_ACTION_REPLY);
    assert(reply.status == P300_BIAS_STATUS_BUSY);

    p300_bias_control_request_t taken = {0};
    assert(p300_bias_state_take_pending(&state, &taken));
    assert(taken.request_id == 3U && taken.requested_mask == 0x35U);
    p300_bias_state_complete(&state, P300_BIAS_STATUS_OK, 0x35U, 101U, true,
                             &reply);
    assert(reply.status == P300_BIAS_STATUS_OK && reply.actual_mask == 0x35U);
    assert(reply.stream_id == 101U);
    assert(reply.flags == (P300_BIAS_FLAG_CHANGED | P300_BIAS_FLAG_VERIFIED | P300_BIAS_FLAG_POWER_CONTROL));
    assert(p300_bias_state_handle(&state, &change, true, false, 101U,
                                  &reply, &cached) == P300_BIAS_ACTION_REPLY);
    assert(cached && (reply.flags & P300_BIAS_FLAG_CACHED) != 0U);

    const p300_bias_control_request_t rollback = {
        .request_id = 5U,
        .operation = P300_BIAS_OPERATION_SET,
        .requested_mask = 0x03U,
    };
    assert(p300_bias_state_handle(&state, &rollback, true, false, 101U,
                                  &reply, &cached) == P300_BIAS_ACTION_QUEUED);
    assert(p300_bias_state_take_pending(&state, &taken));
    p300_bias_state_complete(&state, P300_BIAS_STATUS_APPLY_FAILED,
                             0x35U, 102U, true, &reply);
    assert(reply.actual_mask == 0x35U && reply.stream_id == 102U);
    assert((reply.flags & P300_BIAS_FLAG_VERIFIED) != 0U);

    const p300_bias_control_request_t fatal = {
        .request_id = 6U,
        .operation = P300_BIAS_OPERATION_SET,
        .requested_mask = 0x01U,
    };
    assert(p300_bias_state_handle(&state, &fatal, true, false, 102U,
                                  &reply, &cached) == P300_BIAS_ACTION_QUEUED);
    assert(p300_bias_state_take_pending(&state, &taken));
    p300_bias_state_complete(&state, P300_BIAS_STATUS_RESTORE_FAILED,
                             0U, 0U, false, &reply);
    assert(reply.actual_mask == 0U && reply.stream_id == 0U);
    assert(reply.flags == (P300_BIAS_FLAG_CHANGED | P300_BIAS_FLAG_POWER_CONTROL) && state.fault);

    const p300_bias_control_request_t unauthorized = {
        .request_id = 7U,
        .operation = P300_BIAS_OPERATION_QUERY,
        .requested_mask = 0U,
    };
    assert(p300_bias_state_handle(&state, &unauthorized, false, false, 0U,
                                  &reply, &cached) == P300_BIAS_ACTION_REPLY);
    assert(reply.status == P300_BIAS_STATUS_NO_TCP_STREAM);
}

static void test_bias_power_off_restore_and_cache(void)
{
    p300_bias_state_t state;
    p300_bias_control_result_t reply;
    p300_bias_control_request_t taken;
    bool cached;
    p300_bias_state_init(&state, 0x35U, true);
    p300_bias_control_request_t request = {20U, P300_BIAS_OPERATION_DISABLE, 0U};
    assert(p300_bias_state_handle(&state, &request, true, false, 200U, &reply, &cached)
           == P300_BIAS_ACTION_QUEUED);
    assert(p300_bias_state_take_pending(&state, &taken));
    p300_bias_state_complete(&state, P300_BIAS_STATUS_OK, 0U, 201U, true, &reply);
    assert(reply.actual_mask == 0U && reply.stream_id == 201U);
    assert((reply.flags & (P300_BIAS_FLAG_VERIFIED | P300_BIAS_FLAG_POWER_CONTROL))
           == (P300_BIAS_FLAG_VERIFIED | P300_BIAS_FLAG_POWER_CONTROL));
    assert(p300_bias_state_handle(&state, &request, true, false, 201U, &reply, &cached)
           == P300_BIAS_ACTION_REPLY);
    assert(cached && reply.actual_mask == 0U);
    request.request_id++;
    request.operation = P300_BIAS_OPERATION_QUERY;
    assert(p300_bias_state_handle(&state, &request, true, false, 201U, &reply, &cached)
           == P300_BIAS_ACTION_REPLY);
    assert(reply.status == P300_BIAS_STATUS_OK && reply.actual_mask == 0U);
    assert(reply.flags & P300_BIAS_FLAG_VERIFIED);
    request.request_id++;
    request.operation = P300_BIAS_OPERATION_DISABLE;
    assert(p300_bias_state_handle(&state, &request, true, false, 201U, &reply, &cached)
           == P300_BIAS_ACTION_REPLY);
    assert(!(reply.flags & P300_BIAS_FLAG_CHANGED));
    request.request_id++;
    request.operation = P300_BIAS_OPERATION_SET;
    request.requested_mask = 0x35U;
    assert(p300_bias_state_handle(&state, &request, true, false, 201U, &reply, &cached)
           == P300_BIAS_ACTION_QUEUED);
    assert(p300_bias_state_take_pending(&state, &taken));
    p300_bias_state_complete(&state, P300_BIAS_STATUS_APPLY_FAILED, 0U, 202U, true, &reply);
    assert(state.normal_mask == 0U && reply.actual_mask == 0U && state.verified);
    request.request_id++;
    assert(p300_bias_state_handle(&state, &request, true, false, 202U, &reply, &cached)
           == P300_BIAS_ACTION_QUEUED);
    assert(p300_bias_state_take_pending(&state, &taken));
    p300_bias_state_complete(&state, P300_BIAS_STATUS_OK, 0x35U, 203U, true, &reply);
    assert(state.normal_mask == 0x35U && state.verified);
}

static int32_t volts_to_code(double volts)
{
    const double code = volts * (12.0 * 8388607.0) / 4.5;
    return (int32_t)(code >= 0.0 ? code + 0.5 : code - 0.5);
}

static void test_impedance_synchronous_demodulation(void)
{
    eeg_impedance_accumulator_t background = {0};
    eeg_impedance_accumulator_t excited = {0};
    eeg_impedance_result_t result = {0};
    const double two_pi = 6.2831853071795864769;
    const double target_ohm = 30000.0;
    const double current_fundamental_peak = 4.0 * 24.0e-9 / 3.14159265358979323846;
    const double excitation_peak_v = target_ohm * current_fundamental_peak;
    const double background_peak_v = 50.0e-6;
    const uint64_t origin_us = 1000000ULL;

    eeg_impedance_accumulator_reset(&background);
    for (uint16_t sample = 0; sample < 256U; ++sample) {
        const uint64_t timestamp_us = origin_us + (uint64_t)sample * 1000ULL;
        const double phase = two_pi * 31.25 * ((double)timestamp_us / 1000000.0);
        int32_t codes[EEG_IMPEDANCE_CHANNELS];
        for (uint8_t channel = 0; channel < EEG_IMPEDANCE_CHANNELS; ++channel) {
            codes[channel] = volts_to_code(background_peak_v * sin(phase + 0.37));
        }
        eeg_impedance_accumulator_add(&background, timestamp_us, codes);
    }

    eeg_impedance_accumulator_reset(&excited);
    for (uint16_t sample = 0; sample < 1024U; ++sample) {
        const uint64_t timestamp_us = origin_us + 512000ULL + (uint64_t)sample * 1000ULL;
        const double phase = two_pi * 31.25 * ((double)timestamp_us / 1000000.0);
        int32_t codes[EEG_IMPEDANCE_CHANNELS];
        for (uint8_t channel = 0; channel < EEG_IMPEDANCE_CHANNELS; ++channel) {
            const double signal = background_peak_v * sin(phase + 0.37) +
                                  excitation_peak_v * cos(phase - 0.19);
            codes[channel] = volts_to_code(signal);
        }
        eeg_impedance_accumulator_add(&excited, timestamp_us, codes);
    }

    assert(eeg_impedance_calculate(&background, &excited, 0xFFU, &result));
    assert(result.valid_mask == 0xFFU && result.clipped_mask == 0U);
    for (uint8_t channel = 0; channel < EEG_IMPEDANCE_CHANNELS; ++channel) {
        assert(fabs(result.nominal_ohm[channel] - target_ohm) < 50.0);
        assert(fabs(result.voltage_peak_v[channel] - excitation_peak_v) < 2.0e-6);
    }
}

static eeg_drdy_health_input_t normal_drdy_health_input(void)
{
    const eeg_drdy_health_input_t input = {
        .hardware_edges = 5000U,
        .isr_edges = 5000U,
        .acquired_samples = 5000U,
        .window_us = 5000000ULL,
        .expected_rate_hz = 1000U,
        .hardware_available = true,
    };
    return input;
}

static void test_drdy_health_classification(void)
{
    assert(eeg_drdy_health_classify(NULL) == EEG_DRDY_HEALTH_UNAVAILABLE);

    eeg_drdy_health_input_t input = normal_drdy_health_input();
    assert(eeg_drdy_health_classify(&input) == EEG_DRDY_HEALTH_OK);
    assert(strcmp(eeg_drdy_health_state_name(EEG_DRDY_HEALTH_OK), "OK") == 0);

    input.hardware_edges = 150U;
    input.isr_edges = 150U;
    input.acquired_samples = 150U;
    assert(eeg_drdy_health_classify(&input) == EEG_DRDY_HEALTH_DRDY_RATE);

    input = normal_drdy_health_input();
    input.isr_edges = 100U;
    input.acquired_samples = 100U;
    assert(eeg_drdy_health_classify(&input) == EEG_DRDY_HEALTH_IRQ_LOSS);

    input = normal_drdy_health_input();
    input.acquired_samples = 100U;
    assert(eeg_drdy_health_classify(&input) == EEG_DRDY_HEALTH_ACQ_STALL);

    input = normal_drdy_health_input();
    input.acquired_samples = 5300U;
    assert(eeg_drdy_health_classify(&input) == EEG_DRDY_HEALTH_RECOVERY_READ);
    input = normal_drdy_health_input();
    input.low_level_recovery = 1U;
    assert(eeg_drdy_health_classify(&input) == EEG_DRDY_HEALTH_RECOVERY_READ);

    input = normal_drdy_health_input();
    input.paused = true;
    input.hardware_edges = 0U;
    assert(eeg_drdy_health_classify(&input) == EEG_DRDY_HEALTH_PAUSED);

    input = normal_drdy_health_input();
    input.hardware_available = false;
    assert(eeg_drdy_health_classify(&input) == EEG_DRDY_HEALTH_UNAVAILABLE);
    assert(strcmp(eeg_drdy_health_state_name((eeg_drdy_health_state_t)99), "UNKNOWN") == 0);
}

static void test_tcp_tx_group_coalesces_two_complete_contiguous_frames(void)
{
    p300_tx_group_t group;
    uint8_t first[P300_EEG_BATCH_BYTES];
    uint8_t second[P300_EEG_BATCH_BYTES];
    memset(first, 0x11, sizeof(first));
    memset(second, 0x22, sizeof(second));
    p300_tx_group_init(&group);

    assert(p300_tx_group_empty(&group));
    assert(p300_tx_group_can_append(&group, 7U, 1U, 100U));
    assert(p300_tx_group_append(&group, first, 7U, 1U, 100U, 1000000LL));
    assert(p300_tx_group_frame_count(&group) == 1U);
    assert(p300_tx_group_size(&group) == P300_EEG_BATCH_BYTES);
    assert(!p300_tx_group_full(&group, 2U));
    assert(!p300_tx_group_due(&group, 1024000LL, 25U));
    assert(p300_tx_group_due(&group, 1025000LL, 25U));

    assert(p300_tx_group_can_append(&group, 7U, 1U, 120U));
    assert(p300_tx_group_append(&group, second, 7U, 1U, 120U, 1020000LL));
    assert(p300_tx_group_full(&group, 2U));
    assert(p300_tx_group_size(&group) == 2U * P300_EEG_BATCH_BYTES);
    assert(memcmp(p300_tx_group_data(&group), first, sizeof(first)) == 0);
    assert(memcmp(p300_tx_group_data(&group) + sizeof(first), second, sizeof(second)) == 0);

    p300_tx_group_clear(&group);
    assert(p300_tx_group_empty(&group));
    assert(p300_tx_group_append(&group, first, 8U, 1U, 200U, 2000000LL));
    assert(!p300_tx_group_can_append(&group, 8U, 1U, 241U));
    assert(!p300_tx_group_can_append(&group, 9U, 1U, 220U));
    assert(!p300_tx_group_can_append(&group, 8U, 2U, 220U));
    assert(p300_tx_group_full(&group, 1U));
}

static void test_tx_control_request_and_result_wire_packets(void)
{
    uint8_t packet[P300_TX_CONTROL_REQUEST_BYTES] = {
        0xA5, 0x51, 1, P300_UDP_TYPE_TX_CONTROL_REQUEST,
        0x12, 0x34, 0x56, 0x78,
        P300_TX_OPERATION_SET, 2, 0, 0,
        0, 0, 0, 0,
    };
    p300_tx_control_request_t request = {0};
    put_u32_be(&packet[12], p300_crc32c(packet, 12));

    assert(p300_parse_tx_control_request(packet, sizeof(packet), &request) ==
           P300_TX_PARSE_OK);
    assert(request.request_id == 0x12345678U);
    assert(request.operation == P300_TX_OPERATION_SET);
    assert(request.requested_frames == 2U);

    packet[8] = P300_TX_OPERATION_QUERY;
    packet[9] = 0U;
    put_u32_be(&packet[12], p300_crc32c(packet, 12));
    assert(p300_parse_tx_control_request(packet, sizeof(packet), &request) ==
           P300_TX_PARSE_OK);

    packet[9] = 1U;
    put_u32_be(&packet[12], p300_crc32c(packet, 12));
    assert(p300_parse_tx_control_request(packet, sizeof(packet), &request) ==
           P300_TX_PARSE_INVALID);
    packet[9] = 0U;
    packet[10] = 1U;
    put_u32_be(&packet[12], p300_crc32c(packet, 12));
    assert(p300_parse_tx_control_request(packet, sizeof(packet), &request) ==
           P300_TX_PARSE_INVALID);
    packet[10] = 0U;
    put_u32_be(&packet[12], p300_crc32c(packet, 12));
    packet[15] ^= 1U;
    assert(p300_parse_tx_control_request(packet, sizeof(packet), &request) ==
           P300_TX_PARSE_DROP);

    const p300_tx_control_result_t result = {
        .request_id = 0x89ABCDEFU,
        .status = P300_TX_STATUS_OK,
        .requested_frames = 2U,
        .actual_frames = 2U,
        .flags = P300_TX_FLAG_CHANGED | P300_TX_FLAG_VERIFIED,
        .stream_id = 0x10203040U,
    };
    uint8_t reply[P300_TX_CONTROL_RESULT_BYTES] = {0};
    assert(p300_build_tx_control_result(&result, reply, sizeof(reply)) ==
           P300_TX_CONTROL_RESULT_BYTES);
    assert(reply[0] == 0xA5 && reply[1] == 0x51 && reply[2] == 1U);
    assert(reply[3] == P300_UDP_TYPE_TX_CONTROL_RESULT);
    assert(read_u32_be(&reply[4]) == result.request_id);
    assert(reply[8] == P300_TX_STATUS_OK);
    assert(reply[9] == 2U && reply[10] == 2U);
    assert(reply[11] == (P300_TX_FLAG_CHANGED | P300_TX_FLAG_VERIFIED));
    assert(read_u32_be(&reply[12]) == result.stream_id);
    assert(read_u32_be(&reply[16]) == p300_crc32c(reply, 16));
}

static void test_tx_control_runtime_query_set_idempotency_and_flush_failure(void)
{
    p300_tx_control_state_t state;
    p300_tx_control_result_t reply = {0};
    bool cached = false;
    p300_tx_control_state_init(&state, 1U, true);
    assert(p300_tx_control_state_active_frames(&state) == 1U);

    const p300_tx_control_request_t query = {
        .request_id = 1U,
        .operation = P300_TX_OPERATION_QUERY,
        .requested_frames = 0U,
    };
    assert(p300_tx_control_state_handle(&state, &query, true, true, 0x1111U,
                                        &reply, &cached) == P300_TX_ACTION_REPLY);
    assert(reply.status == P300_TX_STATUS_OK && reply.actual_frames == 1U);
    assert(reply.flags == P300_TX_FLAG_VERIFIED && !cached);

    const p300_tx_control_request_t set_two = {
        .request_id = 2U,
        .operation = P300_TX_OPERATION_SET,
        .requested_frames = 2U,
    };
    assert(p300_tx_control_state_handle(&state, &set_two, true, false, 0x1111U,
                                        &reply, &cached) == P300_TX_ACTION_QUEUED);
    assert(p300_tx_control_state_handle(&state, &set_two, true, false, 0x1111U,
                                        &reply, &cached) == P300_TX_ACTION_WAIT);

    const p300_tx_control_request_t query_while_pending = {
        .request_id = 3U,
        .operation = P300_TX_OPERATION_QUERY,
        .requested_frames = 0U,
    };
    assert(p300_tx_control_state_handle(&state, &query_while_pending, true, true,
                                        0x1111U, &reply, &cached) ==
           P300_TX_ACTION_REPLY);
    assert(reply.status == P300_TX_STATUS_OK && reply.actual_frames == 1U);

    p300_tx_control_request_t active_request = {0};
    assert(p300_tx_control_state_take_pending(&state, &active_request));
    assert(active_request.request_id == set_two.request_id);
    assert(p300_tx_control_state_handle(&state, &set_two, true, false, 0x1111U,
                                        &reply, &cached) == P300_TX_ACTION_WAIT);
    p300_tx_control_state_complete(&state, P300_TX_STATUS_OK, 2U, 0x1111U,
                                   true, true, &reply);
    assert(reply.actual_frames == 2U);
    assert(reply.flags == (P300_TX_FLAG_CHANGED | P300_TX_FLAG_VERIFIED));
    assert(p300_tx_control_state_active_frames(&state) == 2U);
    assert(p300_tx_control_state_handle(&state, &set_two, true, false, 0x1111U,
                                        &reply, &cached) == P300_TX_ACTION_REPLY);
    assert(cached && (reply.flags & P300_TX_FLAG_CACHED) != 0U);

    const p300_tx_control_request_t set_one = {
        .request_id = 4U,
        .operation = P300_TX_OPERATION_SET,
        .requested_frames = 1U,
    };
    assert(p300_tx_control_state_handle(&state, &set_one, true, false, 0x1111U,
                                        &reply, &cached) == P300_TX_ACTION_QUEUED);
    assert(p300_tx_control_state_take_pending(&state, &active_request));
    p300_tx_control_state_complete(&state, P300_TX_STATUS_NO_TCP_STREAM, 2U, 0U,
                                   true, false, &reply);
    assert(reply.status == P300_TX_STATUS_NO_TCP_STREAM);
    assert(reply.actual_frames == 2U && reply.stream_id == 0U);
    assert(reply.flags == P300_TX_FLAG_VERIFIED);
    assert(p300_tx_control_state_active_frames(&state) == 2U);

    const p300_tx_control_request_t unauthorized = {
        .request_id = 5U,
        .operation = P300_TX_OPERATION_SET,
        .requested_frames = 1U,
    };
    assert(p300_tx_control_state_handle(&state, &unauthorized, false, false, 0U,
                                        &reply, &cached) == P300_TX_ACTION_REPLY);
    assert(reply.status == P300_TX_STATUS_NO_TCP_STREAM);
}

int main(void)
{
    test_crc32c_standard_vector();
    test_batch_has_protocol_layout_and_raw_int24();
    test_clock_mapping_and_nearest_sample();
    test_clock_history_is_64_and_tracks_window_minimum_rtt();
    test_marker_and_sync_wire_packets();
    test_sync_result_returns_all_four_timestamps();
    test_impedance_request_and_result_wire_packets();
    test_self_test_mode_request_and_result_wire_packets();
    test_bias_control_request_and_result_wire_packets();
    test_bias_runtime_query_set_idempotency_and_failure();
    test_bias_power_off_restore_and_cache();
    test_impedance_synchronous_demodulation();
    test_drdy_health_classification();
    test_tcp_tx_group_coalesces_two_complete_contiguous_frames();
    test_tx_control_request_and_result_wire_packets();
    test_tx_control_runtime_query_set_idempotency_and_flush_failure();
    return 0;
}
