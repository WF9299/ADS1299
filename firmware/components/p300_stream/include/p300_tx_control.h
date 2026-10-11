#pragma once

#include <stdbool.h>
#include <stdint.h>

#include "p300_protocol.h"

#ifdef __cplusplus
extern "C" {
#endif

typedef enum {
    P300_TX_ACTION_REPLY = 0,
    P300_TX_ACTION_QUEUED = 1,
    P300_TX_ACTION_WAIT = 2,
} p300_tx_action_t;

typedef struct {
    uint8_t active_frames;
    bool verified;
    bool pending;
    bool active;
    p300_tx_control_request_t pending_request;
    bool cache_valid;
    p300_tx_control_result_t cache;
} p300_tx_control_state_t;

void p300_tx_control_state_init(p300_tx_control_state_t *state,
                                uint8_t active_frames,
                                bool verified);
p300_tx_action_t p300_tx_control_state_handle(
    p300_tx_control_state_t *state,
    const p300_tx_control_request_t *request,
    bool tcp_authorized,
    bool control_busy,
    uint32_t stream_id,
    p300_tx_control_result_t *reply,
    bool *cached);
bool p300_tx_control_state_take_pending(p300_tx_control_state_t *state,
                                        p300_tx_control_request_t *request);
void p300_tx_control_state_complete(p300_tx_control_state_t *state,
                                    p300_tx_status_t status,
                                    uint8_t actual_frames,
                                    uint32_t stream_id,
                                    bool verified,
                                    bool changed,
                                    p300_tx_control_result_t *reply);
void p300_tx_control_state_cancel_pending(p300_tx_control_state_t *state);
uint8_t p300_tx_control_state_active_frames(
    const p300_tx_control_state_t *state);

#ifdef __cplusplus
}
#endif
