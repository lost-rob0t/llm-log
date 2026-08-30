:- use_module(library(http/json)).

main :-
    set_stream(user_output, buffer(line)),
    worker_loop.

worker_loop :-
    read_line_to_string(user_input, Line),
    (   Line == end_of_file
    ->  true
    ;   maybe_crash_once,
        atom_json_dict(Line, Request, []),
        dispatch(Request, Reply),
        json_write_dict(user_output, Reply, [width(0)]),
        nl(user_output),
        flush_output(user_output),
        worker_loop
    ).

maybe_crash_once :-
    getenv('LLM_LOG_REASONER_CRASH_MARKER', Marker),
    Marker \== '',
    \+ exists_file(Marker),
    !,
    setup_call_cleanup(open(Marker, write, Stream), write(Stream, crashed), close(Stream)),
    halt(70).
maybe_crash_once.

dispatch(Request, Reply) :-
    get_dict(request_id, Request, RequestId),
    get_dict(operation, Request, Operation),
    (   Operation == "health"
    ->  Reply = _{
            status:ok,
            request_id:RequestId,
            operation:"health",
            rule_version:"fixture.crash_once.health/v1",
            result:_{reasoner:"swipl"}
        }
    ;   Reply = _{
            status:error,
            request_id:RequestId,
            operation:Operation,
            error:_{code:unknown_operation}
        }
    ).

:- initialization(main, main).
