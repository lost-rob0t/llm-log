:- use_module(library(http/json)).

main :-
    set_stream(user_output, buffer(line)),
    worker_loop.

worker_loop :-
    read_line_to_string(user_input, Line),
    (   Line == end_of_file
    ->  true
    ;   atom_json_dict(Line, Request, []),
        dispatch(Request, Reply),
        json_write_dict(user_output, Reply, [width(0)]),
        nl(user_output),
        flush_output(user_output),
        worker_loop
    ).

dispatch(Request, Reply) :-
    get_dict(request_id, Request, RequestId),
    get_dict(operation, Request, Operation),
    (   Operation == "health"
    ->  Reply = _{
            status:ok,
            request_id:RequestId,
            operation:"health",
            rule_version:"fixture.malformed_once.health/v1",
            result:_{reasoner:"swipl"}
        }
    ;   Operation == "event_transport"
    ->  event_transport_reply(RequestId, Request, Reply)
    ;   Reply = _{
            status:error,
            request_id:RequestId,
            operation:Operation,
            error:_{code:unknown_operation}
        }
    ).

event_transport_reply(RequestId, Request, Reply) :-
    getenv('LLM_LOG_REASONER_MALFORMED_MARKER', Marker),
    Marker \== '',
    \+ exists_file(Marker),
    !,
    setup_call_cleanup(open(Marker, write, Stream), write(Stream, malformed), close(Stream)),
    Reply = _{
        status:ok,
        request_id:RequestId,
        operation:"event_transport",
        rule_version:"fixture.malformed_once/v1",
        result:_{transport:"forged-websocket"}
    }.
event_transport_reply(RequestId, Request, Reply) :-
    get_dict(data, Request, Data),
    get_dict(transport, Data, Transport),
    Reply = _{
        status:ok,
        request_id:RequestId,
        operation:"event_transport",
        rule_version:"fixture.malformed_once/v1",
        result:_{transport:Transport}
    }.

:- initialization(main, main).
