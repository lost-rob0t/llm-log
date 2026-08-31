:- use_module(library(http/json)).
:- use_module(library(lists)).

worker_protocol_version(1).
classifier_rule_version("request.classifier/v1").
task_cost_rule_version("task.cost/v1").
outcome_rule_version("outcome.decision/v1").

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
    catch(atom_json_dict(Line, Request, []), Error, reply_parse_error(Error)),
    (   is_dict(Request)
    ->  dispatch(Request, Reply),
        json_write_dict(user_output, Reply, [width(0)]), nl(user_output), flush_output(user_output)
    ;   true
    ).

reply_parse_error(Error) :-
    message_to_string(Error, Message),
    Reply = _{status:error,error:_{code:invalid_json,message:Message}},
    json_write_dict(user_output, Reply, [width(0)]), nl(user_output), flush_output(user_output), fail.

dispatch(Request, Reply) :-
    (   get_dict(version, Request, Version), worker_protocol_version(Version),
        get_dict(request_id, Request, RequestId), get_dict(operation, Request, Operation),
        get_dict(data, Request, Data)
    ->  dispatch_operation(Operation, RequestId, Data, Reply)
    ;   Reply = _{status:error,error:_{code:invalid_request}}
    ).

dispatch_operation("health", RequestId, _Data, Reply) :-
    !, Reply = _{status:ok,request_id:RequestId,operation:"health",rule_version:"worker.health/v1",result:_{reasoner:"swipl"}}.
dispatch_operation("event_transport", RequestId, Data, Reply) :-
    !,
    (   get_dict(transport, Data, Transport), string(Transport)
    ->  Reply = _{status:ok,request_id:RequestId,operation:"event_transport",rule_version:"fixture.event_transport/v1",result:_{transport:Transport}}
    ;   Reply = _{status:error,request_id:RequestId,operation:"event_transport",error:_{code:invalid_data}}
    ).
dispatch_operation("request_classification", RequestId, Data, Reply) :-
    !,
    (   valid_classifier_data(Data, Message, MessageId, SourceRequestId)
    ->  classify_request(Message, MessageId, SourceRequestId, Assertions),
        classifier_rule_version(RuleVersion),
        Reply = _{status:ok,request_id:RequestId,operation:"request_classification",rule_version:RuleVersion,result:_{assertions:Assertions}}
    ;   Reply = _{status:error,request_id:RequestId,operation:"request_classification",error:_{code:invalid_data}}
    ).
dispatch_operation("task_cost", RequestId, Data, Reply) :-
    !,
    (   valid_task_cost_data(Data, State, Amount)
    ->  task_cost_rule_version(RuleVersion),
        Reply = _{status:ok,request_id:RequestId,operation:"task_cost",rule_version:RuleVersion,result:_{state:State,amount:Amount}}
    ;   Reply = _{status:error,request_id:RequestId,operation:"task_cost",error:_{code:invalid_data}}
    ).
dispatch_operation("outcome_decision", RequestId, Data, Reply) :-
    !,
    (   valid_outcome_data(Data, Evidence, EvidenceIds)
    ->  outcome_decision(Evidence, Outcome, RuleId),
        outcome_rule_version(RuleVersion),
        Reply = _{status:ok,request_id:RequestId,operation:"outcome_decision",rule_version:RuleVersion,
                  result:_{outcome:Outcome,rule_id:RuleId,expert_version:"outcome-expert/1",evidence_ids:EvidenceIds}}
    ;   Reply = _{status:error,request_id:RequestId,operation:"outcome_decision",error:_{code:invalid_data}}
    ).
dispatch_operation(Operation, RequestId, _Data, Reply) :-
    Reply = _{status:error,request_id:RequestId,operation:Operation,error:_{code:unknown_operation}}.

valid_task_cost_data(Data, "unknown", 0) :-
    get_dict(pricing_state, Data, "unknown"), !.
valid_task_cost_data(Data, "known", Amount) :-
    get_dict(pricing_state, Data, "known"),
    get_dict(input_tokens, Data, InputTokens), number(InputTokens), InputTokens >= 0,
    get_dict(output_tokens, Data, OutputTokens), number(OutputTokens), OutputTokens >= 0,
    get_dict(input_per_token, Data, InputPrice), number(InputPrice), InputPrice >= 0,
    get_dict(output_per_token, Data, OutputPrice), number(OutputPrice), OutputPrice >= 0,
    Amount is InputTokens * InputPrice + OutputTokens * OutputPrice.

valid_outcome_data(Data, Evidence, EvidenceIds) :-
    get_dict(scope, Data, Scope), memberchk(Scope, ["request", "task"]),
    get_dict(scope_id, Data, ScopeId), string(ScopeId), ScopeId \= "",
    get_dict(evidence, Data, Evidence), is_list(Evidence), Evidence \= [],
    length(Evidence, Count), Count =< 64,
    maplist(valid_outcome_evidence, Evidence),
    maplist(outcome_evidence_id, Evidence, EvidenceIds).

valid_outcome_evidence(Item) :-
    is_dict(Item),
    get_dict(evidence_id, Item, Id), string(Id), Id \= "",
    get_dict(observed_at, Item, ObservedAt), string(ObservedAt), ObservedAt \= "",
    get_dict(evidence_type, Item, Type),
    memberchk(Type, ["provider_transport", "tool_result", "test_result", "user_feedback", "task_state", "manual_label"]),
    get_dict(authority, Item, Authority), memberchk(Authority, ["weak", "normal", "authoritative"]),
    get_dict(observed_value, Item, _).

outcome_evidence_id(Item, Id) :- get_dict(evidence_id, Item, Id).

outcome_decision(Evidence, "rejected", "outcome.authoritative_user_rejection") :-
    member(Item, Evidence),
    get_dict(evidence_type, Item, "user_feedback"),
    get_dict(authority, Item, "authoritative"),
    get_dict(observed_value, Item, "rejected"), !.
outcome_decision(_Evidence, "unknown", "outcome.insufficient_evidence").

valid_classifier_data(Data, Message, MessageId, SourceRequestId) :-
    get_dict(message, Data, Message), string(Message),
    get_dict(user_message_id, Data, MessageId), string(MessageId), MessageId \= "",
    get_dict(source_request_id, Data, SourceRequestId), string(SourceRequestId), SourceRequestId \= "".

contains(Text, Needle) :- string_lower(Text, Lower), sub_string(Lower, _, _, _, Needle).

classifier_assertion(Message, _Evidence, "activity", "coding", "activity.coding", "asserted", "high") :-
    (contains(Message, "fix"); contains(Message, "code"); contains(Message, "classifier")).
classifier_assertion(Message, _Evidence, "operation", "review", "operation.review.conditional", "ambiguous", "medium") :-
    contains(Message, "review"), (contains(Message, "maybe fix"); contains(Message, "if needed"); contains(Message, "unless necessary")).
classifier_assertion(Message, _Evidence, "operation", "fix", "operation.fix.conditional", "ambiguous", "medium") :-
    contains(Message, "review"), contains(Message, "fix"),
    (contains(Message, "maybe"); contains(Message, "if needed"); contains(Message, "unless necessary")).
classifier_assertion(Message, _Evidence, "operation", "fix", "operation.fix", "asserted", "high") :- contains(Message, "fix").
classifier_assertion(Message, _Evidence, "operation", "test", "operation.test", "asserted", "high") :- (contains(Message, "test"); contains(Message, "tests")).
classifier_assertion(Message, _Evidence, "expected_validation", "test", "validation.test", "asserted", "high") :- (contains(Message, "test"); contains(Message, "tests")).
classifier_assertion(Message, _Evidence, "artifact_target", "code", "artifact.code", "asserted", "medium") :- (contains(Message, "fix"); contains(Message, "code"); contains(Message, "classifier")).
classifier_assertion(Message, _Evidence, "artifact_target", "pr", "artifact.pr", "asserted", "high") :- (contains(Message, " pr"); contains(Message, "pull request")).
classifier_assertion(Message, _Evidence, "execution_locality", "connected_service", "locality.connected_service", "asserted", "medium") :- (contains(Message, " pr"); contains(Message, "pull request")).
classifier_assertion(Message, _Evidence, "authority_effect", "write_without_merge", "authority.write_without_merge", "asserted", "high") :- (contains(Message, "do not merge"); contains(Message, "don't merge"); contains(Message, "without merging")).

assertion_dict(Dimension, Value, RuleId, State, Confidence, Evidence, Assertion) :-
    classifier_rule_version(RuleVersion),
    Assertion = _{dimension:Dimension,value:Value,state:State,confidence:Confidence,rule_id:RuleId,rule_version:RuleVersion,evidence_ids:Evidence,expert_version:"request-classifier/1"}.

classify_request(Message, MessageId, SourceRequestId, Assertions) :-
    Evidence = [MessageId, SourceRequestId],
    findall(Assertion,
            (classifier_assertion(Message, Evidence, Dimension, Value, RuleId, State, Confidence),
             assertion_dict(Dimension, Value, RuleId, State, Confidence, Evidence, Assertion)),
            Found),
    sort(Found, Unique),
    (   Unique = []
    ->  assertion_dict("activity", "unknown", "classifier.no_match", "unknown", "unknown", Evidence, Unknown), Assertions = [Unknown]
    ;   length(Unique, Count), Count =< 32, Assertions = Unique
    ).

:- initialization(main, main).
