#pragma once

#include <stdbool.h>
#include <stddef.h>
#include <stdint.h>

#include "p300_protocol.h"

#ifdef __cplusplus
extern "C" {
#endif

#define P300_TX_GROUP_MAX_FRAMES 2U
#define P300_TX_GROUP_MAX_BYTES (P300_TX_GROUP_MAX_FRAMES * P300_EEG_BATCH_BYTES)

typedef struct {
    uint8_t payload[P300_TX_GROUP_MAX_BYTES];
    size_t size;
    uint8_t frame_count;
    uint32_t stream_id;
    uint16_t config_id;
    uint32_t next_seq;
    int64_t first_queued_us;
} p300_tx_group_t;

void p300_tx_group_init(p300_tx_group_t *group);
void p300_tx_group_clear(p300_tx_group_t *group);
bool p300_tx_group_empty(const p300_tx_group_t *group);
bool p300_tx_group_can_append(const p300_tx_group_t *group,
                              uint32_t stream_id,
                              uint16_t config_id,
                              uint32_t start_seq);
bool p300_tx_group_append(p300_tx_group_t *group,
                          const uint8_t frame[P300_EEG_BATCH_BYTES],
                          uint32_t stream_id,
                          uint16_t config_id,
                          uint32_t start_seq,
                          int64_t queued_us);
bool p300_tx_group_full(const p300_tx_group_t *group, uint8_t target_frames);
bool p300_tx_group_due(const p300_tx_group_t *group,
                       int64_t now_us,
                       uint32_t max_wait_ms);
const uint8_t *p300_tx_group_data(const p300_tx_group_t *group);
size_t p300_tx_group_size(const p300_tx_group_t *group);
uint8_t p300_tx_group_frame_count(const p300_tx_group_t *group);

#ifdef __cplusplus
}
#endif
