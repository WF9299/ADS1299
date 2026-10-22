#include "p300_bias_state.h"

#include <string.h>

static p300_bias_control_result_t make_result(
    const p300_bias_state_t *state,
    const p300_bias_control_request_t *request,
    p300_bias_status_t status,
    uint32_t stream_id)
{
    p300_bias_control_result_t result = {
        .request_id = request->request_id,
        .status = status,
        .requested_mask = request->requested_mask,
        .actual_mask = state->verified ? state->normal_mask : 0U,
        .flags = P300_BIAS_FLAG_POWER_CONTROL |
                 (state->verified ? P300_BIAS_FLAG_VERIFIED : 0U),
        .stream_id = state->verified ? stream_id : 0U,
    };
    return result;
}

static p300_bias_action_t cache_and_reply(p300_bias_state_t *state,
                                          const p300_bias_control_result_t *result,
                                          p300_bias_control_result_t *reply,
                                          bool *cached)
{
    state->cache = *result;
    state->cache_valid = true;
    *reply = *result;
    *cached = false;
    return P300_BIAS_ACTION_REPLY;
}

void p300_bias_state_init(p300_bias_state_t *state,
                          uint8_t normal_mask,
                          bool verified)
{
    if (state == NULL) {
        return;
    }
    memset(state, 0, sizeof(*state));
    state->normal_mask = normal_mask;
    state->verified = verified;
}

p300_bias_action_t p300_bias_state_handle(
    p300_bias_state_t *state,
    const p300_bias_control_request_t *request,
    bool tcp_authorized,
    bool control_busy,
    uint32_t stream_id,
    p300_bias_control_result_t *reply,
    bool *cached)
{
    if (state == NULL || request == NULL || reply == NULL || cached == NULL) {
        return P300_BIAS_ACTION_REPLY;
    }
    *cached = false;

    if (!tcp_authorized) {
        const p300_bias_control_result_t result = make_result(
            state, request, P300_BIAS_STATUS_NO_TCP_STREAM, stream_id);
        *reply = result;
        return P300_BIAS_ACTION_REPLY;
    }
    if (state->cache_valid && state->cache.request_id == request->request_id) {
        *reply = state->cache;
        reply->flags |= P300_BIAS_FLAG_CACHED;
        *cached = true;
        return P300_BIAS_ACTION_REPLY;
    }
    if (state->pending || state->active) {
        if (state->pending_request.request_id == request->request_id) {
            return P300_BIAS_ACTION_WAIT;
        }
        const p300_bias_control_result_t result = make_result(
            state, request, P300_BIAS_STATUS_BUSY, stream_id);
        return cache_and_reply(state, &result, reply, cached);
    }
    if (request->operation == P300_BIAS_OPERATION_QUERY) {
        const p300_bias_control_result_t result = make_result(
            state, request,
            state->fault || !state->verified ? P300_BIAS_STATUS_INTERNAL_ERROR
                                             : P300_BIAS_STATUS_OK,
            stream_id);
        return cache_and_reply(state, &result, reply, cached);
    }
    if (state->fault || control_busy) {
        const p300_bias_control_result_t result = make_result(
            state, request,
            state->fault ? P300_BIAS_STATUS_INTERNAL_ERROR : P300_BIAS_STATUS_BUSY,
            stream_id);
        return cache_and_reply(state, &result, reply, cached);
    }
    if (state->verified && request->requested_mask == state->normal_mask) {
        const p300_bias_control_result_t result = make_result(
            state, request, P300_BIAS_STATUS_OK, stream_id);
        return cache_and_reply(state, &result, reply, cached);
    }

    state->pending = true;
    state->pending_request = *request;
    return P300_BIAS_ACTION_QUEUED;
}

bool p300_bias_state_take_pending(p300_bias_state_t *state,
                                  p300_bias_control_request_t *request)
{
    if (state == NULL || request == NULL || !state->pending || state->active) {
        return false;
    }
    state->pending = false;
    state->active = true;
    *request = state->pending_request;
    return true;
}

void p300_bias_state_complete(p300_bias_state_t *state,
                              p300_bias_status_t status,
                              uint8_t actual_mask,
                              uint32_t stream_id,
                              bool verified,
                              p300_bias_control_result_t *reply)
{
    if (state == NULL || reply == NULL) {
        return;
    }
    const p300_bias_control_request_t request = state->pending_request;
    p300_bias_control_result_t result = {
        .request_id = request.request_id,
        .status = status,
        .requested_mask = request.requested_mask,
        .actual_mask = verified ? actual_mask : 0U,
        .flags = P300_BIAS_FLAG_CHANGED | P300_BIAS_FLAG_POWER_CONTROL |
                 (verified ? P300_BIAS_FLAG_VERIFIED : 0U),
        .stream_id = verified ? stream_id : 0U,
    };
    if (status == P300_BIAS_STATUS_RESTORE_FAILED) {
        result.actual_mask = 0U;
        result.stream_id = 0U;
        result.flags = P300_BIAS_FLAG_CHANGED | P300_BIAS_FLAG_POWER_CONTROL;
        verified = false;
        state->fault = true;
    }
    if (verified) {
        state->normal_mask = actual_mask;
    }
    state->verified = verified;
    state->active = false;
    state->cache = result;
    state->cache_valid = true;
    *reply = result;
}

void p300_bias_state_cancel_pending(p300_bias_state_t *state)
{
    if (state != NULL && state->pending && !state->active) {
        state->pending = false;
    }
}

void p300_bias_state_clear_fault(p300_bias_state_t *state,
                                 uint8_t actual_mask,
                                 bool verified)
{
    if (state == NULL) {
        return;
    }
    state->fault = false;
    state->normal_mask = actual_mask;
    state->verified = verified;
}
