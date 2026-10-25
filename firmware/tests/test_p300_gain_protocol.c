#include <assert.h>
#include <string.h>
#include "p300_protocol.h"
#include "p300_gain_state.h"
static void crc(uint8_t *p) {
    uint32_t c=p300_crc32c(p,12);
    p[12]=c>>24; p[13]=c>>16; p[14]=c>>8; p[15]=c;
}
int main(void) {
    assert(P300_UDP_TYPE_GAIN_CONTROL_REQUEST!=P300_UDP_TYPE_TIME_ANCHOR);
    uint8_t p[16]={0xa5,0x51,1,P300_UDP_TYPE_GAIN_CONTROL_REQUEST,0,0,0,1,1,12,0,0};
    const uint8_t gains[]={1,2,4,6,8,12,24};
    p300_gain_control_request_t req;
    for(unsigned i=0;i<7;++i) {
        p[9]=gains[i];crc(p);
        assert(p300_parse_gain_control_request(p,16,&req)==P300_GAIN_PARSE_OK);
        assert(req.requested_gain==gains[i]);
    }
    p[9]=3;crc(p);assert(p300_parse_gain_control_request(p,16,&req)==P300_GAIN_PARSE_INVALID);
    p[9]=12;p[10]=1;crc(p);assert(p300_parse_gain_control_request(p,16,&req)==P300_GAIN_PARSE_INVALID);
    p[10]=0;crc(p);p[15]^=1;assert(p300_parse_gain_control_request(p,16,&req)==P300_GAIN_PARSE_DROP);
    p300_gain_state_t state;
    p300_gain_state_init(&state,12,true);
    p300_gain_control_result_t reply;
    bool cached;
    req=(p300_gain_control_request_t){.request_id=10,.operation=P300_GAIN_OPERATION_QUERY};
    assert(p300_gain_state_handle(&state,&req,true,false,9,&reply,&cached)==P300_GAIN_ACTION_REPLY);
    assert(reply.actual_gain==12 && (reply.flags&P300_GAIN_FLAG_VERIFIED));
    req=(p300_gain_control_request_t){.request_id=11,.operation=P300_GAIN_OPERATION_SET,.requested_gain=24};
    assert(p300_gain_state_handle(&state,&req,false,false,9,&reply,&cached)==P300_GAIN_ACTION_REPLY);
    assert(reply.status==P300_GAIN_STATUS_NO_TCP_STREAM);
    assert(p300_gain_state_handle(&state,&req,true,true,9,&reply,&cached)==P300_GAIN_ACTION_REPLY);
    assert(reply.status==P300_GAIN_STATUS_BUSY);
    req.request_id=12;
    assert(p300_gain_state_handle(&state,&req,true,false,9,&reply,&cached)==P300_GAIN_ACTION_QUEUED);
    assert(p300_gain_state_handle(&state,&req,true,false,9,&reply,&cached)==P300_GAIN_ACTION_WAIT);
    p300_gain_control_request_t active;
    assert(p300_gain_state_take_pending(&state,&active));
    p300_gain_state_complete(&state,P300_GAIN_STATUS_OK,24,10,true,&reply);
    assert(reply.actual_gain==24 && reply.stream_id==10 && (reply.flags&P300_GAIN_FLAG_CHANGED));
    assert(p300_gain_state_handle(&state,&req,true,false,10,&reply,&cached)==P300_GAIN_ACTION_REPLY);
    assert(cached && (reply.flags&P300_GAIN_FLAG_CACHED));
    uint8_t result[20];
    assert(p300_build_gain_control_result(&reply,result,20)==20);
    assert(result[3]==P300_UDP_TYPE_GAIN_CONTROL_RESULT && result[10]==24);
    req.request_id=13;
    assert(p300_gain_state_handle(&state,&req,true,false,10,&reply,&cached)==P300_GAIN_ACTION_REPLY);
    assert(!(reply.flags&P300_GAIN_FLAG_CHANGED));
    req.request_id=14;req.requested_gain=6;
    assert(p300_gain_state_handle(&state,&req,true,false,10,&reply,&cached)==P300_GAIN_ACTION_QUEUED);
    assert(p300_gain_state_take_pending(&state,&active));
    p300_gain_state_complete(&state,P300_GAIN_STATUS_RESTORE_FAILED,24,11,true,&reply);
    assert(state.fault && !state.verified && reply.actual_gain==0 && reply.stream_id==0);
    req.request_id=15;req.operation=P300_GAIN_OPERATION_QUERY;req.requested_gain=0;
    p300_gain_state_handle(&state,&req,true,false,10,&reply,&cached);
    assert(reply.status==P300_GAIN_STATUS_INTERNAL_ERROR && !(reply.flags&P300_GAIN_FLAG_VERIFIED));
    return 0;
}
