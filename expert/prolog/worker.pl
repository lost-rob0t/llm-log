:- use_module(library(http/json)).

worker_protocol_version(1).

main :-
    set_stream(user_output, buffer(line)),
    worker_loop.

worker_loop :-
    read_line_to_string(user_input, Line),
    (   Line == end_of_file
    ->  true
    ;   handle_line(Line),
        worker_loop
    ).

handle_line(Line) :-
    catch(
        atom_json_dict(Line, Request, []),
        Error,
        reply_parse_error(Error)
    ),
    (   is_dict(Request)
    ->  dispatch(Request, Reply),
        json_write_dict(user_output, Reply, [width(0)]),
        nl(user_output),
        flush_output(user_output)
    ;   true
    ).

reply_parse_error(Error) :-
    message_to_string(Error, Message),
    Reply = _{
        status:error,
        error:_{code:invalid_json, message:Message}
    },
    json_write_dict(user_output, Reply, [width(0)]),
    nl(user_output),
    flush_output(user_output),
    fail.

dispatch(Request, Reply) :-
    (   get_dict(version, Request, Version),
        worker_protocol_version(Version),
        get_dict(request_id, Request, RequestId),
        get_dict(operation, Request, Operation),
        get_dict(data, Request, Data)
    ->  dispatch_operation(Operation, RequestId, Data, Reply)
    ;   Reply = _{
            status:error,
            error:_{code:invalid_request}
        }
    ).

dispatch_operation("health", RequestId, _Data, Reply) :-
    !,
    Reply = _{
        status:ok,
        request_id:RequestId,
        operation:"health",
        rule_version:"worker.health/v1",
        result:_{reasoner:"swipl"}
    }.
dispatch_operation("event_transport", RequestId, Data, Reply) :-
    !,
    (   get_dict(transport, Data, Transport),
        string(Transport)
    ->  Reply = _{
            status:ok,
            request_id:RequestId,
            operation:"event_transport",
            rule_version:"fixture.event_transport/v1",
            result:_{transport:Transport}
        }
    ;   Reply = _{
            status:error,
            request_id:RequestId,
            operation:"event_transport",
            error:_{code:invalid_data}
        }
    ).
dispatch_operation(Operation, RequestId, _Data, Reply) :-
    Reply = _{
        status:error,
        request_id:RequestId,
        operation:Operation,
        error:_{code:unknown_operation}
    }.

:- initialization(main, main).
