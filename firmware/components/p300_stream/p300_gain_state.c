#include "p300_gain_state.h"

#include <string.h>

static p300_gain_control_result_t make_result(
    const p300_gain_state_t *state,
    const p300_gain_control_request_t *request,
    p300_gain_status_t status,
    uint32_t stream_id)
{
    p300_gain_control_result_t result = {
        .request_id = request->request_id,
        .status = status,
        .requested_gain = request->requested_gain,
        .actual_gain = state->verified ? state->normal_gain : 0U,
        .flags = state->verified ? P300_GAIN_FLAG_VERIFIED : 0U,
        .stream_id = state->verified ? stream_id : 0U,
    };
    return result;
}

static p300_gain_action_t cache_and_reply(p300_gain_state_t *state,
                                          const p300_gain_control_result_t *result,
                                          p300_gain_control_result_t *reply,
                                          bool *cached)
{
    state->cache = *result;
    state->cache_valid = true;
    *reply = *result;
    *cached = false;
    return P300_GAIN_ACTION_REPLY;
}

void p300_gain_state_init(p300_gain_state_t *state,
                          uint8_t normal_gain,
                          bool verified)
{
    if (state == NULL) {
        return;
    }
    memset(state, 0, sizeof(*state));
    state->normal_gain = normal_gain;
    state->verified = verified;
}

p300_gain_action_t p300_gain_state_handle(
    p300_gain_state_t *state,
    const p300_gain_control_request_t *request,
    bool tcp_authorized,
    bool control_busy,
    uint32_t stream_id,
    p300_gain_control_result_t *reply,
    bool *cached)
{
    if (state == NULL || request == NULL || reply == NULL || cached == NULL) {
        return P300_GAIN_ACTION_REPLY;
    }
    *cached = false;

    if (!tcp_authorized) {
        const p300_gain_control_result_t result = make_result(
            state, request, P300_GAIN_STATUS_NO_TCP_STREAM, stream_id);
        *reply = result;
        return P300_GAIN_ACTION_REPLY;
    }
    if (state->cache_valid && state->cache.request_id == request->request_id) {
        *reply = state->cache;
        reply->flags |= P300_GAIN_FLAG_CACHED;
        *cached = true;
        return P300_GAIN_ACTION_REPLY;
    }
    if ((request->operation != P300_GAIN_OPERATION_QUERY &&
         request->operation != P300_GAIN_OPERATION_SET) ||
        (request->operation == P300_GAIN_OPERATION_SET &&
         !p300_gain_valid(request->requested_gain)) ||
        (request->operation == P300_GAIN_OPERATION_QUERY && request->requested_gain != 0)) {
        const p300_gain_control_result_t result = make_result(
            state, request, P300_GAIN_STATUS_INVALID_REQUEST, stream_id);
        return cache_and_reply(state, &result, reply, cached);
    }
    if (state->pending || state->active) {
        if (state->pending_request.request_id == request->request_id) {
            return P300_GAIN_ACTION_WAIT;
        }
        const p300_gain_control_result_t result = make_result(
            state, request, P300_GAIN_STATUS_BUSY, stream_id);
        return cache_and_reply(state, &result, reply, cached);
    }
    if (request->operation == P300_GAIN_OPERATION_QUERY) {
        const p300_gain_control_result_t result = make_result(
            state, request,
            control_busy ? P300_GAIN_STATUS_BUSY :
            (state->fault || !state->verified ? P300_GAIN_STATUS_INTERNAL_ERROR
                                             : P300_GAIN_STATUS_OK),
            stream_id);
        return cache_and_reply(state, &result, reply, cached);
    }
    if (state->fault || control_busy) {
        const p300_gain_control_result_t result = make_result(
            state, request,
            state->fault ? P300_GAIN_STATUS_INTERNAL_ERROR : P300_GAIN_STATUS_BUSY,
            stream_id);
        return cache_and_reply(state, &result, reply, cached);
    }
    if (state->verified && request->requested_gain == state->normal_gain) {
        const p300_gain_control_result_t result = make_result(
            state, request, P300_GAIN_STATUS_OK, stream_id);
        return cache_and_reply(state, &result, reply, cached);
    }

    state->pending = true;
    state->pending_request = *request;
    return P300_GAIN_ACTION_QUEUED;
}

bool p300_gain_state_take_pending(p300_gain_state_t *state,
                                  p300_gain_control_request_t *request)
{
    if (state == NULL || request == NULL || !state->pending || state->active) {
        return false;
    }
    state->pending = false;
    state->active = true;
    *request = state->pending_request;
    return true;
}

void p300_gain_state_complete(p300_gain_state_t *state,
                              p300_gain_status_t status,
                              uint8_t actual_gain,
                              uint32_t stream_id,
                              bool verified,
                              p300_gain_control_result_t *reply)
{
    if (state == NULL || reply == NULL) {
        return;
    }
    const p300_gain_control_request_t request = state->pending_request;
    p300_gain_control_result_t result = {
        .request_id = request.request_id,
        .status = status,
        .requested_gain = request.requested_gain,
        .actual_gain = verified ? actual_gain : 0U,
        .flags = P300_GAIN_FLAG_CHANGED |
                 (verified ? P300_GAIN_FLAG_VERIFIED : 0U),
        .stream_id = verified ? stream_id : 0U,
    };
    if (status == P300_GAIN_STATUS_RESTORE_FAILED) {
        result.actual_gain = 0U;
        result.stream_id = 0U;
        result.flags = P300_GAIN_FLAG_CHANGED;
        verified = false;
        state->fault = true;
    }
    if (verified) {
        state->normal_gain = actual_gain;
    }
    state->verified = verified;
    state->active = false;
    state->cache = result;
    state->cache_valid = true;
    *reply = result;
}

void p300_gain_state_cancel_pending(p300_gain_state_t *state)
{
    if (state != NULL && state->pending && !state->active) {
        state->pending = false;
    }
}

void p300_gain_state_clear_fault(p300_gain_state_t *state,
                                 uint8_t actual_gain,
                                 bool verified)
{
    if (state == NULL) {
        return;
    }
    state->fault = false;
    state->normal_gain = actual_gain;
    state->verified = verified;
}
