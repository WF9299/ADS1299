#include "p300_tx_group.h"

#include <string.h>

void p300_tx_group_init(p300_tx_group_t *group)
{
    p300_tx_group_clear(group);
}

void p300_tx_group_clear(p300_tx_group_t *group)
{
    if (group != NULL) {
        memset(group, 0, sizeof(*group));
    }
}

bool p300_tx_group_empty(const p300_tx_group_t *group)
{
    return group == NULL || group->frame_count == 0U;
}

bool p300_tx_group_can_append(const p300_tx_group_t *group,
                              uint32_t stream_id,
                              uint16_t config_id,
                              uint32_t start_seq)
{
    if (group == NULL || group->frame_count >= P300_TX_GROUP_MAX_FRAMES) {
        return false;
    }
    return group->frame_count == 0U ||
           (group->stream_id == stream_id &&
            group->config_id == config_id &&
            group->next_seq == start_seq);
}

bool p300_tx_group_append(p300_tx_group_t *group,
                          const uint8_t frame[P300_EEG_BATCH_BYTES],
                          uint32_t stream_id,
                          uint16_t config_id,
                          uint32_t start_seq,
                          int64_t queued_us)
{
    if (group == NULL || frame == NULL ||
        !p300_tx_group_can_append(group, stream_id, config_id, start_seq)) {
        return false;
    }
    if (group->frame_count == 0U) {
        group->stream_id = stream_id;
        group->config_id = config_id;
        group->first_queued_us = queued_us;
    }
    memcpy(group->payload + group->size, frame, P300_EEG_BATCH_BYTES);
    group->size += P300_EEG_BATCH_BYTES;
    group->frame_count++;
    group->next_seq = start_seq + P300_EEG_BATCH_SAMPLES;
    return true;
}

bool p300_tx_group_full(const p300_tx_group_t *group, uint8_t target_frames)
{
    return group != NULL &&
           target_frames >= 1U &&
           target_frames <= P300_TX_GROUP_MAX_FRAMES &&
           group->frame_count >= target_frames;
}

bool p300_tx_group_due(const p300_tx_group_t *group,
                       int64_t now_us,
                       uint32_t max_wait_ms)
{
    return group != NULL &&
           group->frame_count > 0U &&
           now_us >= group->first_queued_us &&
           (uint64_t)(now_us - group->first_queued_us) >=
               (uint64_t)max_wait_ms * 1000ULL;
}

const uint8_t *p300_tx_group_data(const p300_tx_group_t *group)
{
    return group == NULL ? NULL : group->payload;
}

size_t p300_tx_group_size(const p300_tx_group_t *group)
{
    return group == NULL ? 0U : group->size;
}

uint8_t p300_tx_group_frame_count(const p300_tx_group_t *group)
{
    return group == NULL ? 0U : group->frame_count;
}
