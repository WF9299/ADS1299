#pragma once

#include <stdbool.h>
#include <stdint.h>

#include "p300_protocol.h"

#ifdef __cplusplus
extern "C" {
#endif

typedef enum {
    P300_GAIN_ACTION_REPLY = 0,
    P300_GAIN_ACTION_QUEUED = 1,
    P300_GAIN_ACTION_WAIT = 2,
} p300_gain_action_t;

typedef struct {
    uint8_t normal_gain;
    bool verified;
    bool pending;
    bool active;
    bool fault;
    p300_gain_control_request_t pending_request;
    bool cache_valid;
    p300_gain_control_result_t cache;
} p300_gain_state_t;

void p300_gain_state_init(p300_gain_state_t *state,
                          uint8_t normal_gain,
                          bool verified);
p300_gain_action_t p300_gain_state_handle(
    p300_gain_state_t *state,
    const p300_gain_control_request_t *request,
    bool tcp_authorized,
    bool control_busy,
    uint32_t stream_id,
    p300_gain_control_result_t *reply,
    bool *cached);
bool p300_gain_state_take_pending(p300_gain_state_t *state,
                                  p300_gain_control_request_t *request);
void p300_gain_state_complete(p300_gain_state_t *state,
                              p300_gain_status_t status,
                              uint8_t actual_gain,
                              uint32_t stream_id,
                              bool verified,
                              p300_gain_control_result_t *reply);
void p300_gain_state_cancel_pending(p300_gain_state_t *state);
void p300_gain_state_clear_fault(p300_gain_state_t *state,
                                 uint8_t actual_gain,
                                 bool verified);

#ifdef __cplusplus
}
#endif
