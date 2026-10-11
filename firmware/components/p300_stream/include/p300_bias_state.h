#pragma once

#include <stdbool.h>
#include <stdint.h>

#include "p300_protocol.h"

#ifdef __cplusplus
extern "C" {
#endif

typedef enum {
    P300_BIAS_ACTION_REPLY = 0,
    P300_BIAS_ACTION_QUEUED = 1,
    P300_BIAS_ACTION_WAIT = 2,
} p300_bias_action_t;

typedef struct {
    uint8_t normal_mask;
    bool verified;
    bool pending;
    bool active;
    bool fault;
    p300_bias_control_request_t pending_request;
    bool cache_valid;
    p300_bias_control_result_t cache;
} p300_bias_state_t;

void p300_bias_state_init(p300_bias_state_t *state,
                          uint8_t normal_mask,
                          bool verified);
p300_bias_action_t p300_bias_state_handle(
    p300_bias_state_t *state,
    const p300_bias_control_request_t *request,
    bool tcp_authorized,
    bool control_busy,
    uint32_t stream_id,
    p300_bias_control_result_t *reply,
    bool *cached);
bool p300_bias_state_take_pending(p300_bias_state_t *state,
                                  p300_bias_control_request_t *request);
void p300_bias_state_complete(p300_bias_state_t *state,
                              p300_bias_status_t status,
                              uint8_t actual_mask,
                              uint32_t stream_id,
                              bool verified,
                              p300_bias_control_result_t *reply);
void p300_bias_state_cancel_pending(p300_bias_state_t *state);
void p300_bias_state_clear_fault(p300_bias_state_t *state,
                                 uint8_t actual_mask,
                                 bool verified);

#ifdef __cplusplus
}
#endif
