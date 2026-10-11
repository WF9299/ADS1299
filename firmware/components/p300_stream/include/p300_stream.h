#pragma once

#include <stdbool.h>
#include <stdint.h>

#include "esp_err.h"

#ifdef __cplusplus
extern "C" {
#endif

typedef struct {
    uint32_t frames_sent;
    uint32_t tcp_fail;
    uint32_t udp_packets_sent;
    uint32_t udp_fail;
    uint32_t dropped_no_connection;
    uint32_t reconnects;
    uint32_t max_buffered;
    uint32_t impedance_requests;
    uint32_t impedance_runs;
    uint32_t intentional_interrupts;
    uint8_t connected;
    uint8_t udp_ready;
    uint8_t impedance_active;
} p300_stream_stats_t;

esp_err_t p300_stream_start(void);
void p300_stream_stop(void);
bool p300_stream_is_running(void);
void p300_stream_get_stats(p300_stream_stats_t *stats);

#ifdef __cplusplus
}
#endif
